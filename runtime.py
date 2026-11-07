"""
Pengaturan runtime yang bisa diubah dari dashboard admin (tanpa edit config.yaml).

Disimpan di runtime_state.json (override di atas config.yaml) dan dipakai oleh:
  - app.py        -> ROI, ambang kepadatan, program lampu, koordinat
  - MQTT / HTTP   -> konfigurasi firmware (kamera ESP32-S3, lampu ESP32-C6)

Modul ini sengaja murni Python (tanpa FastAPI/MQTT) supaya mudah diuji.
"""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import threading
import time
from typing import Any, Optional

STATE_FILE = os.environ.get("GF_RUNTIME_FILE", "runtime_state.json")
_LOCK = threading.RLock()
_STATE: dict = {"locations": {}}
# CONFIG["locations"] dari config.yaml: nilai awal untuk lokasi yang belum diubah dari dashboard
_BASE: dict = {}
DEVICES: dict[str, dict] = {}

FRAMESIZES = ["QQVGA", "QVGA", "VGA", "SVGA", "XGA", "SXGA", "UXGA"]
OFFLINE_MODES = ["fallback", "blink_yellow", "all_red"]

DEFAULTS: dict[str, dict] = {
    "camera": {
        "framesize": "SVGA",
        "jpeg_quality": 12,        # 4 (terbaik) .. 63 (paling kecil)
        "capture_interval_s": 5,
        "vflip": True,
        "hmirror": False,
        "brightness": 1,
        "contrast": 0,
        "saturation": 0,
    },
    "light": {
        "poll_interval_s": 15,
        "fallback_red": 180,
        "fallback_yellow": 30,
        "fallback_green": 420,
        "offline_mode": "fallback",   # perilaku lampu kalau server lama tak terjangkau
        "offline_after_s": 120,
    },
    "program": {
        "baseline": {"red": 300, "yellow": 30, "green": 60},
        "congested": {"red": 120, "yellow": 60, "green": 180},
    },
    "thresholds": {"density_low": 5, "density_high": 15, "calibration_max_count": 20},
}

# Hanya section ini yang mengubah konfigurasi yang dibaca perangkat
DEVICE_SECTIONS = {"camera", "light"}


class ValidationError(ValueError):
    pass


# ----------------------------------------------------------------- util ----

def slugify(text: str) -> str:
    """Harus sama dengan LOCATION_SLUG di firmware (lihat README)."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return slug or "default"


def topic(prefix: str, slug: str, *parts: str) -> str:
    return "/".join([prefix.strip("/"), slug, *parts])


def _num(value: Any, lo: float, hi: float, name: str, integer: bool = True):
    try:
        n = float(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{name} harus berupa angka")
    if integer:
        n = int(round(n))
    if not lo <= n <= hi:
        raise ValidationError(f"{name} harus antara {lo:g} dan {hi:g}")
    return n


def _bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "ya", "on")
    return bool(value)


# ------------------------------------------------------------ validators ----

def clean_camera(data: dict, current: dict) -> dict:
    d = {**current, **{k: v for k, v in data.items() if k in DEFAULTS["camera"]}}
    if d["framesize"] not in FRAMESIZES:
        raise ValidationError("framesize tidak dikenal")
    return {
        "framesize": d["framesize"],
        "jpeg_quality": _num(d["jpeg_quality"], 4, 63, "jpeg_quality"),
        "capture_interval_s": _num(d["capture_interval_s"], 1, 600, "capture_interval_s"),
        "vflip": _bool(d["vflip"]),
        "hmirror": _bool(d["hmirror"]),
        "brightness": _num(d["brightness"], -2, 2, "brightness"),
        "contrast": _num(d["contrast"], -2, 2, "contrast"),
        "saturation": _num(d["saturation"], -2, 2, "saturation"),
    }


def clean_light(data: dict, current: dict) -> dict:
    d = {**current, **{k: v for k, v in data.items() if k in DEFAULTS["light"]}}
    if d["offline_mode"] not in OFFLINE_MODES:
        raise ValidationError("offline_mode tidak dikenal")
    return {
        "poll_interval_s": _num(d["poll_interval_s"], 5, 300, "poll_interval_s"),
        "fallback_red": _num(d["fallback_red"], 1, 3600, "fallback_red"),
        "fallback_yellow": _num(d["fallback_yellow"], 3, 600, "fallback_yellow (minimal 3 detik)"),
        "fallback_green": _num(d["fallback_green"], 1, 3600, "fallback_green"),
        "offline_mode": d["offline_mode"],
        "offline_after_s": _num(d["offline_after_s"], 10, 3600, "offline_after_s"),
    }


def _phase(data: dict, name: str) -> dict:
    return {
        "red": _num(data.get("red"), 1, 3600, f"{name}: merah"),
        "yellow": _num(data.get("yellow"), 3, 600, f"{name}: kuning (minimal 3 detik)"),
        "green": _num(data.get("green"), 1, 3600, f"{name}: hijau"),
    }


def clean_program(data: dict, current: dict) -> dict:
    out = {}
    for key, label in (("baseline", "program normal"), ("congested", "program padat")):
        out[key] = _phase({**current[key], **(data.get(key) or {})}, label)
    return out


def clean_thresholds(data: dict, current: dict) -> dict:
    d = {**current, **{k: v for k, v in data.items() if k in DEFAULTS["thresholds"]}}
    low = _num(d["density_low"], 1, 500, "density_low")
    high = _num(d["density_high"], 1, 1000, "density_high")
    if high <= low:
        raise ValidationError("density_high harus lebih besar dari density_low")
    return {
        "density_low": low,
        "density_high": high,
        "calibration_max_count": _num(d["calibration_max_count"], 1, 1000, "calibration_max_count"),
    }


def clean_location(data: dict) -> dict:
    out: dict = {}
    if "label" in data:
        out["label"] = str(data["label"] or "").strip()[:80]
    if "lat" in data or "lng" in data:
        lat, lng = data.get("lat"), data.get("lng")
        if lat in (None, "") and lng in (None, ""):
            out["lat"] = out["lng"] = None            # hapus titik koordinat
        elif lat in (None, "") or lng in (None, ""):
            raise ValidationError("lat dan lng harus diisi bersamaan")
        else:
            out["lat"] = round(_num(lat, -90, 90, "lat", integer=False), 6)
            out["lng"] = round(_num(lng, -180, 180, "lng", integer=False), 6)
    return out


def _area(points: list) -> float:
    s = 0.0
    for i, (x1, y1) in enumerate(points):
        x2, y2 = points[(i + 1) % len(points)]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2


def clean_roi(data: dict) -> dict:
    pts = data.get("roi")
    out = {"roi_label": str(data.get("roi_label", "") or "").strip()[:80]}
    if pts in (None, [], ""):
        out["roi"] = None          # ROI dimatikan -> hitung seluruh frame
        return out
    if not isinstance(pts, list) or not 3 <= len(pts) <= 40:
        raise ValidationError("ROI butuh 3 sampai 40 titik")
    clean = []
    for p in pts:
        if not (isinstance(p, (list, tuple)) and len(p) == 2):
            raise ValidationError("setiap titik ROI harus [x, y]")
        clean.append([round(_num(p[0], 0, 1, "x ROI", integer=False), 4),
                      round(_num(p[1], 0, 1, "y ROI", integer=False), 4)])
    if _area(clean) < 0.002:
        raise ValidationError("area ROI terlalu kecil atau titiknya segaris")
    out["roi"] = clean
    return out


# ----------------------------------------------------------------- store ----

def load(path: Optional[str] = None) -> None:
    global _STATE
    path = path or STATE_FILE
    with _LOCK:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            _STATE = data if isinstance(data.get("locations"), dict) else {"locations": {}}
        except FileNotFoundError:
            _STATE = {"locations": {}}


def _save(path: Optional[str] = None) -> None:
    path = path or STATE_FILE
    folder = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".runtime_", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(_STATE, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)   # atomik: file tidak pernah setengah tertulis


def get_location(loc: str) -> dict:
    """Pengaturan satu lokasi, dengan nilai default untuk yang belum diubah."""
    with _LOCK:
        saved = _STATE["locations"].get(loc, {})
        base = _BASE.get(loc) or {}

        def pick(key, default=None):
            # kunci yang ada di runtime_state (termasuk None = sengaja dihapus) menang atas config.yaml
            return copy.deepcopy(saved[key] if key in saved else base.get(key, default))

        out = {
            "lat": pick("lat"),
            "lng": pick("lng"),
            "label": pick("label", "") or "",
            "roi": pick("roi"),
            "roi_label": pick("roi_label", "") or "",
            "config_version": saved.get("config_version", 1),
        }
        for section in ("camera", "light"):
            merged = copy.deepcopy(DEFAULTS[section])
            merged.update(saved.get(section, {}))
            out[section] = merged
        thresholds = copy.deepcopy(DEFAULTS["thresholds"])
        thresholds.update({k: base[k] for k in DEFAULTS["thresholds"] if k in base})
        thresholds.update(saved.get("thresholds", {}))
        out["thresholds"] = thresholds
        program = copy.deepcopy(DEFAULTS["program"])
        for k in program:
            program[k].update((base.get("program") or {}).get(k) or {})
            program[k].update(saved.get("program", {}).get(k, {}))
        out["program"] = program
        return out


def config_payload(loc: str) -> dict:
    """Isi topik <prefix>/<slug>/config (retained)."""
    s = get_location(loc)
    return {"version": s["config_version"], "camera": s["camera"], "light": s["light"]}


def update(loc: str, section: str, data: dict, path: Optional[str] = None) -> dict:
    """Validasi lalu simpan satu section. Melempar ValidationError kalau salah."""
    if not isinstance(data, dict):
        raise ValidationError("data harus berupa objek")
    with _LOCK:
        current = get_location(loc)
        new_entry = copy.deepcopy(_STATE["locations"].get(loc, {}))
        if section == "camera":
            new_entry["camera"] = clean_camera(data, current["camera"])
        elif section == "light":
            new_entry["light"] = clean_light(data, current["light"])
        elif section == "program":
            new_entry["program"] = clean_program(data, current["program"])
        elif section == "thresholds":
            new_entry["thresholds"] = clean_thresholds(data, current["thresholds"])
        elif section == "location":
            new_entry.update(clean_location(data))
        elif section == "roi":
            new_entry.update(clean_roi(data))
        else:
            raise ValidationError(f"section '{section}' tidak dikenal")
        if section in DEVICE_SECTIONS:
            new_entry["config_version"] = current["config_version"] + 1
        _STATE["locations"][loc] = new_entry      # baru disimpan setelah semua validasi lolos
        _save(path)
        return get_location(loc)


def bump_version(loc: str, path: Optional[str] = None) -> int:
    """Dipakai perintah 'kirim ulang konfigurasi'."""
    with _LOCK:
        entry = _STATE["locations"].setdefault(loc, {})
        entry["config_version"] = get_location(loc)["config_version"] + 1
        _save(path)
        return entry["config_version"]


def apply_to_config(config: dict, only: Optional[str] = None) -> None:
    """Salin override ke CONFIG['locations'] supaya kode lama (app.py) membacanya."""
    global _BASE
    locations = config.setdefault("locations", {})
    _BASE = locations
    for loc in list(locations):
        if only and loc != only:
            continue
        saved = _STATE["locations"].get(loc)
        if not saved:
            continue
        cfg = locations[loc] = locations[loc] or {}
        s = get_location(loc)
        if "roi" in saved:
            if s["roi"] is None:
                cfg.pop("roi", None)
            else:
                cfg["roi"] = s["roi"]
            if s["roi_label"]:
                cfg["roi_label"] = s["roi_label"]
        if "thresholds" in saved:
            cfg.update(s["thresholds"])
        if "program" in saved:
            cfg["program"] = s["program"]
        for key in ("lat", "lng", "label"):
            if key in saved:
                cfg[key] = saved[key]


def device_config(loc: str, role: str) -> dict:
    if role not in ("camera", "light"):
        raise ValidationError("role harus camera atau light")
    s = get_location(loc)
    return {"version": s["config_version"], role: s[role]}


# --------------------------------------------------------------- devices ----

def touch_device(device_id: str, location: str, role: str, **fields) -> None:
    with _LOCK:
        d = DEVICES.setdefault(device_id, {"device_id": device_id})
        d.update({"location": location, "role": role, "last_seen": time.time()})
        d.update({k: v for k, v in fields.items() if v is not None})


def set_device_state(location: str, device_id: str, data: dict) -> None:
    """Dari MQTT: <prefix>/<slug>/device/<id>/state (termasuk pesan LWT {"online": false})."""
    role = data.get("role") or DEVICES.get(device_id, {}).get("role", "camera")
    keep = {k: data[k] for k in ("online", "battery", "rssi", "fw", "config_version", "uptime_s") if k in data}
    touch_device(device_id, location, role, **keep)


def list_devices(now: Optional[float] = None) -> list[dict]:
    now = now or time.time()
    out = []
    with _LOCK:
        for d in DEVICES.values():
            s = get_location(d["location"]) if d.get("location") else None
            if s:
                interval = s["camera"]["capture_interval_s"] if d.get("role") == "camera" else s["light"]["poll_interval_s"]
            else:
                interval = 15
            limit = max(45, interval * 3)
            seen = now - d.get("last_seen", 0)
            item = dict(d)
            item["online"] = bool(d.get("online", True)) and seen < limit
            item["seconds_since_seen"] = int(seen)
            out.append(item)
    return sorted(out, key=lambda x: (x.get("location", ""), x.get("role", "")))
