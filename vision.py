"""
GreenFlow vision + timing logic.

- detect_vehicles(): runs YOLOv8n over a JPEG frame, returns per-class
  vehicle counts (car/motorcycle/bus/truck) plus every box (class, conf,
  normalized xyxy, inside-ROI flag) so the dashboard can show/export them.
  Optionally filtered to a region of interest (ROI) polygon representing a single lane, so a wide
  CCTV frame covering multiple directions of traffic can be calibrated to
  count only the lane that actually feeds the traffic light being served
  (e.g. "jalur kanan" / right lane). See app.py's `roi` config field and
  GET /api/debug/roi-preview for calibrating the polygon.
- render_detections(): draws those boxes + ROI on the frame (JPEG output),
  served by GET /api/detections/image.

  conf/iou are tuned lower/higher than the ultralytics defaults (0.35/0.7)
  specifically for dense Indonesian motorcycle traffic, where several
  motorcycles riding side-by-side overlap heavily in the frame:
    - conf=0.25 (default was 0.35): catches partially-occluded motorcycles
      that would otherwise fall just below the confidence cutoff.
    - iou=0.85 (default was 0.7): NMS threshold - the ultralytics default
      of 0.7 tends to merge two closely-adjacent (but genuinely separate)
      motorcycles into a single detection. Raising it to 0.85 means only
      near-total overlaps get suppressed as duplicates, so tightly-packed
      but distinct motorcycles are more likely to be counted individually.

- classify_density(): turns a vehicle count into LANCAR / PADAT / MACET.
- compute_light_program(): turns that classification into a fixed
  red/yellow/green program (two-state, not interpolated):

    LANCAR            -> MERAH 05:00 / KUNING 00:30 / HIJAU 01:00 (baseline)
    PADAT atau MACET  -> MERAH 02:00 / KUNING 01:00 / HIJAU 03:00 (kondisi padat)

  Begitu kondisi kembali LANCAR, program otomatis kembali ke baseline.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Optional, Sequence

import numpy as np
from PIL import Image
from ultralytics import YOLO

# COCO class ids we treat as "vehicles"
VEHICLE_CLASSES = {
    2: "car",
    3: "motorcycle",
    5: "bus",
    7: "truck",
}

_model: YOLO | None = None


def get_model() -> YOLO:
    global _model
    if _model is None:
        # yolov8n.pt auto-downloads on first run (needs outbound internet
        # once). Swap for a local .pt path if the VPS has no internet egress.
        _model = YOLO("yolov8n.pt")
    return _model


def _point_in_polygon(x: float, y: float, polygon: Sequence[tuple[float, float]]) -> bool:
    """Standard ray-casting point-in-polygon test. polygon is a list of
    (x, y) points in the SAME units as the point (here: pixels)."""
    inside = False
    n = len(polygon)
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        intersects = ((yi > y) != (yj > y)) and (
            x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-9) + xi
        )
        if intersects:
            inside = not inside
        j = i
    return inside


@dataclass
class DetectionResult:
    vehicle_count: int
    counts_by_class: dict = field(default_factory=dict)
    # Always the WHOLE-FRAME totals, regardless of ROI filtering above, so
    # callers can show "jalur kanan: X / semua jalur: Y" style comparisons.
    total_vehicle_count: int = 0
    total_counts_by_class: dict = field(default_factory=dict)
    # Hasil per kotak (untuk overlay/unduhan di dashboard). Koordinat dinormalisasi 0-1:
    #   {"cls": "car", "conf": 0.93, "box": [x1, y1, x2, y2], "in_roi": True}
    boxes: list = field(default_factory=list)
    width: int = 0
    height: int = 0


def detect_vehicles(
    jpeg_bytes: bytes,
    conf: float = 0.25,
    iou: float = 0.85,
    roi_polygon: Optional[Sequence[tuple[float, float]]] = None,
) -> DetectionResult:
    """
    roi_polygon: optional list of (x, y) points in NORMALIZED coordinates
    (each 0.0-1.0, fraction of frame width/height) describing the lane to
    count. Using normalized coordinates means the same polygon config works
    regardless of the camera's actual resolution. A vehicle is counted as
    "inside" the lane if its bounding-box CENTER point falls inside the
    polygon. When roi_polygon is None, every detected vehicle is counted
    (original whole-frame behaviour).

    conf/iou: see module docstring for why these differ from ultralytics'
    own defaults (0.35 / 0.7) — tuned for dense, overlapping motorcycle
    traffic rather than generic/sparse traffic scenes.
    """
    image = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
    frame = np.array(image)
    height, width = frame.shape[:2]

    pixel_polygon = None
    if roi_polygon:
        pixel_polygon = [(px * width, py * height) for (px, py) in roi_polygon]

    model = get_model()
    results = model.predict(frame, conf=conf, iou=iou, verbose=False)

    counts_roi: dict[str, int] = {name: 0 for name in VEHICLE_CLASSES.values()}
    counts_all: dict[str, int] = {name: 0 for name in VEHICLE_CLASSES.values()}
    boxes: list[dict] = []

    for box in results[0].boxes:
        cls_id = int(box.cls[0])
        if cls_id not in VEHICLE_CLASSES:
            continue
        cls_name = VEHICLE_CLASSES[cls_id]
        counts_all[cls_name] += 1

        x1, y1, x2, y2 = box.xyxy[0].tolist()
        cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
        in_roi = pixel_polygon is None or _point_in_polygon(cx, cy, pixel_polygon)
        if in_roi:
            counts_roi[cls_name] += 1
        boxes.append({
            "cls": cls_name,
            "conf": round(float(box.conf[0]), 3),
            "box": [round(x1 / width, 4), round(y1 / height, 4), round(x2 / width, 4), round(y2 / height, 4)],
            "in_roi": in_roi,
        })

    total_all = sum(counts_all.values())
    total_roi = sum(counts_roi.values())
    used_roi = pixel_polygon is not None

    return DetectionResult(
        vehicle_count=total_roi if used_roi else total_all,
        counts_by_class=counts_roi if used_roi else counts_all,
        total_vehicle_count=total_all,
        total_counts_by_class=counts_all,
        boxes=boxes,
        width=width,
        height=height,
    )


# warna kotak per kelas (RGB), mirip tampilan umum YOLO
CLASS_COLORS = {
    "car": (0, 200, 255),
    "motorcycle": (255, 190, 0),
    "bus": (180, 120, 255),
    "truck": (255, 90, 160),
}


def _font(size: int):
    from PIL import ImageFont
    for name in ("DejaVuSans-Bold.ttf", "arialbd.ttf", "Arial Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    try:
        return ImageFont.load_default(size=size)   # Pillow >= 10.1
    except TypeError:
        return ImageFont.load_default()


def render_detections(
    jpeg_bytes: bytes,
    boxes: Sequence[dict],
    roi_polygon: Optional[Sequence[Sequence[float]]] = None,
    roi_label: str = "",
    show_outside: bool = True,
    show_roi: bool = True,
    quality: int = 88,
) -> bytes:
    """Gambar hasil deteksi di atas frame: kotak + 'kelas: conf' per kendaraan,
    poligon ROI, dan ringkasan 'Vehicles detected' di pojok kiri atas.
    Kendaraan di luar ROI digambar abu-abu tipis (atau disembunyikan)."""
    from PIL import ImageDraw

    image = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
    w, h = image.size
    draw = ImageDraw.Draw(image, "RGBA")
    lw = max(2, round(w / 400))
    font = _font(max(11, round(w / 70)))
    big = _font(max(13, round(w / 48)))

    if roi_polygon and show_roi:
        pts = [(x * w, y * h) for x, y in roi_polygon]
        draw.polygon(pts, fill=(34, 197, 94, 40))
        draw.line(pts + [pts[0]], fill=(34, 197, 94, 255), width=lw + 1)
        if roi_label:
            tx, ty = min(pts, key=lambda p: p[1])
            tb = draw.textbbox((0, 0), roi_label, font=font)
            tx = min(max(4, tx - tb[2] / 2), w - tb[2] - 10)
            ty = max(ty, tb[3] + 12)
            draw.rectangle([tx - 4, ty - tb[3] - 10, tx + tb[2] + 6, ty - 2], fill=(11, 59, 32, 220))
            draw.text((tx + 1, ty - tb[3] - 7), roi_label, fill=(255, 255, 255), font=font)

    inside = 0
    for b in boxes:
        x1, y1, x2, y2 = b["box"][0] * w, b["box"][1] * h, b["box"][2] * w, b["box"][3] * h
        if not b.get("in_roi", True):
            if show_outside:
                draw.rectangle([x1, y1, x2, y2], outline=(170, 170, 170, 200), width=max(1, lw - 1))
            continue
        inside += 1
        color = CLASS_COLORS.get(b["cls"], (0, 200, 255))
        draw.rectangle([x1, y1, x2, y2], outline=color + (255,), width=lw)
        text = f"{b['cls']}: {b['conf']:.2f}"
        tb = draw.textbbox((0, 0), text, font=font)
        ty = y1 - tb[3] - 4 if y1 - tb[3] - 4 > 0 else y1
        draw.rectangle([x1, ty, x1 + tb[2] + 6, ty + tb[3] + 4], fill=color + (230,))
        draw.text((x1 + 3, ty + 1), text, fill=(0, 0, 0), font=font)

    total = len(boxes)
    head = f"Vehicles detected: {inside}" + (f"  (semua jalur: {total})" if roi_polygon else "")
    tb = draw.textbbox((0, 0), head, font=big)
    draw.rectangle([6, 6, tb[2] + 18, tb[3] + 16], fill=(0, 0, 0, 150))
    draw.text((12, 10), head, fill=(60, 255, 120), font=big)

    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def classify_density(vehicle_count: int, low: int, high: int) -> str:
    """low/high thresholds are per-location calibration (see config.yaml)."""
    if vehicle_count < low:
        return "LANCAR"
    if vehicle_count < high:
        return "PADAT"
    return "MACET"


@dataclass
class LightProgram:
    red_seconds: int
    yellow_seconds: int
    green_seconds: int
    traffic_status: str


# Dua kondisi tetap (bukan interpolasi linear seperti versi sebelumnya).
BASELINE_PROGRAM = {"red": 300, "yellow": 30, "green": 60}    # 05:00 / 00:30 / 01:00
CONGESTED_PROGRAM = {"red": 120, "yellow": 60, "green": 180}  # 02:00 / 01:00 / 03:00


def compute_light_program(
    vehicle_count: int,
    calibration_max_count: int = 20,  # dipertahankan untuk kompatibilitas signature, tidak dipakai di logika baru
    density_low: int = 5,
    density_high: int = 15,
    baseline: Optional[dict] = None,    # {"red":..,"yellow":..,"green":..} dari dashboard admin
    congested: Optional[dict] = None,
) -> LightProgram:
    """
    Menentukan program lampu berdasarkan status kepadatan saja (dua
    kondisi tetap), bukan skor kontinu:

    - status LANCAR -> program baseline (MERAH 05:00 / KUNING 00:30 / HIJAU 01:00)
    - status PADAT atau MACET -> program padat (MERAH 02:00 / KUNING 01:00 / HIJAU 03:00)

    calibration_max_count tidak lagi memengaruhi durasi lampu di logika
    ini, tapi parameter tetap diterima supaya app.py tidak perlu diubah.
    """
    status = classify_density(vehicle_count, density_low, density_high)

    program = (baseline or BASELINE_PROGRAM) if status == "LANCAR" else (congested or CONGESTED_PROGRAM)

    return LightProgram(
        red_seconds=program["red"],
        yellow_seconds=program["yellow"],
        green_seconds=program["green"],
        traffic_status=status,
    )
