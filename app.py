"""
GreenFlow VPS server (FastAPI).

Two ingestion modes, both feeding the same pipeline (detect -> classify ->
compute light program -> store -> serve):

  MODE A ("esp32"): the ESP32-S3-VROOM-1 camera node POSTs JPEG frames to
      POST /api/ingest/esp32/{device_id}?location=...

  MODE B ("cctv"): this server itself periodically pulls a snapshot from an
      existing online CCTV API/stream URL, for locations configured with
      mode: cctv in config.yaml. No ESP32-S3 needed for those locations.
      Three CCTV source types are supported (set with cctv_stream_type):
        - "snapshot" (default): the URL returns a plain JPEG directly.
        - "hls": the URL is a live HLS (.m3u8) stream.
        - "mjpeg": the URL is a live MJPEG/multipart stream (common for
          city-run CCTV proxies, e.g. Diskominfo-style "proxy.php" feeds).
      Both "hls" and "mjpeg" grab a single frame using ffmpeg, since
      requests.get() alone can't reliably pull one still frame out of a
      live multi-frame stream.

  LANE FILTERING (ROI): a wide CCTV frame often covers more than one lane
      or direction of traffic, but the light program should usually react
      to just the lane feeding that light. Each location in config.yaml
      can optionally set a `roi` — a polygon (list of [x, y] points, each
      0.0-1.0, normalized to frame width/height) marking the lane to
      count. See vision.detect_vehicles() for the matching logic, and
      GET /api/debug/roi-preview to visually calibrate the polygon against
      a live frame before committing to config.yaml.

Either mode ends up calling process_frame(), which writes one row to the
Supabase `greenflow_logs` table (used by the web dashboard) and also keeps
the latest program in memory per location, served to the ESP32-C6 traffic
light controllers via:

  GET /api/light/latest?location=...
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import shutil
import subprocess
import threading
import time
from typing import Optional

# systemd services can run with a more limited PATH than an interactive
# shell, so "ffmpeg" alone may not resolve even if `apt install ffmpeg`
# succeeded. Resolve the absolute path once at import time, with a
# fallback to the common Debian/Ubuntu install location.
FFMPEG_BIN = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"

import requests
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageDraw
from supabase import Client, create_client

import runtime
from admin_api import create_router
from mqtt_bus import MqttBus
from vision import compute_light_program, detect_vehicles, render_detections

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("greenflow")

app = FastAPI(title="GreenFlow VPS Server")

with open("config.yaml", "r") as f:
    CONFIG = yaml.safe_load(f)

supabase: Client = create_client(CONFIG["supabase"]["url"], CONFIG["supabase"]["service_key"])

# Pengaturan yang diubah lewat dashboard admin (ROI, ambang, program, firmware)
# disimpan di runtime_state.json dan ditimpakan di atas config.yaml.
runtime.load()
runtime.apply_to_config(CONFIG)

# Dashboard (satu file HTML) boleh dipasang di domain lain; token admin dikirim
# lewat header Authorization, bukan cookie, jadi allow_origins=["*"] aman.
app.add_middleware(
    CORSMiddleware,
    allow_origins=CONFIG.get("cors_origins", ["*"]),
    allow_methods=["*"],
    allow_headers=["*"],
)

# in-memory cache of the most recent light program per location, so the
# ESP32-C6 poll endpoint is instant and doesn't hit Supabase every request
LATEST_PROGRAMS: dict[str, dict] = {}
# frame JPEG terakhir per lokasi: (bytes, epoch) -> dipakai /api/frame (pantauan + editor ROI)
LATEST_FRAMES: dict[str, tuple[bytes, float]] = {}
# hasil deteksi terakhir per lokasi (kotak + frame yang dideteksi) -> /api/detections
LATEST_DETECTIONS: dict[str, dict] = {}


def _slug_to_location(slug: str):
    for name, cfg in CONFIG.get("locations", {}).items():
        if ((cfg or {}).get("slug") or runtime.slugify(name)) == slug:
            return name
    return None


bus = MqttBus(CONFIG.get("mqtt"), _slug_to_location)


def _slug(location: str) -> str:
    return (CONFIG.get("locations", {}).get(location) or {}).get("slug") or runtime.slugify(location)


def _republish_configs():
    """Konfigurasi yang diubah saat MQTT putus tetap sampai ke perangkat setelah tersambung lagi."""
    for loc in list(CONFIG.get("locations", {})):
        bus.publish_config(_slug(loc), runtime.config_payload(loc))


bus.on_connected = _republish_configs

# YOLO tidak thread-safe; process_frame dijalankan di thread supaya event loop tidak macet
_DETECT_LOCK = threading.Lock()


def location_calibration(location: str) -> dict:
    """Per-location thresholds from config.yaml, with sane fallbacks."""
    locations = CONFIG.get("locations", {})
    return locations.get(location, {}) or {}


def process_frame(jpeg_bytes: bytes, location: str, device_id: str, battery_level: Optional[int] = None):
    calib = location_calibration(location)
    calibration_max_count = calib.get("calibration_max_count", 20)
    density_low = calib.get("density_low", 5)
    density_high = calib.get("density_high", 15)
    roi_polygon = calib.get("roi")  # optional: [[x,y], ...] normalized 0-1, one lane

    with _DETECT_LOCK:
        detection = detect_vehicles(jpeg_bytes, roi_polygon=roi_polygon)
    custom = calib.get("program") or {}
    program = compute_light_program(
        vehicle_count=detection.vehicle_count,
        calibration_max_count=calibration_max_count,
        density_low=density_low,
        density_high=density_high,
        baseline=custom.get("baseline"),
        congested=custom.get("congested"),
    )
    LATEST_FRAMES[location] = (jpeg_bytes, time.time())
    LATEST_DETECTIONS[location] = {
        "ts": time.time(),
        "device_id": device_id,
        "width": detection.width,
        "height": detection.height,
        "roi": roi_polygon,
        "roi_label": calib.get("roi_label", "") if roi_polygon else "",
        "vehicle_count": detection.vehicle_count,
        "all_lanes_total": detection.total_vehicle_count,
        "counts_by_class": detection.counts_by_class,
        "total_counts_by_class": detection.total_counts_by_class,
        "traffic_status": program.traffic_status,
        "boxes": detection.boxes,
        "jpeg": jpeg_bytes,   # frame persis yang dideteksi, supaya kotak selalu pas
    }

    # counts_by_class is jsonb, so we can pack the lane-vs-whole-frame
    # comparison in there without a schema migration. Only added when this
    # location actually has a `roi` configured, to keep old rows/locations
    # unchanged.
    counts_payload = dict(detection.counts_by_class)
    if roi_polygon:
        counts_payload["_lane_label"] = calib.get("roi_label", "jalur kanan")
        counts_payload["_all_lanes_total"] = detection.total_vehicle_count
        counts_payload["_all_lanes_by_class"] = detection.total_counts_by_class

    row = {
        "device_id": device_id,
        "location": location,
        "vehicle_count": detection.vehicle_count,   # = lane count when roi is set, else whole-frame count
        "counts_by_class": counts_payload,
        "light_status": "HIJAU",  # status at the moment of capture; dashboard shows history, not live phase
        "green_duration": program.green_seconds,
        "battery_level": battery_level if battery_level is not None else 100,
        "traffic_status": program.traffic_status,
    }

    try:
        supabase.table("greenflow_logs").insert(row).execute()
    except Exception as e:
        log.error("failed to write to Supabase: %s", e)

    LATEST_PROGRAMS[location] = {
        "red_seconds": program.red_seconds,
        "yellow_seconds": program.yellow_seconds,
        "green_seconds": program.green_seconds,
        "traffic_status": program.traffic_status,
        "vehicle_count": detection.vehicle_count,
        "all_lanes_total": detection.total_vehicle_count,
        "counts_by_class": detection.counts_by_class,
        "updated_at": time.time(),
    }

    runtime.touch_device(device_id, location, "camera", online=True, battery=row["battery_level"])
    bus.publish_status(_slug(location), {
        "location": location,
        "device_id": device_id,
        "ts": int(time.time()),
        "vehicle_count": detection.vehicle_count,
        "all_lanes_total": detection.total_vehicle_count,
        "traffic_status": program.traffic_status,
        "red_seconds": program.red_seconds,
        "yellow_seconds": program.yellow_seconds,
        "green_seconds": program.green_seconds,
        # versi ringkas (tanpa _all_lanes_by_class) supaya muat di buffer JSON ESP32-C6
        "counts_by_class": detection.counts_by_class,
        "roi_used": bool(roi_polygon),
    })

    log.info(
        "[%s] %s vehicles=%d status=%s -> R%d/Y%d/G%d",
        location, device_id, detection.vehicle_count, program.traffic_status,
        program.red_seconds, program.yellow_seconds, program.green_seconds,
    )
    return row, program


# ------------------------- MODE A: ESP32-S3 push ---------------------------

@app.post("/api/ingest/esp32/{device_id}")
async def ingest_esp32(device_id: str, request: Request, location: str = "default"):
    jpeg_bytes = await request.body()
    if not jpeg_bytes:
        raise HTTPException(400, "empty body, expected raw JPEG bytes")

    row, program = await asyncio.to_thread(process_frame, jpeg_bytes, location, device_id)
    return JSONResponse({
        "ok": True,
        "vehicle_count": row["vehicle_count"],
        "traffic_status": program.traffic_status,
        "red_seconds": program.red_seconds,
        "yellow_seconds": program.yellow_seconds,
        "green_seconds": program.green_seconds,
    })


# ------------------------- MODE B: online CCTV pull -------------------------

def grab_frame_ffmpeg(stream_url: str, timeout: int = 20) -> bytes:
    """Extract a single JPEG frame from a live stream (HLS .m3u8, RTSP, etc.)
    using ffmpeg. Needed for CCTV providers that only expose a live stream
    rather than a plain still-image snapshot endpoint."""
    if not shutil.which(FFMPEG_BIN) and not (FFMPEG_BIN.startswith("/") and __import__("os").path.exists(FFMPEG_BIN)):
        raise RuntimeError(
            f"ffmpeg binary not found at '{FFMPEG_BIN}'. "
            "Install it with: apt install -y ffmpeg"
        )

    cmd = [
        FFMPEG_BIN,
        "-y",
        "-loglevel", "error",
        "-i", stream_url,
        "-frames:v", "1",
        "-q:v", "2",
        "-f", "image2pipe",
        "-vcodec", "mjpeg",
        "-",
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if result.returncode != 0 or not result.stdout:
        stderr = result.stderr.decode(errors="ignore")[:300]
        raise RuntimeError(f"ffmpeg failed to grab frame: {stderr}")
    return result.stdout


def fetch_cctv_frame(cfg: dict) -> bytes:
    """Fetch one frame for a mode: cctv location, using whichever method
    matches cctv_stream_type:
      - "hls" or "mjpeg": grabbed via ffmpeg (handles live multi-frame
        streams, including MJPEG/multipart proxies like many city
        Diskominfo CCTV feeds).
      - anything else (default "snapshot"): plain GET, URL returns one
        JPEG directly."""
    url = cfg["cctv_snapshot_url"]
    if cfg.get("cctv_stream_type") in ("hls", "mjpeg"):
        return grab_frame_ffmpeg(url, timeout=cfg.get("cctv_ffmpeg_timeout", 20))

    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    return resp.content


async def cctv_poll_loop():
    """Background task: for every location configured with mode: cctv,
    fetch a fresh snapshot on its own interval and run it through the same
    pipeline as the ESP32-S3 push path."""
    while True:
        for location, cfg in CONFIG.get("locations", {}).items():
            if cfg.get("mode") != "cctv":
                continue
            try:
                jpeg_bytes = await asyncio.to_thread(fetch_cctv_frame, cfg)
                await asyncio.to_thread(process_frame, jpeg_bytes, location, cfg.get("device_id", location))
            except Exception as e:
                log.error("[%s] CCTV pull failed: %s", location, e)
        await asyncio.sleep(CONFIG.get("cctv_poll_interval_seconds", 10))


@app.on_event("startup")
async def start_background_tasks():
    bus.start()
    if any(c.get("mode") == "cctv" for c in CONFIG.get("locations", {}).values()):
        asyncio.create_task(cctv_poll_loop())


# ------------------------- served to the ESP32-C6 ---------------------------

@app.get("/api/light/latest")
async def light_latest(location: str = "default"):
    # id harus sama dengan firmware ESP32-C6: "light-" + LOCATION_SLUG
    runtime.touch_device(f"light-{_slug(location)}", location, "light", online=True)
    program = LATEST_PROGRAMS.get(location)
    if program is None:
        # Belum ada frame yang diproses: pakai program fallback lokasi ini
        # (sama dengan yang dijalankan firmware saat offline, bisa diatur di tab Admin > Lampu).
        light = runtime.get_location(location)["light"]
        return {
            "red_seconds": light["fallback_red"],
            "yellow_seconds": light["fallback_yellow"],
            "green_seconds": light["fallback_green"],
            "traffic_status": "LANCAR",
            "vehicle_count": 0,
            "note": "fallback, no data processed yet for this location",
        }
    return program


@app.get("/api/health")
async def health():
    return {"ok": True, "locations_tracked": list(LATEST_PROGRAMS.keys()), "mqtt": bus.connected}


# ------------------------- served to the dashboard --------------------------

@app.get("/api/cameras")
async def list_cameras():
    """List CCTV-mode locations from config.yaml, so the web dashboard can
    render a live-view panel without needing access to config.yaml itself.
    Only the stream URL is exposed (already a public camera feed, not a
    secret) — never the Supabase keys or other server-only config."""
    cameras = []
    for location, cfg in CONFIG.get("locations", {}).items():
        if cfg.get("mode") == "cctv":
            cameras.append({
                "location": location,
                "stream_url": cfg.get("cctv_snapshot_url"),
                "stream_type": cfg.get("cctv_stream_type", "snapshot"),
                "has_roi": bool(cfg.get("roi")),
            })
    return {"cameras": cameras}


# ------------------------- ROI (lane) calibration ---------------------------

@app.get("/api/debug/roi-preview")
async def roi_preview(location: str):
    """Grab one live frame for `location` (must be mode: cctv) and draw the
    configured `roi` polygon on top of it, so the normalized [x, y]
    coordinates in config.yaml can be tuned by eye instead of guesswork.

    Open this URL directly in a browser:
        http://<server>/api/debug/roi-preview?location=Nama%20Lokasi

    If `roi` isn't set yet for this location, the raw frame is returned
    unmodified so you can still see what you're calibrating against.
    """
    cfg = location_calibration(location)
    if not cfg or cfg.get("mode") != "cctv":
        raise HTTPException(400, f"location '{location}' tidak ditemukan atau bukan mode: cctv")

    try:
        jpeg_bytes = await asyncio.to_thread(fetch_cctv_frame, cfg)
    except Exception as e:
        raise HTTPException(502, f"gagal ambil frame CCTV: {e}")

    roi = cfg.get("roi")
    if not roi:
        return Response(content=jpeg_bytes, media_type="image/jpeg")

    image = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
    width, height = image.size

    draw = ImageDraw.Draw(image, "RGBA")
    pixel_points = [(x * width, y * height) for x, y in roi]
    draw.polygon(pixel_points, outline=(255, 0, 0, 255), width=4, fill=(255, 0, 0, 60))
    label = cfg.get("roi_label", "ROI")
    label_x, label_y = pixel_points[0]
    draw.text((label_x + 8, label_y + 8), label, fill=(255, 255, 255, 255))

    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=85)
    return Response(content=buf.getvalue(), media_type="image/jpeg")


# ------------------- dashboard Flowincheck: data, ROI, admin, firmware ------------------
# Endpoint ada di admin_api.py. ctx = objek sederhana berisi state yang dibagi bersama.
from types import SimpleNamespace  # noqa: E402

app.include_router(create_router(SimpleNamespace(
    CONFIG=CONFIG,
    supabase=supabase,
    fetch_cctv_frame=fetch_cctv_frame,
    LATEST_PROGRAMS=LATEST_PROGRAMS,
    LATEST_FRAMES=LATEST_FRAMES,
    LATEST_DETECTIONS=LATEST_DETECTIONS,
    render_detections=render_detections,
    bus=bus,
)))


# ------------------------- dashboard satu file (opsional) -------------------
# Taruh dashboard.html (dan folder pictures/) di samping app.py, lalu buka http://<server>/
DASHBOARD_FILE = CONFIG.get("dashboard_file", "dashboard.html")
if os.path.isdir("pictures"):
    app.mount("/pictures", StaticFiles(directory="pictures"), name="pictures")


@app.get("/", include_in_schema=False)
async def dashboard():
    if not os.path.isfile(DASHBOARD_FILE):
        raise HTTPException(404, f"{DASHBOARD_FILE} tidak ditemukan di samping app.py")
    return FileResponse(DASHBOARD_FILE, media_type="text/html", headers={"Cache-Control": "no-cache"})
