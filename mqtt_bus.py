"""
Jembatan MQTT untuk GreenFlow / Flowincheck.

Kontrak topik (prefix default "greenflow", <slug> = slugify(nama lokasi)):

  <prefix>/<slug>/status                       server -> semua   (retained)  hasil deteksi terakhir
  <prefix>/<slug>/config                       server -> perangkat (retained) konfigurasi firmware
  <prefix>/<slug>/cmd                          server -> perangkat            perintah (capture_now, reboot, resend_config)
  <prefix>/<slug>/device/<device_id>/state     perangkat -> server (retained) online/offline (LWT), baterai, RSSI, versi

Dashboard hanya SUBSCRIBE ke status dan device/+/state (lewat MQTT over WebSocket).
Semua perubahan (config, perintah) lewat API server yang memeriksa token admin,
lalu server yang mem-publish. Jadi broker tidak perlu mengizinkan browser menulis.

MQTT bersifat opsional: kalau mqtt.enabled=false, semua method jadi no-op dan
perangkat tetap bekerja lewat HTTP (GET /api/device/config, GET /api/light/latest).
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Callable, Optional

import runtime

log = logging.getLogger("greenflow.mqtt")


class MqttBus:
    def __init__(self, cfg: Optional[dict], slug_to_location: Callable[[str], Optional[str]]):
        self.cfg = cfg or {}
        self.enabled = bool(self.cfg.get("enabled"))
        self.prefix = (self.cfg.get("prefix") or "greenflow").strip("/")
        self._slug_to_location = slug_to_location
        self._client = None
        self._connected = False
        self._lock = threading.Lock()
        # dipanggil (dari thread paho) setiap kali tersambung, mis. untuk publish ulang config retained
        self.on_connected: Optional[Callable[[], None]] = None

    @property
    def connected(self) -> bool:
        return self.enabled and self._connected

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if not self.enabled:
            log.info("MQTT dimatikan (mqtt.enabled=false)")
            return
        import paho.mqtt.client as mqtt   # import malas: server tetap jalan tanpa paho kalau MQTT mati

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="flowincheck-server")
        if self.cfg.get("username"):
            client.username_pw_set(self.cfg["username"], self.cfg.get("password", ""))
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        client.connect_async(self.cfg.get("host", "localhost"), int(self.cfg.get("port", 1883)), keepalive=60)
        client.loop_start()
        self._client = client

    def stop(self) -> None:
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()

    # ------------------------------------------------------------ callbacks
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        self._connected = not getattr(reason_code, "is_failure", False)
        log.info("MQTT terhubung (rc=%s)", reason_code)
        client.subscribe(f"{self.prefix}/+/device/+/state", qos=1)
        if self._connected and self.on_connected:
            try:
                self.on_connected()
            except Exception:
                log.exception("gagal menjalankan on_connected")

    def _on_disconnect(self, client, userdata, *args):
        self._connected = False
        log.warning("MQTT terputus, mencoba menyambung ulang")

    def _on_message(self, client, userdata, msg):
        try:
            parts = msg.topic.split("/")
            # <prefix>/<slug>/device/<device_id>/state
            if len(parts) == 5 and parts[2] == "device" and parts[4] == "state":
                location = self._slug_to_location(parts[1])
                if location is None:
                    return
                data = json.loads(msg.payload.decode("utf-8") or "{}")
                if isinstance(data, dict):
                    runtime.set_device_state(location, parts[3], data)
        except Exception:
            log.exception("gagal memproses pesan MQTT %s", msg.topic)

    # -------------------------------------------------------------- publish
    def publish(self, slug: str, suffix: str, payload: dict, retain: bool = False) -> bool:
        """Kembalikan True kalau pesan diserahkan ke broker."""
        if not self.connected or self._client is None:
            return False
        topic = runtime.topic(self.prefix, slug, suffix)
        try:
            info = self._client.publish(topic, json.dumps(payload, ensure_ascii=False), qos=1, retain=retain)
            return info.rc == 0
        except Exception:
            log.exception("gagal publish ke %s", topic)
            return False

    def publish_status(self, slug: str, payload: dict) -> bool:
        return self.publish(slug, "status", payload, retain=True)

    def publish_config(self, slug: str, payload: dict) -> bool:
        return self.publish(slug, "config", payload, retain=True)

    def publish_cmd(self, slug: str, payload: dict) -> bool:
        return self.publish(slug, "cmd", payload, retain=False)
