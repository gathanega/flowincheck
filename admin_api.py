"""
Endpoint tambahan untuk dashboard Flowincheck (satu file HTML).

Publik (hanya baca):
  GET  /api/public-config                  alamat MQTT WebSocket (read-only) + daftar opsi firmware
  GET  /api/locations                      daftar lokasi + koordinat + ROI + program/status terakhir
  GET  /api/logs?location=&limit=          riwayat deteksi (dari Supabase)
  GET  /api/hourly?location=&range=        rata-rata kendaraan per jam (today|yesterday|7days)
  GET  /api/frame?location=                frame JPEG terbaru (untuk pantauan & editor ROI)
  GET  /api/roi?location=                  polygon ROI tersimpan
  GET  /api/devices                        status perangkat (online, baterai, RSSI, versi)
  GET  /api/device/config?location=&role=  konfigurasi firmware (fallback HTTP kalau MQTT mati)

Admin (header  Authorization: Bearer <admin_token>):
  POST /api/admin/login
  GET  /api/admin/settings?location=
  PUT  /api/admin/settings   {location, section, data}   section: location|roi|camera|light|program|thresholds
  POST /api/admin/command    {location, target, cmd}     cmd: capture_now|reboot|resend_config
"""

from __future__ import annotations

import asyncio
import hmac
import re
import time
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request, Response

import runtime


def _parse_ts(value: str) -> datetime:
    """Supabase kadang mengirim pecahan detik 5 digit; Python < 3.11 menolaknya."""
    s = value.replace("Z", "+00:00")
    s = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], s, count=1)
    return datetime.fromisoformat(s)


def create_router(ctx) -> APIRouter:
    """ctx: objek dengan CONFIG, supabase, fetch_cctv_frame, LATEST_PROGRAMS, LATEST_FRAMES, bus."""
    router = APIRouter()
    cfg_all = lambda: ctx.CONFIG.setdefault("locations", {})   # noqa: E731

    # ------------------------------------------------------------- helpers
    def slug_of(loc: str) -> str:
        return (cfg_all().get(loc) or {}).get("slug") or runtime.slugify(loc)

    def loc_or_404(loc: str) -> str:
        if not loc or loc not in cfg_all():
            raise HTTPException(404, f"lokasi '{loc}' tidak ada di config.yaml")
        return loc

    def require_admin(request: Request) -> None:
        token = str(ctx.CONFIG.get("admin_token") or "").strip()
        if not token:
            raise HTTPException(503, "admin_token belum diisi di config.yaml, fitur admin dimatikan")
        header = request.headers.get("authorization", "")
        given = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if not hmac.compare_digest(given.encode(), token.encode()):
            raise HTTPException(401, "Token admin salah")

    def tz() -> ZoneInfo:
        return ZoneInfo(ctx.CONFIG.get("timezone", "Asia/Jakarta"))

    def full_settings(loc: str) -> dict:
        s = runtime.get_location(loc)
        cfg = cfg_all()[loc] or {}
        slug = slug_of(loc)
        prefix = ctx.bus.prefix
        return {
            **s,
            "location": loc,
            "slug": slug,
            "mode": cfg.get("mode", "esp32"),
            "has_roi": bool(s["roi"]),
            "topics": {
                "status": runtime.topic(prefix, slug, "status"),
                "config": runtime.topic(prefix, slug, "config"),
                "cmd": runtime.topic(prefix, slug, "cmd"),
                "state": runtime.topic(prefix, slug, "device", "<device_id>", "state"),
            },
            "mqtt_connected": ctx.bus.connected,
        }

    def publish_config(loc: str) -> bool:
        return ctx.bus.publish_config(slug_of(loc), runtime.config_payload(loc))

    def _int_or_none(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------- publik
    @router.get("/api/public-config")
    async def public_config():
        """Info yang aman dibaca browser: alamat MQTT over WebSocket (user read-only), prefix topik."""
        m = ctx.CONFIG.get("mqtt") or {}
        ws_url = str(m.get("ws_url") or "").strip()
        return {
            "mqtt": {
                "enabled": bool(m.get("enabled") and ws_url),
                "ws_url": ws_url,
                "prefix": ctx.bus.prefix,
                "username": m.get("ws_username", ""),
                "password": m.get("ws_password", ""),
            },
            "server_mqtt_connected": ctx.bus.connected,
            "admin_enabled": bool(str(ctx.CONFIG.get("admin_token") or "").strip()),
            "timezone": ctx.CONFIG.get("timezone", "Asia/Jakarta"),
            "framesizes": runtime.FRAMESIZES,
            "offline_modes": runtime.OFFLINE_MODES,
        }

    @router.get("/api/locations")
    async def locations():
        out = []
        for loc, cfg in cfg_all().items():
            cfg = cfg or {}
            s = runtime.get_location(loc)
            latest = ctx.LATEST_PROGRAMS.get(loc) or {}
            out.append({
                "location": loc,
                "slug": slug_of(loc),
                "mode": cfg.get("mode", "esp32"),
                "device_id": cfg.get("device_id"),
                "label": s["label"] or loc,
                "lat": s["lat"],
                "lng": s["lng"],
                "has_roi": bool(s["roi"]),
                "roi": s["roi"],
                "roi_label": s["roi_label"],
                "stream_url": cfg.get("cctv_snapshot_url") if cfg.get("mode") == "cctv" else None,
                "stream_type": cfg.get("cctv_stream_type", "snapshot") if cfg.get("mode") == "cctv" else None,
                "program": s["program"],
                "thresholds": s["thresholds"],
                "config_version": s["config_version"],
                "traffic_status": latest.get("traffic_status"),
                "vehicle_count": latest.get("vehicle_count"),
                "all_lanes_total": latest.get("all_lanes_total"),
                "counts_by_class": latest.get("counts_by_class"),
                "red_seconds": latest.get("red_seconds"),
                "yellow_seconds": latest.get("yellow_seconds"),
                "green_seconds": latest.get("green_seconds"),
                "updated_at": latest.get("updated_at"),
            })
        return {"locations": out, "mqtt_connected": ctx.bus.connected}

    @router.get("/api/logs")
    async def logs(request: Request):
        q = request.query_params
        try:
            limit = max(1, min(int(q.get("limit", 20)), 200))
        except ValueError:
            raise HTTPException(400, "limit harus angka")
        location = q.get("location") or ""

        def run():
            query = ctx.supabase.table("greenflow_logs").select("*").order("created_at", desc=True).limit(limit)
            if location:
                query = query.eq("location", location)
            return query.execute().data or []

        try:
            return {"rows": await asyncio.to_thread(run)}
        except Exception as e:
            raise HTTPException(502, f"gagal membaca Supabase: {e}")

    @router.get("/api/hourly")
    async def hourly(request: Request):
        q = request.query_params
        rng = q.get("range", "today")
        if rng not in ("today", "yesterday", "7days"):
            rng = "today"
        location = q.get("location") or ""

        zone = tz()
        today = datetime.now(zone).replace(hour=0, minute=0, second=0, microsecond=0)
        if rng == "yesterday":
            start, end = today - timedelta(days=1), today
        elif rng == "7days":
            start, end = today - timedelta(days=6), today + timedelta(days=1)
        else:
            start, end = today, today + timedelta(days=1)

        def run():
            rows, offset, page = [], 0, 1000      # PostgREST membatasi 1000 baris per permintaan
            while offset < 50000:
                query = (ctx.supabase.table("greenflow_logs")
                         .select("created_at,vehicle_count")
                         .gte("created_at", start.astimezone(ZoneInfo("UTC")).isoformat())
                         .lt("created_at", end.astimezone(ZoneInfo("UTC")).isoformat())
                         .order("created_at")
                         .range(offset, offset + page - 1))
                if location:
                    query = query.eq("location", location)
                data = query.execute().data or []
                rows.extend(data)
                if len(data) < page:
                    break
                offset += page
            return rows

        try:
            rows = await asyncio.to_thread(run)
        except Exception as e:
            raise HTTPException(502, f"gagal membaca Supabase: {e}")

        total, count = [0] * 24, [0] * 24
        for r in rows:
            h = _parse_ts(r["created_at"]).astimezone(zone).hour
            total[h] += int(r.get("vehicle_count") or 0)
            count[h] += 1
        values = [round(total[h] / count[h], 1) if count[h] else 0 for h in range(24)]
        peak = max(values)
        return {
            "range": rng,
            "labels": [f"{h:02d}" for h in range(24)],
            "values": values,
            "peak_hour": values.index(peak) if peak > 0 else None,
            "peak_value": peak,
            "samples": len(rows),
        }

    @router.get("/api/frame")
    async def frame(request: Request):
        loc = loc_or_404(request.query_params.get("location", ""))
        cached = ctx.LATEST_FRAMES.get(loc)
        if cached and time.time() - cached[1] < 120:
            return Response(content=cached[0], media_type="image/jpeg", headers={"Cache-Control": "no-store"})
        cfg = cfg_all()[loc] or {}
        if cfg.get("mode") == "cctv":
            try:
                jpeg = await asyncio.to_thread(ctx.fetch_cctv_frame, cfg)
            except Exception as e:
                if cached:
                    return Response(content=cached[0], media_type="image/jpeg", headers={"Cache-Control": "no-store"})
                raise HTTPException(502, f"gagal ambil frame CCTV: {e}")
            ctx.LATEST_FRAMES[loc] = (jpeg, time.time())
            return Response(content=jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})
        if cached:
            return Response(content=cached[0], media_type="image/jpeg", headers={"Cache-Control": "no-store"})
        raise HTTPException(404, "belum ada frame untuk lokasi ini")

    @router.get("/api/roi")
    async def get_roi(request: Request):
        loc = loc_or_404(request.query_params.get("location", ""))
        s = runtime.get_location(loc)
        return {"roi": s["roi"], "label": s["roi_label"]}

    @router.get("/api/devices")
    async def devices():
        return {"devices": runtime.list_devices(), "mqtt_connected": ctx.bus.connected}

    @router.get("/api/device/config")
    async def device_config(request: Request):
        q = request.query_params
        loc = loc_or_404(q.get("location", ""))
        try:
            data = runtime.device_config(loc, q.get("role", ""))
        except runtime.ValidationError as e:
            raise HTTPException(400, str(e))
        device_id = q.get("device_id")
        if device_id:
            # firmware mengirim fw/cv/rssi supaya dashboard tetap tahu status sinkron walau MQTT mati
            runtime.touch_device(device_id, loc, q.get("role"), online=True,
                                 fw=(q.get("fw") or None),
                                 config_version=_int_or_none(q.get("cv")),
                                 rssi=_int_or_none(q.get("rssi")))
        return data

    # -------------------------------------------------------------- admin
    @router.post("/api/admin/login")
    async def login(request: Request):
        try:
            require_admin(request)
        except HTTPException as e:
            if e.status_code == 401:
                await asyncio.sleep(0.4)     # perlambat tebak-tebakan token
            raise
        return {"ok": True}

    @router.get("/api/admin/settings")
    async def get_settings(request: Request):
        require_admin(request)
        loc = loc_or_404(request.query_params.get("location", ""))
        return full_settings(loc)

    @router.put("/api/admin/settings")
    async def put_settings(request: Request):
        require_admin(request)
        body = await request.json()
        loc = loc_or_404(body.get("location", ""))
        section = body.get("section", "")
        try:
            runtime.update(loc, section, body.get("data") or {})
        except runtime.ValidationError as e:
            raise HTTPException(422, str(e))

        runtime.apply_to_config(ctx.CONFIG, only=loc)    # ROI/ambang langsung dipakai frame berikutnya
        published = False
        if section in runtime.DEVICE_SECTIONS:
            published = publish_config(loc)
        return {"ok": True, "published": published, "mqtt_connected": ctx.bus.connected,
                "settings": full_settings(loc)}

    @router.post("/api/admin/command")
    async def command(request: Request):
        require_admin(request)
        body = await request.json()
        loc = loc_or_404(body.get("location", ""))
        target, cmd = body.get("target", "all"), body.get("cmd", "")
        if target not in ("camera", "light", "all"):
            raise HTTPException(422, "target harus camera, light, atau all")
        if cmd not in ("capture_now", "reboot", "resend_config"):
            raise HTTPException(422, "cmd tidak dikenal")
        if not ctx.bus.connected:
            raise HTTPException(503, "MQTT tidak terhubung; perintah hanya bisa dikirim lewat MQTT")

        if cmd == "resend_config":
            runtime.bump_version(loc)
            ok = publish_config(loc)
        else:
            ok = ctx.bus.publish_cmd(slug_of(loc), {
                "id": uuid.uuid4().hex[:8], "target": target, "cmd": cmd, "ts": int(time.time()),
            })
        if not ok:
            raise HTTPException(502, "gagal mengirim ke broker MQTT")
        return {"ok": True, "version": runtime.get_location(loc)["config_version"]}

    return router
