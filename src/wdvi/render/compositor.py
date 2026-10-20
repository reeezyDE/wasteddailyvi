"""Bildkomposition Frame für Frame (numpy/OpenCV) -> rohe BGR-Frames für ffmpeg.

Layouts (Ausgabe render.width x render.height):
- facecam_split: oben Facecam (split_facecam_ratio der Höhe, cover-crop aus facecam_box), unten
  Gameplay (cover-crop aus gameplay_box), dünne Trennlinie in color_accent
- face_track: 9:16-Fenster volle Höhe, x folgt geglättetem Gesichtstrack (kritisch gedämpft, kein Zittern)
- gameplay_fit: Hintergrund = Bild cover-skaliert, stark weichgezeichnet (auf 1/4 Auflösung) und
  abgedunkelt; Vordergrund = ganzes Bild auf volle Breite, leicht gezoomt (1.15), vertikal mittig
- vertical_native: cover-skaliert
Zooms (EditPlan.zooms, in Ausgabezeit umgerechnet): weiches Ein-/Ausblenden (ease-in-out 0.25 s),
subpixel-genau via cv2.warpAffine; focus face -> Facecam-Panel bzw. Gesicht, gameplay -> Gameplay-Panel,
center -> ganzes Bild. Overlays (RGBA-PNGs) mit Zeitfenstern und Pop-Animation werden alpha-geblendet.

Edit-Engine v2 (alles optional, leere Felder = Verhalten wie oben):
- Multi-Shot: CompositionSpec.shots (Ausgabezeit) -> je Frame die Geometrie des aktiven Shots
  (face = 9:16 um das Gesicht, split, gameplay = 9:16 aus Gameplay/focus, fit); Geometrie je Shot vorberechnet
- Übergänge am Shot-Anfang: flash (Weißblitz), punch (Zoom 1.12 -> 1.0), glitch (RGB-Versatz + Zeilenversatz)
- Effekte: shake (abklingendes Kamerawackeln), flash
- Annotationen (Kreis/Rahmen/Pfeil) in Quellkoordinaten -> über die Shot-Geometrie ins Ausgabebild
- Farbkorrektur (Sättigung/Kontrast/Wärme) auf dem Quellbild, also vor Trennlinie/Overlays/Captions
Reihenfolge je Frame: Grade -> Panels -> Trennlinie -> Annotationen -> Kamera (Zoom/Punch/Shake) -> Glitch
-> Blitz -> Overlays (Captions brennt ffmpeg/libass danach ein).
"""

from __future__ import annotations

import bisect
import logging
import math
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw

from wdvi.config import RenderConfig, Settings
from wdvi.render import _ff, _fonts, captions
from wdvi.render import encoder as encoder_mod
from wdvi.render._ff import RenderError
from wdvi.schemas import Box, LayoutHint

log = logging.getLogger(__name__)

ZOOM_RAMP_S = 0.25
ZOOM_DRIFT = 0.03  # sanftes Weiterzoomen während eines Zooms (+3 %)
POP_S = 0.12
POP_FROM = 0.85
DIVIDER_PX = 6
FACECAM_INSET = 0.04  # Facecam-Rahmen/Ränder abschneiden
FG_ZOOM = 1.15
BG_DARKEN = 0.62
BG_SIGMA = 7.0  # Weichzeichner auf 1/4 Auflösung (~28 px bei voller Auflösung)
SPRING_OMEGA = 3.2  # Face-Track-Feder (rad/s): ~1 s Einschwingzeit, kein Überschwingen
TRACK_DEADZONE = 0.025  # Anteil der Quellbreite; kleinere Bewegungen werden ignoriert
NATIVE_COVER_MAX_ASPECT = 0.8  # breitere Hochkant-/Quadratquellen -> Fit statt Cover (sonst zu viel Beschnitt)
QUEUE_FRAMES = 6
STALL_TIMEOUT_S = 120.0  # so lange ohne Fortschritt (kein Frame gelesen/angenommen) -> ffmpeg hängt
MIN_DEADLINE_S = 300.0  # Gesamtfrist der Komposition: max(MIN_DEADLINE_S, Dauer * DEADLINE_PER_S)
DEADLINE_PER_S = 30.0

# --- Edit-Engine v2
FACE_HEAD_SHOULDERS = 3.4  # Ausschnitthöhe = Gesichtshöhe * Faktor (Kopf + Schultern)
FACE_MIN_CROP = 0.5  # face-Shot: mindestens halbe Höhe des Bereichs (sonst zu starke Vergrößerung)
FACE_Y_IN_CROP = 0.40  # Gesichtsmitte bei 40 % der Ausschnitthöhe (Platz für Schultern)
GAMEPLAY_MIN_CROP = 0.55  # gameplay-Shot mit focus: min. Anteil der Gameplay-Höhe
GAMEPLAY_FOCUS_PAD = 1.6  # focus-Box * Faktor muss in den Ausschnitt passen
GAMEPLAY_FOCUS_Y = 0.42  # focus etwas über der Mitte (Abstand zur Caption-Zeile bei 0.66)
SHARPEN_ABOVE = 2.5  # ab dieser Vergrößerung: auf halber Zielgröße skalieren, dort schärfen, dann hoch
SHARPEN_AMOUNT = 0.6
SHARPEN_SIGMA = 2.0
FIRST_FRAME_CLEAN_S = 0.05  # Übergänge/Blitze davor entfallen (Frame 0 = Cover, muss sauber sein)
FLASH_PEAK = 0.85
FX_FLASH_PEAK = 0.7
PUNCH_FROM = 1.12
GLITCH_SHIFT = 26.0  # px RGB-Versatz bei voller Stärke (bezogen auf 1080 Breite)
GLITCH_SLICE = 70.0  # px Zeilenversatz
SHAKE_S = 0.35
SHAKE_PX = 18.0
ANN_DRAW_S = 0.25  # Kreis "zeichnet sich selbst"
ANN_POP_S = 0.2
ANN_FADE_S = 0.12
ANN_STROKE = 12  # px bei 1080 Breite
ANN_OUTLINE = 4  # dunkle Kontur je Seite
ANN_MIN_VISIBLE = 0.4  # Anteil der Box, der im aktuellen Shot sichtbar sein muss
ANN_YELLOW = (0x35, 0xE1, 0xFF)  # BGR von #FFE135
ANN_DARK = (18, 18, 18)


@dataclass
class TimedOverlay:
    png: Path
    start: float  # Ausgabezeit
    end: float
    x: int  # linke obere Ecke im Ausgabebild (vor Pop-Skalierung, Pop skaliert um die Mitte)
    y: int
    pop: bool = True
    fade_out: float = 0.15
    pop_from: float = POP_FROM  # Startskalierung der Pop-Animation
    pop_s: float = POP_S


@dataclass
class ZoomOut:
    start: float  # Ausgabezeit
    end: float
    scale: float
    focus: str


@dataclass
class ShotOut:
    """Shot in Ausgabezeit (aus schemas.Shot über die TimeMap)."""

    start: float
    end: float
    kind: str  # face | split | gameplay | fit
    focus: Box | None = None  # normalisiert, Quelle
    zoom: float = 1.0
    transition: str = "cut"  # cut | flash | punch | glitch (am Anfang des Shots)


@dataclass
class AnnotationOut:
    start: float  # Ausgabezeit
    end: float
    kind: str  # circle | box | arrow
    box: Box  # normalisiert, Quelle
    color: str = "#FF3B3B"  # "#RRGGBB"
    label: str | None = None


@dataclass
class FxEvent:
    t: float  # Ausgabezeit
    kind: str  # shake | flash


@dataclass
class GradeSpec:
    saturation: float = 1.15
    contrast: float = 1.06
    warmth: float = 0.03

    @classmethod
    def from_render(cls, r: RenderConfig) -> GradeSpec:
        return cls(saturation=float(r.grade_saturation), contrast=float(r.grade_contrast),
                   warmth=float(r.grade_warmth))


@dataclass
class CompositionSpec:
    layout: LayoutHint
    zooms: list[ZoomOut] = field(default_factory=list)
    overlays: list[TimedOverlay] = field(default_factory=list)
    # --- Edit-Engine v2
    shots: list[ShotOut] = field(default_factory=list)
    annotations: list[AnnotationOut] = field(default_factory=list)
    effects: list[FxEvent] = field(default_factory=list)
    grade: GradeSpec | None = None
    face_box: Box | None = None  # typische Gesichtsposition (normalisiert, Quelle) für face-Shots


# --------------------------------------------------------------------------- Mathe-Helfer


def ease_in_out(x: float) -> float:
    x = min(1.0, max(0.0, x))
    return x * x * (3.0 - 2.0 * x)


def ease_out_back(x: float, s: float = 1.70158) -> float:
    x = min(1.0, max(0.0, x)) - 1.0
    return 1.0 + (s + 1.0) * x ** 3 + s * x ** 2


def ease_out_cubic(x: float) -> float:
    x = min(1.0, max(0.0, x))
    return 1.0 - (1.0 - x) ** 3


def zoom_value(z: ZoomOut, t: float) -> float:
    """Zoomfaktor zur Ausgabezeit t (1.0 außerhalb), weiche Rampen an beiden Enden."""
    if t <= z.start or t >= z.end:
        return 1.0
    length = z.end - z.start
    ramp = min(ZOOM_RAMP_S, length / 2.0)
    if t < z.start + ramp:
        e = ease_in_out((t - z.start) / ramp)
    elif t > z.end - ramp:
        e = ease_in_out((z.end - t) / ramp)
    else:
        e = 1.0
    target = min(2.0, max(1.0, z.scale)) * (1.0 + ZOOM_DRIFT * (t - z.start) / length)
    return 1.0 + (target - 1.0) * e


def cover_rect(x: float, y: float, w: float, h: float, aspect: float, cx: float | None = None,
               cy: float | None = None) -> tuple[float, float, float, float]:
    """Größtes Rechteck mit Seitenverhältnis aspect (w/h) im Rechteck (x, y, w, h), um (cx, cy) zentriert."""
    if w <= 0 or h <= 0:
        raise ValueError("leeres Quellrechteck")
    if w / h > aspect:
        rh, rw = h, h * aspect
    else:
        rw, rh = w, w / aspect
    cx = x + w / 2 if cx is None else cx
    cy = y + h / 2 if cy is None else cy
    rx = min(max(cx - rw / 2, x), x + w - rw)
    ry = min(max(cy - rh / 2, y), y + h - rh)
    return rx, ry, rw, rh


def sized_rect(bounds: tuple[float, float, float, float], aspect: float, height: float, cx: float,
               cy: float) -> tuple[float, float, float, float]:
    """Rechteck mit Seitenverhältnis aspect und (höchstens) Höhe height um (cx, cy), in bounds geklemmt."""
    bx, by, bw, bh = bounds
    rx, ry, rw, rh = cover_rect(bx, by, bw, bh, aspect)  # größtmöglich
    k = min(1.0, max(1e-6, height) / rh)
    rw, rh = rw * k, rh * k
    return (min(max(cx - rw / 2, bx), bx + bw - rw), min(max(cy - rh / 2, by), by + bh - rh), rw, rh)


def face_crop(bounds: tuple[float, float, float, float], face: tuple[float, float, float, float] | None,
              aspect: float) -> tuple[float, float, float, float]:
    """9:16-Ausschnitt für einen face-Shot: Kopf + Schultern, Gesicht im oberen Drittel, in bounds."""
    bx, by, bw, bh = bounds
    if face is None:
        return cover_rect(bx, by, bw, bh, aspect)
    fx, fy, fw, fh = face
    fcx, fcy = fx + fw / 2, fy + fh / 2
    if not (bx <= fcx <= bx + bw and by <= fcy <= by + bh):  # Gesicht liegt nicht im Bereich
        return cover_rect(bx, by, bw, bh, aspect)
    full_h = cover_rect(bx, by, bw, bh, aspect)[3]
    h = min(full_h, max(fh * FACE_HEAD_SHOULDERS, full_h * FACE_MIN_CROP))
    return sized_rect(bounds, aspect, h, fcx, fcy + h * (0.5 - FACE_Y_IN_CROP))


def _box_px(b: Box, sw: int, sh: int) -> tuple[float, float, float, float]:
    x0, y0 = min(max(b.x, 0.0), 1.0) * sw, min(max(b.y, 0.0), 1.0) * sh
    x1, y1 = min(max(b.x + b.w, 0.0), 1.0) * sw, min(max(b.y + b.h, 0.0), 1.0) * sh
    return x0, y0, max(1.0, x1 - x0), max(1.0, y1 - y0)


def _hex_bgr(h: str) -> tuple[int, int, int]:
    h = h.strip().lstrip("#")
    try:
        return int(h[4:6], 16), int(h[2:4], 16), int(h[0:2], 16)
    except (ValueError, IndexError):
        return 255, 255, 255


# --------------------------------------------------------------------------- Farbkorrektur


class Grader:
    """Sättigung (uint8, cv2.addWeighted gegen Graustufen) + Kontrast-S-Kurve und Wärme als LUT je Kanal."""

    def __init__(self, g: GradeSpec) -> None:
        self.sat = float(min(2.0, max(0.0, g.saturation)))
        x = np.arange(256, dtype=np.float32) / 255.0
        k = float(min(0.6, max(-0.6, g.contrast - 1.0)))
        # S-Kurve ohne Clipping: Steigung 1+k in der Mitte, 1-k an den Enden
        y = x - k * np.sin(2 * np.pi * x) / (2 * np.pi)
        w = float(min(0.2, max(-0.2, g.warmth)))
        bell = 4.0 * x * (1.0 - x)  # Wärme v. a. in den Mitten, Schwarz/Weiß bleiben neutral
        r = y + w * 0.9 * bell
        gr = y + w * 0.25 * bell
        b = y - w * 0.9 * bell
        lut = np.stack([b, gr, r], axis=-1).reshape(256, 1, 3)
        self.lut = np.clip(np.round(lut * 255.0), 0, 255).astype(np.uint8)

    def __call__(self, img: np.ndarray) -> np.ndarray:
        if abs(self.sat - 1.0) > 1e-3:
            gray = cv2.cvtColor(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)
            img = cv2.addWeighted(img, self.sat, gray, 1.0 - self.sat, 0.0)
        return cv2.LUT(img, self.lut)


# --------------------------------------------------------------------------- Panels


@dataclass
class _Panel:
    name: str  # "face" | "gameplay" | "fg" | "full"
    dst: tuple[int, int, int, int]  # x, y, w, h im Ausgabebild
    src: tuple[float, float, float, float]  # Quellrechteck (px), gleiches Seitenverhältnis wie dst
    focus: tuple[float, float] | None = None  # Zoom-Fokus in Quell-px (None = Mitte)
    sharpen: bool = False  # kleiner Quellausschnitt stark vergrößert -> vorher nachschärfen

    def int_crop(self) -> tuple[int, int, int, int]:
        x, y, w, h = self.src
        x0, y0 = int(round(x)), int(round(y))
        return x0, y0, max(1, int(round(x + w)) - x0), max(1, int(round(y + h)) - y0)


def _resize_interp(scale: float) -> int:
    if scale < 0.95:
        return cv2.INTER_AREA
    return cv2.INTER_CUBIC if scale > 1.05 else cv2.INTER_LINEAR


def _warp_interp(scale: float) -> int:
    return cv2.INTER_CUBIC if scale > 1.05 else cv2.INTER_LINEAR


def _sharpen(img: np.ndarray) -> np.ndarray:
    blur = cv2.GaussianBlur(img, (0, 0), SHARPEN_SIGMA)
    return cv2.addWeighted(img, 1.0 + SHARPEN_AMOUNT, blur, -SHARPEN_AMOUNT, 0.0)


def _effective_src(p: _Panel, zoom: float = 1.0, src: tuple[float, float, float, float] | None = None,
                   focus: tuple[float, float] | None = None) -> tuple[float, float, float, float]:
    """Tatsächlich sichtbares Quellrechteck eines Panels bei Zoom (um den Fokuspunkt)."""
    sx, sy, sw, sh = src if src is not None else p.src
    if zoom <= 1.0005:
        return sx, sy, sw, sh
    fx, fy = focus if focus is not None else (p.focus or (sx + sw / 2, sy + sh / 2))
    fx, fy = min(max(fx, sx), sx + sw), min(max(fy, sy), sy + sh)
    return fx + (sx - fx) / zoom, fy + (sy - fy) / zoom, sw / zoom, sh / zoom


def _draw_panel(img: np.ndarray, canvas: np.ndarray, p: _Panel, zoom: float = 1.0,
                src: tuple[float, float, float, float] | None = None, focus: tuple[float, float] | None = None) -> None:
    """Quellrechteck -> Zielregion (eine Resampling-Stufe; Zoom/Subpixel über warpAffine)."""
    dx, dy, dw, dh = p.dst
    region = canvas[dy:dy + dh, dx:dx + dw]
    if p.sharpen and dw >= 8 and dh >= 8:
        # starke Vergrößerung (kleine Facecam): auf halber Größe rendern + schärfen, dann linear hoch –
        # schärfer als direkt bikubisch und ohne breite Überschwinger-Säume
        half = _Panel(p.name, (0, 0, dw // 2, dh // 2), p.src, p.focus)
        tmp = np.empty((dh // 2, dw // 2, 3), np.uint8)
        _draw_panel(img, tmp, half, zoom, src, focus)
        cv2.resize(_sharpen(tmp), (dw, dh), dst=region, interpolation=cv2.INTER_LINEAR)
        return
    sx, sy, sw, sh = src if src is not None else p.src
    dynamic = src is not None
    if zoom <= 1.0005 and not dynamic:
        x0, y0, w0, h0 = p.int_crop()
        crop = img[y0:y0 + h0, x0:x0 + w0]
        cv2.resize(crop, (dw, dh), dst=region, interpolation=_resize_interp(dw / w0))
        return
    zx, zy, zw, zh = _effective_src(p, zoom, src, focus)
    kx, ky = dw / zw, dh / zh
    if kx >= 0.8:
        # Pixelmitten-Konvention wie cv2.resize (sonst springt das Bild beim Zoomstart um ~0.5 px)
        m = np.float32([[kx, 0, kx * (0.5 - zx) - 0.5], [0, ky, ky * (0.5 - zy) - 0.5]])
        cv2.warpAffine(img, m, (dw, dh), dst=region, flags=_warp_interp(kx), borderMode=cv2.BORDER_REPLICATE)
        return
    # starke Verkleinerung: erst sauber (INTER_AREA) aufs Panel, dann dort zoomen
    x0, y0, w0, h0 = p.int_crop()
    base = cv2.resize(img[y0:y0 + h0, x0:x0 + w0], (dw, dh), interpolation=cv2.INTER_AREA)
    fx, fy = focus if focus is not None else (p.focus or (sx + sw / 2, sy + sh / 2))
    fx, fy = min(max(fx, sx), sx + sw), min(max(fy, sy), sy + sh)
    pfx, pfy = (fx - sx) * dw / sw, (fy - sy) * dh / sh
    cv2.warpAffine(base, _zoom_matrix(zoom, pfx, pfy), (dw, dh), dst=region, flags=_warp_interp(zoom),
                   borderMode=cv2.BORDER_REPLICATE)


def _zoom_matrix(z: float, cx: float, cy: float) -> np.ndarray:
    """Skalierung um (cx, cy) (kontinuierliche Koordinaten) in Pixelmitten-Konvention."""
    return np.float32([[z, 0, z * 0.5 - 0.5 + cx * (1 - z)], [0, z, z * 0.5 - 0.5 + cy * (1 - z)]])


# --------------------------------------------------------------------------- Overlays


@dataclass
class _OverlayAsset:
    ov: TimedOverlay
    prem: np.ndarray  # float32 (h, w, 3): BGR * alpha
    alpha: np.ndarray  # float32 (h, w, 1) in [0, 1]
    inv: np.ndarray  # 1 - alpha
    cx: float  # Mitte des sichtbaren Inhalts im Ausgabebild
    cy: float


def _load_overlay(ov: TimedOverlay) -> _OverlayAsset | None:
    img = cv2.imread(str(ov.png), cv2.IMREAD_UNCHANGED)
    if img is None:
        log.warning("Overlay %s not readable, skipped", ov.png)
        return None
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGRA)
    elif img.shape[2] == 3:
        img = cv2.cvtColor(img, cv2.COLOR_BGR2BGRA)
    if img.dtype != np.uint8:
        img = (img / 257).astype(np.uint8) if img.dtype == np.uint16 else img.astype(np.uint8)
    a = img[:, :, 3]
    ys, xs = np.nonzero(a)
    if len(xs) == 0:
        return None
    y0, y1, x0, x1 = int(ys.min()), int(ys.max()) + 1, int(xs.min()), int(xs.max()) + 1
    img = img[y0:y1, x0:x1]
    alpha = img[:, :, 3:4].astype(np.float32) / 255.0
    prem = img[:, :, :3].astype(np.float32) * alpha
    h, w = img.shape[:2]
    return _OverlayAsset(ov, prem, alpha, 1.0 - alpha, ov.x + x0 + w / 2, ov.y + y0 + h / 2)


def _blend(canvas: np.ndarray, prem: np.ndarray, alpha: np.ndarray, inv: np.ndarray | None, x: int, y: int,
           k: float) -> None:
    H, W = canvas.shape[:2]
    h, w = alpha.shape[:2]
    x0, y0, x1, y1 = max(0, x), max(0, y), min(W, x + w), min(H, y + h)
    if x1 <= x0 or y1 <= y0:
        return
    sl = (slice(y0 - y, y1 - y), slice(x0 - x, x1 - x))
    roi = canvas[y0:y1, x0:x1]
    if k >= 0.999 and inv is not None:
        out = roi.astype(np.float32) * inv[sl] + prem[sl]
    else:
        out = roi.astype(np.float32) * (1.0 - alpha[sl] * k) + prem[sl] * k
    out += 0.5
    roi[:] = out.astype(np.uint8)


def _apply_overlay(canvas: np.ndarray, a: _OverlayAsset, t: float) -> None:
    ov = a.ov
    if not (ov.start <= t < ov.end):
        return
    dt = t - ov.start
    k, s = 1.0, 1.0
    pop_s = max(0.01, ov.pop_s)
    if ov.pop and dt < pop_s:
        p = dt / pop_s
        if ov.start <= 0.001:  # Overlay ab Frame 0: voll deckend (TikTok nimmt Frame 0 als Cover)
            s = 0.94 + 0.06 * ease_out_back(p)
        else:
            s = ov.pop_from + (1.0 - ov.pop_from) * ease_out_back(p)
            k = 0.4 + 0.6 * min(1.0, p / 0.5)
    if ov.fade_out > 0 and ov.end - t < ov.fade_out:
        k *= max(0.0, (ov.end - t) / ov.fade_out)
    if k <= 0.004:
        return
    if abs(s - 1.0) > 0.004:
        h, w = a.alpha.shape[:2]
        nw, nh = max(1, round(w * s)), max(1, round(h * s))
        prem = cv2.resize(a.prem, (nw, nh), interpolation=cv2.INTER_LINEAR)
        alpha = cv2.resize(a.alpha, (nw, nh), interpolation=cv2.INTER_LINEAR)[:, :, None]
        _blend(canvas, prem, alpha, None, round(a.cx - nw / 2), round(a.cy - nh / 2), k)
    else:
        h, w = a.alpha.shape[:2]
        _blend(canvas, a.prem, a.alpha, a.inv, round(a.cx - w / 2), round(a.cy - h / 2), k)


# --------------------------------------------------------------------------- Effekte (v2)


def flash_frame(canvas: np.ndarray, a: float) -> None:
    """Weißblitz in place: canvas = canvas * (1 - a) + 255 * a."""
    if a > 0.004:
        cv2.convertScaleAbs(canvas, dst=canvas, alpha=1.0 - a, beta=255.0 * a)


def glitch_frame(canvas: np.ndarray, k: float, seed: int) -> None:
    """RGB-Kanalversatz + horizontal verschobene Bildstreifen (Stärke k 0..1), in place."""
    if k <= 0.01:
        return
    H, W = canvas.shape[:2]
    rng = np.random.default_rng(seed)
    d = int(round(GLITCH_SHIFT * W / 1080 * k))
    if d > 0:
        canvas[:, d:, 2] = canvas[:, :-d, 2].copy()  # Rot nach rechts
        canvas[:, :-d, 0] = canvas[:, d:, 0].copy()  # Blau nach links
    for _ in range(int(rng.integers(4, 8))):
        h = int(rng.integers(max(2, H // 60), max(3, H // 9)))
        y0 = int(rng.integers(0, max(1, H - h)))
        s = int(round(float(rng.uniform(-1.0, 1.0)) * GLITCH_SLICE * W / 1080 * k))
        if s:
            canvas[y0:y0 + h] = np.roll(canvas[y0:y0 + h], s, axis=1)


def shake_offset(t: float, times: list[float], width: int = 1080) -> tuple[float, float, float]:
    """(dx, dy, Amplitude) der abklingenden Kamerawackler zur Zeit t (Summe aller aktiven Ereignisse)."""
    dx = dy = amp = 0.0
    px = SHAKE_PX * width / 1080
    for te in times:
        u = t - te
        if 0.0 <= u < SHAKE_S:
            a = px * (1.0 - u / SHAKE_S) ** 2
            dx += a * math.sin(2 * math.pi * 13.0 * u + 1.3)
            dy += a * 0.8 * math.cos(2 * math.pi * 10.0 * u + 0.4)
            amp += a
    return dx, dy, amp


# --------------------------------------------------------------------------- Annotationen (v2)


def _lerp_bgr(a: tuple[int, int, int], b: tuple[int, int, int], k: float) -> tuple[int, int, int]:
    return tuple(int(round(x + (y - x) * k)) for x, y in zip(a, b, strict=True))  # type: ignore[return-value]


def _rounded_rect_pts(x0: float, y0: float, x1: float, y1: float, r: float) -> np.ndarray:
    r = max(1.0, min(r, (x1 - x0) / 2, (y1 - y0) / 2))
    pts: list[tuple[float, float]] = []
    for cx, cy, a0 in ((x1 - r, y0 + r, -90), (x1 - r, y1 - r, 0), (x0 + r, y1 - r, 90), (x0 + r, y0 + r, 180)):
        for i in range(9):
            a = math.radians(a0 + i * 90 / 8)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return np.array(pts, np.float64)


@dataclass
class _Label:
    prem: np.ndarray
    alpha: np.ndarray
    inv: np.ndarray


def _render_label(text: str, color: tuple[int, int, int], font_path: str, size: int) -> _Label | None:
    text = " ".join(text.split()).upper()[:28]
    if not text:
        return None
    font = _fonts.load(font_path, size)
    stroke = max(3, size // 9)
    probe = ImageDraw.Draw(Image.new("RGBA", (4, 4)))
    x0, y0, x1, y1 = probe.textbbox((0, 0), text, font=font, stroke_width=stroke)
    pad = stroke + 4
    img = Image.new("RGBA", (x1 - x0 + 2 * pad, y1 - y0 + 2 * pad), (0, 0, 0, 0))
    b, g, r = color
    ImageDraw.Draw(img).text((pad - x0, pad - y0), text, font=font, fill=(r, g, b, 255), stroke_width=stroke,
                             stroke_fill=(10, 10, 10, 255))
    arr = np.asarray(img)
    alpha = arr[:, :, 3:4].astype(np.float32) / 255.0
    prem = arr[:, :, 2::-1].astype(np.float32) * alpha
    return _Label(prem, alpha, 1.0 - alpha)


class _AnnotationDrawer:
    """Zeichnet eine Annotation (Ausgabe-px-Rechteck) animiert ins Bild (OpenCV, kantengeglättet)."""

    def __init__(self, a: AnnotationOut, W: int, H: int, font_path: str,
                 avoid: tuple[float, float] | None = None) -> None:
        self.a = a
        self.avoid = avoid  # y-Band (Captions), das das Label meiden soll
        self.W, self.H = W, H
        self.base = _hex_bgr(a.color)
        k = W / 1080
        self.stroke = max(2, round(ANN_STROKE * k))
        self.outline = max(1, round(ANN_OUTLINE * k))
        self.label = (_render_label(a.label, self.base, font_path, max(16, round(54 * k)))
                      if a.label and a.label.strip() else None)

    def color(self, u: float) -> tuple[int, int, int]:
        """Farbe pulsiert zwischen Grundfarbe und Gelb (wie in Referenz-Shorts: rot -> gelb)."""
        if self.base == ANN_YELLOW:
            return self.base
        return _lerp_bgr(self.base, ANN_YELLOW, 0.5 - 0.5 * math.cos(2 * math.pi * u / 0.9))

    def draw(self, canvas: np.ndarray, rect: tuple[float, float, float, float], t: float,
             area: tuple[int, int, int, int]) -> None:
        a = self.a
        u = t - a.start
        alpha = 1.0
        if a.end - t < ANN_FADE_S:
            alpha = max(0.0, (a.end - t) / ANN_FADE_S)
        if alpha <= 0.01:
            return
        x0, y0, x1, y1 = rect
        col = self.color(u)
        if a.kind == "box":
            shape = self._box_shape(x0, y0, x1, y1, u)
        elif a.kind == "arrow":
            shape = self._arrow_shape(x0, y0, x1, y1, u, area)
        else:
            shape = self._circle_shape(x0, y0, x1, y1, u)
        if shape is None:
            return
        kind, geo, bbox = shape
        m = self.stroke * 3 + 24
        bx0, by0 = max(0, int(bbox[0]) - m), max(0, int(bbox[1]) - m)
        bx1, by1 = min(self.W, int(bbox[2]) + m), min(self.H, int(bbox[3]) + m)
        if bx1 - bx0 < 2 or by1 - by0 < 2:
            return
        roi = canvas[by0:by1, bx0:bx1]
        layer = roi.copy()
        # weicher Farb-Glow unter dem Strich
        glow = np.zeros_like(layer)
        self._stroke(glow, kind, geo, col, self.stroke * 3, (bx0, by0))
        glow = cv2.GaussianBlur(glow, (0, 0), self.stroke * 1.2)
        cv2.addWeighted(layer, 1.0, glow, 0.55, 0.0, dst=layer)
        self._stroke(layer, kind, geo, ANN_DARK, self.stroke + 2 * self.outline, (bx0, by0))
        self._stroke(layer, kind, geo, col, self.stroke, (bx0, by0))
        if alpha >= 0.999:
            roi[:] = layer
        else:
            cv2.addWeighted(roi, 1.0 - alpha, layer, alpha, 0.0, dst=roi)
        if self.label is not None:
            self._draw_label(canvas, bbox, geo if kind == "arrow" else None, alpha)

    # -- Formen: (art, geometrie, bbox)

    def _circle_shape(self, x0, y0, x1, y1, u):
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        pad = 16 * self.W / 1080
        ax = max((x1 - x0) / 2 * 1.22 + pad, 46 * self.W / 1080)
        ay = max((y1 - y0) / 2 * 1.38 + pad, 40 * self.W / 1080)
        p = ease_out_cubic(u / ANN_DRAW_S)
        if u > ANN_DRAW_S:
            s = 1.0 + 0.035 * math.sin(2 * math.pi * 1.8 * (u - ANN_DRAW_S))
            ax, ay = ax * s, ay * s
        sweep = 372.0 * p
        if sweep < 2.0:
            return None
        start = 205.0  # links oben beginnen, mit kleiner Überlappung schließen (handgezeichnet)
        return "ellipse", (cx, cy, ax, ay, -4.0, start, start + sweep), (cx - ax, cy - ay, cx + ax, cy + ay)

    def _box_shape(self, x0, y0, x1, y1, u):
        pad = 14 * self.W / 1080
        s = 1.25 - 0.25 * ease_out_back(u / ANN_POP_S) if u < ANN_POP_S else 1.0
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        hw, hh = ((x1 - x0) / 2 + pad) * s, ((y1 - y0) / 2 + pad) * s
        pts = _rounded_rect_pts(cx - hw, cy - hh, cx + hw, cy + hh, 22 * self.W / 1080)
        return "poly", pts, (cx - hw, cy - hh, cx + hw, cy + hh)

    def _arrow_shape(self, x0, y0, x1, y1, u, area):
        ax0, ay0, aw, ah = area
        k = self.W / 1080
        free = {"left": (x0 - ax0, (-1.0, 0.0)), "right": (ax0 + aw - x1, (1.0, 0.0)),
                "top": (y0 - ay0, (0.0, -1.0)), "bottom": ((ay0 + ah - y1) * 0.8, (0.0, 1.0))}
        side = max(free, key=lambda s: free[s][0])
        space, (dx, dy) = free[side]
        length = min(230 * k, max(90 * k, space - 40 * k))
        gap = 16 * k
        if side in ("left", "right"):
            tip = (x0 - gap if side == "left" else x1 + gap, (y0 + y1) / 2)
        else:
            tip = ((x0 + x1) / 2, y0 - gap if side == "top" else y1 + gap)
        off = (1.0 - ease_out_cubic(u / ANN_POP_S)) * 90 * k + 9 * k * (0.5 + 0.5 * math.sin(2 * math.pi * 2.0 * u))
        tx, ty = tip[0] + dx * off, tip[1] + dy * off
        sx, sy = tx + dx * length, ty + dy * length
        return "arrow", (sx, sy, tx, ty), (min(sx, tx), min(sy, ty), max(sx, tx), max(sy, ty))

    def _stroke(self, img: np.ndarray, kind: str, geo, color, thick: int, off: tuple[int, int]) -> None:
        S = 4  # Subpixel-Shift (Koordinaten * 16)
        f = 1 << S
        ox, oy = off
        if kind == "ellipse":
            cx, cy, ax, ay, ang, a0, a1 = geo
            cv2.ellipse(img, (round((cx - ox) * f), round((cy - oy) * f)), (round(ax * f), round(ay * f)), ang, a0,
                        a1, color, thick, cv2.LINE_AA, S)
        elif kind == "poly":
            pts = np.round((geo - np.array([ox, oy])) * f).astype(np.int32)
            cv2.polylines(img, [pts], True, color, thick, cv2.LINE_AA, S)
        else:
            sx, sy, tx, ty = geo
            length = max(1.0, math.hypot(tx - sx, ty - sy))
            cv2.arrowedLine(img, (round((sx - ox) * f), round((sy - oy) * f)), (round((tx - ox) * f),
                            round((ty - oy) * f)), color, thick, cv2.LINE_AA, S,
                            tipLength=min(0.45, 70 * self.W / 1080 / length))

    def _draw_label(self, canvas: np.ndarray, bbox, arrow, alpha: float) -> None:
        lab = self.label
        h, w = lab.alpha.shape[:2]
        gap = round(14 * self.W / 1080)
        if arrow is not None:  # am Pfeilende
            sx, sy, tx, ty = arrow
            x = round(sx - w / 2) if abs(sx - tx) < 1 else (round(sx - w - gap) if sx < tx else round(sx + gap))
            y = round(sy - h / 2) if abs(sy - ty) < 1 else (round(sy - h - gap) if sy < ty else round(sy + gap))
        else:
            cx = (bbox[0] + bbox[2]) / 2
            x = round(cx - w / 2)
            below, above = round(bbox[3] + gap), round(bbox[1] - gap - h)
            order = [below, above] if (bbox[1] + bbox[3]) / 2 < self.H * 0.45 else [above, below]
            if self.avoid is not None:  # nicht in die Caption-Zeile
                a0, a1 = self.avoid
                free = [yy for yy in order if (yy + h <= a0 or yy >= a1) and 0 <= yy <= self.H - h]
                order = free + order
            y = order[0]
        m = round(30 * self.W / 1080)
        x = min(max(x, m), self.W - m - w)
        y = min(max(y, m), self.H - m - h)
        _blend(canvas, lab.prem, lab.alpha, lab.inv if alpha >= 0.999 else None, x, y, alpha)


# --------------------------------------------------------------------------- Komposition


@dataclass
class _Geom:
    """Bildaufteilung eines Layouts bzw. Shots (vorberechnet)."""

    kind: str  # facecam_split | face_track | gameplay_fit | vertical_native | face | gameplay
    panels: list[_Panel]
    divider: tuple[int, int] | None = None
    bg: dict | None = None
    win: tuple[float, float, float, float] | None = None  # face_track: Fenster, dessen Lage der Track steuert
    horizontal: bool = True
    tracked: bool = False


@dataclass
class _ShotRun:
    start: float  # Ausgabezeit (lückenlos normalisiert)
    end: float
    shot: ShotOut
    geom: _Geom
    zoom: float  # zusätzlicher Shot-Zoom


def normalize_shots(shots: list[ShotOut], duration: float | None = None) -> list[ShotOut]:
    """Shots sortieren, Überlappungen kappen (späterer Shot gewinnt), Lücken zur Hälfte dem Vorgänger und
    Nachfolger zuschlagen (= nächstgelegener Shot); erster beginnt bei 0, letzter läuft bis zum Ende."""
    items = sorted((s for s in shots if s.end > s.start), key=lambda s: (s.start, s.end))
    out: list[ShotOut] = []
    for s in items:
        s = ShotOut(start=max(0.0, s.start), end=s.end, kind=s.kind, focus=s.focus, zoom=s.zoom,
                    transition=s.transition)
        if duration is not None:
            s.end = min(s.end, duration)
            if s.end - s.start < 1e-3:
                continue
        if out and s.start < out[-1].end:
            if s.start - out[-1].start < 1e-3:  # gleicher Start -> der spätere ersetzt den früheren
                out.pop()
            else:
                out[-1].end = s.start
        out.append(s)
    if not out:
        return []
    out[0].start = 0.0
    for a, b in zip(out, out[1:], strict=False):
        if b.start > a.end:
            mid = (a.end + b.start) / 2
            a.end, b.start = mid, mid
    out[-1].end = max(out[-1].end, duration) if duration is not None else math.inf
    return out


class Composer:
    """Baut aus einem Quellframe (BGR) das fertige Ausgabebild (ohne Captions – die macht libass)."""

    def __init__(self, spec: CompositionSpec, settings: Settings, src_w: int, src_h: int, fps: float) -> None:
        r = settings.config.render
        self.W, self.H, self.fps = int(r.width), int(r.height), float(fps)
        self.sw, self.sh = int(src_w), int(src_h)
        self.accent = _hex_bgr(settings.config.brand.color_accent)
        self.settings = settings
        hint = spec.layout
        self.hint = hint
        self.zooms = [z for z in spec.zooms if z.end - z.start > 0.05 and z.scale > 1.001]
        self._track: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None
        pts = sorted(hint.track, key=lambda p: p.t)
        if pts:
            self._track = (np.array([p.t for p in pts], np.float64), np.array([p.cx for p in pts], np.float64),
                           np.array([p.cy for p in pts], np.float64))
        self._spring: list[float] | None = None
        self._spring_target: float | None = None
        self.face_box = spec.face_box
        self.grader = Grader(spec.grade) if spec.grade is not None else None
        self.trans_s = max(0.04, float(r.transition_ms) / 1000.0)

        self.runs: list[_ShotRun] = []
        self._run_starts: list[float] = []
        self._cur_run = -1
        shots = normalize_shots(spec.shots)
        if shots:
            cache: dict[tuple, _Geom] = {}
            for s in shots:
                g = self._shot_geom(s, cache)
                self.runs.append(_ShotRun(s.start, s.end, s, g, float(min(1.6, max(1.0, s.zoom)))))
            self._run_starts = [ru.start for ru in self.runs]
            self.kind = "shots"
            self.geom = self.runs[0].geom
        else:
            kind = hint.kind
            if kind == "facecam_split" and hint.facecam_box is None:
                log.info("facecam_split without facecam_box -> gameplay_fit")
                kind = "gameplay_fit"
            fit_zoom = FG_ZOOM
            aspect = self.sw / self.sh
            if kind == "vertical_native" and aspect > NATIVE_COVER_MAX_ASPECT:
                kind, fit_zoom = "gameplay_fit", 1.0  # quadratisch/breit: nichts abschneiden
            elif kind == "gameplay_fit" and aspect <= NATIVE_COVER_MAX_ASPECT:
                kind = "vertical_native"  # Quelle ist schon hochkant -> Cover
            self.kind = kind
            self.geom = self._layout_geom(kind, settings, fit_zoom)
        # Kompatibilität (v1-Attribute)
        self.panels = self.geom.panels
        self.divider = self.geom.divider
        self._bg = self.geom.bg
        if self.geom.win is not None:
            self.win, self.horizontal = self.geom.win, self.geom.horizontal

        self.overlays = [a for a in (_load_overlay(o) for o in spec.overlays if o.end > o.start) if a is not None]
        # Übergänge (nicht am ersten Frame: der muss sauber sein)
        self.transitions: list[tuple[float, str, int]] = [
            (ru.start, ru.shot.transition, i) for i, ru in enumerate(self.runs)
            if ru.shot.transition != "cut" and ru.start > FIRST_FRAME_CLEAN_S]
        self.shakes = sorted(e.t for e in spec.effects if e.kind == "shake" and e.t > FIRST_FRAME_CLEAN_S)
        self.flashes = sorted(e.t for e in spec.effects if e.kind == "flash" and e.t > FIRST_FRAME_CLEAN_S)
        font = str(_fonts.font_path(settings.config.paths.fonts_dir, settings.config.brand.font_captions))
        self.annotations = [_AnnotationDrawer(a, self.W, self.H, font, self._caption_band())
                            for a in sorted(spec.annotations, key=lambda a: a.start) if a.end - a.start > 0.05]
        self._maps: list[tuple[_Panel, tuple[float, float, float, float]]] = []

    def _caption_band(self) -> tuple[float, float]:
        cfg = self.settings.config
        y = captions.caption_y_ratio(cfg, "shots" if self.runs else self.kind) * self.H
        half = cfg.render.captions.font_size * (1.1 if self.runs else 0.7) + cfg.render.captions.outline
        return y - half, y + half

    # -- Geometrie je Layout (v1)

    def _layout_geom(self, kind: str, settings: Settings, fit_zoom: float = FG_ZOOM) -> _Geom:
        if kind == "facecam_split":
            return self._geom_split(settings)
        if kind == "face_track":
            aspect = self.W / self.H
            return self._geom_tracked(cover_rect(0, 0, self.sw, self.sh, aspect), "full", "face_track")
        if kind == "gameplay_fit":
            return self._geom_fit(fit_zoom)
        return self._geom_cover()

    def _geom_split(self, settings: Settings) -> _Geom:
        hint = self.hint
        top_h = int(round(settings.config.render.split_facecam_ratio * self.H))
        top_h = min(max(top_h, self.H // 5), self.H - self.H // 5)
        bx, by, bw, bh = self._facecam_px()
        face_src = cover_rect(bx, by, bw, bh, self.W / top_h)
        gx, gy, gw, gh = _box_px(hint.gameplay_box or Box(x=0, y=0, w=1, h=1), self.sw, self.sh)
        game_src = cover_rect(gx, gy, gw, gh, self.W / (self.H - top_h))
        panels = [_Panel("face", (0, 0, self.W, top_h), face_src),
                  _Panel("gameplay", (0, top_h, self.W, self.H - top_h), game_src)]
        return _Geom("facecam_split", panels, divider=(top_h - DIVIDER_PX // 2, top_h - DIVIDER_PX // 2 + DIVIDER_PX))

    def _facecam_px(self) -> tuple[float, float, float, float]:
        bx, by, bw, bh = _box_px(self.hint.facecam_box, self.sw, self.sh)  # type: ignore[arg-type]
        return (bx + bw * FACECAM_INSET, by + bh * FACECAM_INSET, bw * (1 - 2 * FACECAM_INSET),
                bh * (1 - 2 * FACECAM_INSET))

    def _geom_tracked(self, win: tuple[float, float, float, float], name: str, kind: str) -> _Geom:
        horizontal = self.sw / self.sh > self.W / self.H
        return _Geom(kind, [_Panel(name, (0, 0, self.W, self.H), win)], win=win, horizontal=horizontal,
                     tracked=True)

    def _geom_fit(self, z: float) -> _Geom:
        W, H = self.W, self.H
        fg_h = int(round(W * z * self.sh / self.sw))
        fg_h = min(fg_h - fg_h % 2, H)
        src_w = self.sw / z
        fg_src = cover_rect((self.sw - src_w) / 2, 0, src_w, self.sh, W / fg_h)
        fg_y = (H - fg_h) // 2
        panels = [_Panel("fg", (0, fg_y, W, fg_h), fg_src)]
        # Hintergrund auf 1/4 Auflösung: Cover-Ausschnitt, Weichzeichner, Abdunkeln + Vignette + Schatten am Vordergrund
        sw4, sh4 = max(8, W // 4), max(8, H // 4)
        yy = np.linspace(-1, 1, sh4, dtype=np.float32)[:, None]
        xx = np.linspace(-1, 1, sw4, dtype=np.float32)[None, :]
        vign = 1.0 - 0.35 * np.clip(xx ** 2 * 0.6 + yy ** 2, 0, 1)
        rows = np.arange(sh4, dtype=np.float32)[:, None] * (H / sh4)
        top, bot = fg_y, fg_y + fg_h
        dist = np.minimum(np.abs(rows - top), np.abs(rows - bot))
        shadow = 1.0 - 0.55 * np.exp(-((dist / (H * 0.035)) ** 2))
        dark = (BG_DARKEN * vign * shadow).astype(np.float32)[:, :, None]
        bg = {"src": cover_rect(0, 0, self.sw, self.sh, W / H), "size": (sw4, sh4), "dark": dark}
        return _Geom("gameplay_fit", panels, bg=bg)

    def _geom_cover(self) -> _Geom:
        return _Geom("vertical_native",
                     [_Panel("full", (0, 0, self.W, self.H), cover_rect(0, 0, self.sw, self.sh, self.W / self.H))])

    # -- Geometrie je Shot (v2)

    def _fit_geom(self) -> _Geom:
        aspect = self.sw / self.sh
        if aspect <= NATIVE_COVER_MAX_ASPECT:
            return self._geom_cover()
        return self._geom_fit(FG_ZOOM if aspect >= 1.2 else 1.0)

    def _single(self, kind: str, name: str, src: tuple[float, float, float, float],
                focus: tuple[float, float] | None = None) -> _Geom:
        sharpen = self.H / max(1.0, src[3]) > SHARPEN_ABOVE
        return _Geom(kind, [_Panel(name, (0, 0, self.W, self.H), src, focus=focus, sharpen=sharpen)])

    def _shot_geom(self, s: ShotOut, cache: dict[tuple, _Geom]) -> _Geom:
        fkey = (round(s.focus.x, 4), round(s.focus.y, 4), round(s.focus.w, 4), round(s.focus.h, 4)) if s.focus else None
        key = (s.kind, fkey if s.kind in ("gameplay", "face") else None)
        if key not in cache:
            cache[key] = self._build_shot_geom(s)
        return cache[key]

    def _build_shot_geom(self, s: ShotOut) -> _Geom:
        hint = self.hint
        aspect = self.W / self.H
        full = (0.0, 0.0, float(self.sw), float(self.sh))
        if s.kind == "split":
            if hint.facecam_box is not None:
                return self._geom_split(self.settings)
            log.info("Shot 'split' without facecam_box -> fit")
            return self._fit_geom()
        if s.kind == "face":
            face = _box_px(s.focus or self.face_box, self.sw, self.sh) if (s.focus or self.face_box) else None
            if hint.facecam_box is not None:
                src = face_crop(self._facecam_px(), face, aspect)
                return self._single("face", "face", src, (src[0] + src[2] / 2, src[1] + src[3] / 2))
            if self._track is not None:
                win = face_crop(full, face, aspect) if face else cover_rect(*full, aspect)
                return self._geom_tracked(win, "face", "face")
            if face is not None:
                src = face_crop(full, face, aspect)
                return self._single("face", "face", src, (face[0] + face[2] / 2, face[1] + face[3] / 2))
            log.info("Shot 'face' without face info -> fit")
            return self._fit_geom()
        if s.kind == "gameplay":
            bounds = _box_px(hint.gameplay_box, self.sw, self.sh) if hint.gameplay_box else full
            if s.focus is None:
                return self._single("gameplay", "gameplay", cover_rect(*bounds, aspect))
            # mit focus: ganzes Bild als Rahmen (gameplay_box würde den focus sonst an den Rand drücken)
            fx, fy, fw, fh = _box_px(s.focus, self.sw, self.sh)
            fcx, fcy = fx + fw / 2, fy + fh / 2
            bounds = full
            full_h = cover_rect(*bounds, aspect)[3]
            h = min(full_h, max(fh * GAMEPLAY_FOCUS_PAD, fw * GAMEPLAY_FOCUS_PAD / aspect, full_h * GAMEPLAY_MIN_CROP))
            src = sized_rect(bounds, aspect, h, fcx, fcy + h * (0.5 - GAMEPLAY_FOCUS_Y))
            return self._single("gameplay", "gameplay", src, (fcx, fcy))
        return self._fit_geom()

    # -- Zoom-Zuordnung

    def _targets(self, focus: str, geom: _Geom | None = None) -> tuple[str, ...]:
        g = geom or self.geom
        if g.kind == "facecam_split":
            return {"face": ("face",), "gameplay": ("gameplay",)}.get(focus, ("face", "gameplay"))
        if g.kind == "gameplay_fit":
            return ("canvas",) if focus == "center" else ("fg",)
        return tuple(p.name for p in g.panels)

    def _zoom_for(self, target: str, t: float, geom: _Geom | None = None) -> tuple[float, str]:
        best, foc = 1.0, "center"
        for z in self.zooms:
            if target in self._targets(z.focus, geom):
                v = zoom_value(z, t)
                if v > best:
                    best, foc = v, z.focus
        return best, foc

    # -- Face-Track

    def _track_at(self, t: float) -> tuple[float, float] | None:
        if self._track is None:
            return None
        ts, cx, cy = self._track
        return float(np.interp(t, ts, cx)), float(np.interp(t, ts, cy))

    def _window(self, t: float, win: tuple[float, float, float, float] | None = None,
                horizontal: bool | None = None) -> tuple[tuple[float, float, float, float], tuple[float, float] | None]:
        win = win if win is not None else self.win
        horizontal = self.horizontal if horizontal is None else horizontal
        wx, wy, ww, wh = win
        pt = self._track_at(t)
        if pt is None:
            return win, None
        fx, fy = pt[0] * self.sw, pt[1] * self.sh
        raw = fx if horizontal else fy
        dead = TRACK_DEADZONE * (self.sw if horizontal else self.sh)
        if self._spring_target is None or abs(raw - self._spring_target) > dead:
            self._spring_target = raw
        target = self._spring_target
        if self._spring is None:
            self._spring = [target, 0.0]
        else:  # kritisch gedämpfte Feder (exakte Lösung, stabil für jedes dt)
            x, v = self._spring
            dt = 1.0 / self.fps
            w = SPRING_OMEGA
            e = math.exp(-w * dt)
            change = x - target
            temp = (v + w * change) * dt
            self._spring = [target + (change + temp) * e, (v - w * temp) * e]
        c = self._spring[0]
        if horizontal:
            wx = min(max(c - ww / 2, 0.0), self.sw - ww)
            fx = c  # Zoom-Fokus ebenfalls geglättet (kein Zittern beim Gesichts-Zoom)
        else:
            wy = min(max(c - wh / 2, 0.0), self.sh - wh)
            fy = c
        return (wx, wy, ww, wh), (fx, fy)

    # -- Shots

    def run_at(self, t: float) -> _ShotRun | None:
        """Aktiver Shot zur Ausgabezeit t (None ohne Shots)."""
        if not self.runs:
            return None
        i = max(0, bisect.bisect_right(self._run_starts, t) - 1)
        return self.runs[i]

    def _select(self, t: float) -> tuple[_Geom, float]:
        if not self.runs:
            return self.geom, 1.0
        i = max(0, bisect.bisect_right(self._run_starts, t) - 1)
        if i != self._cur_run:
            self._cur_run = i
            self._spring, self._spring_target = None, None  # harter Schnitt: Track ohne Nachlauf
        ru = self.runs[i]
        return ru.geom, ru.zoom

    # -- Annotationen

    def _panel_maps(self, geom: _Geom, t: float, shot_zoom: float = 1.0
                    ) -> list[tuple[_Panel, float, tuple[float, float, float, float] | None,
                                    tuple[float, float] | None]]:
        """Je Panel: (Panel, Zoom, dynamisches Quellrechteck oder None, Zoom-Fokus)."""
        out = []
        if geom.tracked:
            win, face = self._window(t, geom.win, geom.horizontal)
            z, focus = self._zoom_for(geom.panels[0].name, t, geom)
            out.append((geom.panels[0], z * shot_zoom, win, face if focus == "face" or geom.kind == "face" else None))
            return out
        for p in geom.panels:
            z, _ = self._zoom_for(p.name, t, geom)
            out.append((p, z * shot_zoom, None, None))
        return out

    def map_box(self, box: Box, maps: list[tuple[_Panel, tuple[float, float, float, float]]] | None = None
                ) -> tuple[tuple[float, float, float, float], tuple[int, int, int, int]] | None:
        """Quell-Box (normalisiert) -> (Rechteck x0, y0, x1, y1 im Ausgabebild, Panel-Zielbereich) für das
        zuletzt komponierte Bild; None, wenn die Box im aktuellen Shot nicht (ausreichend) sichtbar ist."""
        maps = self._maps if maps is None else maps
        bx, by, bw, bh = _box_px(box, self.sw, self.sh)
        best = None
        for p, (zx, zy, zw, zh) in maps:
            ix = max(0.0, min(bx + bw, zx + zw) - max(bx, zx))
            iy = max(0.0, min(by + bh, zy + zh) - max(by, zy))
            vis = ix * iy / max(1e-6, bw * bh)
            if vis < ANN_MIN_VISIBLE:
                continue
            dx, dy, dw, dh = p.dst
            k = dw / zw
            score = (round(vis, 2), k)  # meist sichtbar, dann am stärksten vergrößert (Facecam vor Gameplay)
            if best is None or score > best[0]:
                rect = (dx + (bx - zx) * k, dy + (by - zy) * (dh / zh), dx + (bx + bw - zx) * k,
                        dy + (by + bh - zy) * (dh / zh))
                best = (score, rect, p.dst)
        if best is None:
            return None
        return best[1], best[2]

    def annotation_rect(self, box: Box, t: float) -> tuple[float, float, float, float] | None:
        """Wie map_box, aber für Zeit t ohne zu komponieren (Tests/Diagnose)."""
        geom, sz = self._select(t)
        maps = [(p, _effective_src(p, z, src, foc)) for p, z, src, foc in self._panel_maps(geom, t, sz)]
        m = self.map_box(box, maps)
        return m[0] if m else None

    # -- Frame

    def frame(self, img: np.ndarray, t: float, out: np.ndarray | None = None) -> np.ndarray:
        if img.shape[0] != self.sh or img.shape[1] != self.sw:
            img = cv2.resize(img, (self.sw, self.sh), interpolation=cv2.INTER_AREA)
        if self.grader is not None:
            img = self.grader(img)
        canvas = out if out is not None else np.empty((self.H, self.W, 3), np.uint8)
        geom, shot_zoom = self._select(t)

        if geom.bg is not None:
            bx, by, bw, bh = geom.bg["src"]
            x0, y0 = int(round(bx)), int(round(by))
            crop = img[y0:y0 + int(round(bh)), x0:x0 + int(round(bw))]
            small = cv2.resize(crop, geom.bg["size"], interpolation=cv2.INTER_AREA)
            small = cv2.GaussianBlur(small, (0, 0), BG_SIGMA)
            small = (small.astype(np.float32) * geom.bg["dark"]).astype(np.uint8)
            cv2.resize(small, (self.W, self.H), dst=canvas, interpolation=cv2.INTER_LINEAR)

        maps: list[tuple[_Panel, tuple[float, float, float, float]]] = []
        for p, z, src, foc in self._panel_maps(geom, t, shot_zoom):
            _draw_panel(img, canvas, p, z, src=src, focus=foc)
            if self.annotations:
                maps.append((p, _effective_src(p, z, src, foc)))
        self._maps = maps

        if geom.divider is not None:
            y0, y1 = geom.divider
            canvas[max(0, y0):y1] = self.accent

        for a in self.annotations:
            if a.a.start <= t < a.a.end:
                m = self.map_box(a.a.box)
                if m is not None:
                    a.draw(canvas, m[0], t, m[1])

        # Kamera: Zentrumszoom (gameplay_fit), Punch-Übergang, Wackeln -> eine warpAffine
        zc, _ = self._zoom_for("canvas", t, geom) if geom.kind == "gameplay_fit" else (1.0, "")
        dx = dy = 0.0
        if self.transitions or self.shakes:
            zc *= self._punch(t)
            dx, dy, amp = shake_offset(t, self.shakes, self.W)
            if amp > 0:
                zc *= 1.0 + 2.0 * amp / self.W
        if zc > 1.0005 or abs(dx) > 0.01 or abs(dy) > 0.01:
            m = _zoom_matrix(zc, self.W / 2, self.H / 2)
            m[0, 2] += dx
            m[1, 2] += dy
            canvas = cv2.warpAffine(canvas, m, (self.W, self.H), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)

        if self.transitions or self.flashes:
            self._transition_fx(canvas, t)

        for a in self.overlays:
            _apply_overlay(canvas, a, t)
        return canvas

    def _punch(self, t: float) -> float:
        z = 1.0
        d = self.trans_s * 1.5
        for ts, kind, _ in self.transitions:
            if kind == "punch" and ts <= t < ts + d:
                z *= 1.0 + (PUNCH_FROM - 1.0) * (1.0 - ease_out_cubic((t - ts) / d))
        return z

    def _transition_fx(self, canvas: np.ndarray, t: float) -> None:
        d = self.trans_s
        a = 0.0
        for ts, kind, i in self.transitions:
            if not (ts <= t < ts + d * 1.5):
                continue
            p = (t - ts) / d
            if kind == "glitch" and p < 1.0:
                glitch_frame(canvas, 1.0 - 0.6 * p, seed=i * 1000 + int(round((t - ts) * self.fps)))
            elif kind == "flash" and p < 1.0:
                a = max(a, FLASH_PEAK * (1.0 - p) ** 2)
        for tf in self.flashes:
            u = t - tf
            if 0.0 <= u < d * 1.2:
                a = max(a, FX_FLASH_PEAK * (1.0 - u / (d * 1.2)) ** 2)
        flash_frame(canvas, a)


# --------------------------------------------------------------------------- ffmpeg-Pipes


def _color_matrix(info: dict) -> str:
    vs = _ff.video_stream(info) or {}
    cs = str(vs.get("color_space") or "").lower()
    if cs in ("bt709",):
        return "bt709"
    if cs in ("smpte170m", "bt470bg", "bt601"):
        return "bt601"
    if cs.startswith("bt2020"):
        return "bt2020"
    return "bt709" if int(vs.get("height") or 0) >= 720 else "bt601"


class _Reader:
    """Dekodiert cut_video per ffmpeg zu BGR-Frames (Thread + Queue, entkoppelt vom Komponieren)."""

    def __init__(self, path: Path, w: int, h: int, matrix: str) -> None:
        self.w, self.h = w, h
        cmd = [_ff.FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(path), "-map", "0:v:0",
               "-vf", f"scale={w}:{h}:in_color_matrix={matrix}:flags=bicubic+accurate_rnd+full_chroma_int,"
                      "format=bgr24", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
        try:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, bufsize=0)
        except FileNotFoundError as e:
            raise RenderError("'ffmpeg' nicht gefunden – ist ffmpeg installiert und im PATH?") from e
        self.err = _ff.StderrCollector(self.proc.stderr)
        self.q: queue.Queue = queue.Queue(maxsize=QUEUE_FRAMES)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        size = self.w * self.h * 3
        stream = self.proc.stdout
        try:
            while not self.stop.is_set():
                buf = bytearray(size)
                mv = memoryview(buf)
                got = 0
                while got < size:
                    n = stream.readinto(mv[got:])
                    if not n:
                        break
                    got += n
                if got < size:
                    break
                frame = np.frombuffer(buf, np.uint8).reshape(self.h, self.w, 3)
                while not self.stop.is_set():
                    try:
                        self.q.put(frame, timeout=0.2)
                        break
                    except queue.Full:
                        continue
        except Exception as e:  # pragma: no cover - IO-Fehler
            self._put_final(e)
            return
        self._put_final(None)

    def _put_final(self, item) -> None:
        while not self.stop.is_set():
            try:
                self.q.put(item, timeout=0.2)
                return
            except queue.Full:
                continue

    def get(self, timeout: float | None = None):
        """Nächster Frame, None bei EOF, Exception bei Lesefehler; queue.Empty nach timeout."""
        return self.q.get(timeout=timeout)

    def close(self, graceful: bool) -> int | None:
        """graceful=True: Prozess hat EOF geliefert -> regulär beenden lassen (Exit-Code zählt)."""
        if graceful:
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                pass
        self.stop.set()
        if self.proc.poll() is None:
            self.proc.kill()
        try:
            rc = self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            rc = None
        self.thread.join(timeout=5)
        self.err.join(timeout=2)
        return rc if graceful else None


class _Writer:
    """Schreibt BGR-Frames in einen ffmpeg-Prozess (Captions einbrennen, Audio muxen, encodieren)."""

    def __init__(self, cmd: list[str], cwd: Path) -> None:
        try:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.PIPE, cwd=str(cwd), bufsize=0)
        except FileNotFoundError as e:
            raise RenderError("'ffmpeg' nicht gefunden – ist ffmpeg installiert und im PATH?") from e
        self.err = _ff.StderrCollector(self.proc.stderr)
        self.q: queue.Queue = queue.Queue(maxsize=QUEUE_FRAMES)
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        stdin = self.proc.stdin
        while True:
            item = self.q.get()
            if item is None:
                break
            if self.error is not None:
                continue  # nach Fehler nur noch leeren, damit der Producer nicht blockiert
            try:
                stdin.write(memoryview(item).cast("B"))
            except (BrokenPipeError, OSError, ValueError) as e:
                self.error = e
        try:
            stdin.close()
        except (BrokenPipeError, OSError):
            pass

    def put(self, frame: np.ndarray, timeout: float | None = None) -> None:
        """queue.Full nach timeout (Encoder nimmt keine Frames mehr an)."""
        self.q.put(np.ascontiguousarray(frame), timeout=timeout)

    def finish(self, timeout: float) -> int:
        self.q.put(None)
        self.thread.join(timeout=timeout)
        try:
            rc = self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            rc = self.proc.wait(timeout=10)
        self.err.join(timeout=5)
        return rc

    def kill(self) -> None:
        """Abbruch: Prozess beenden, Queue leeren, Thread sauber auslaufen lassen (kein Thread-Leck im 24/7-Betrieb)."""
        if self.proc.poll() is None:
            self.proc.kill()
        while True:
            try:
                self.q.get_nowait()
            except queue.Empty:
                break
        try:
            self.q.put_nowait(None)
        except queue.Full:
            pass
        self.thread.join(timeout=5)
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        self.err.join(timeout=2)


_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


def _hint(stderr: str) -> str:
    """Bekannte ffmpeg-Probleme in eine verständliche Handlungsanweisung übersetzen."""
    low = stderr.lower()
    if "no such filter: 'ass'" in low or "no such filter: \"ass\"" in low:
        return (" – dieses ffmpeg wurde ohne libass gebaut, Captions können nicht eingebrannt werden. "
                "Bitte ein ffmpeg mit --enable-libass installieren (z. B. das Debian/Ubuntu-Paket 'ffmpeg').")
    if "unknown encoder" in low or "encoder not found" in low:
        return " – der Encoder fehlt in diesem ffmpeg-Build (render.encoder auf 'x264' bzw. 'auto' stellen)."
    if "nvenc" in low or "cuda" in low or "nvcuda" in low:
        return " – NVENC/GPU nicht nutzbar (Treiber/NVIDIA Container Toolkit prüfen oder render.encoder: x264)."
    return ""


def _prepare_ass(ass_path: Path, settings: Settings) -> tuple[Path, str]:
    """ASS + Schriften so ablegen, dass der ass-Filter nur einfache relative Namen braucht
    (Filter-Escaping von Pfaden ist unter Windows/mit Doppelpunkten fehleranfällig)."""
    ass_path = Path(ass_path).resolve()
    d = ass_path.parent
    name = ass_path.name
    if not _SAFE_NAME.match(name):
        name = "captions_render.ass"
        shutil.copyfile(ass_path, d / name)
    fonts_local = d / "fonts"
    fonts_local.mkdir(exist_ok=True)
    src_dir = settings.config.paths.fonts_dir
    for fn in {settings.config.brand.font_captions, settings.config.brand.font_hook}:
        src = Path(src_dir) / fn
        dst = fonts_local / Path(fn).name
        try:
            if src.is_file() and (not dst.is_file() or dst.stat().st_size != src.stat().st_size):
                shutil.copyfile(src, dst)
        except OSError as e:
            log.warning("Cannot copy font %s: %s", src, e)
    return d, f"ass=filename={name}:fontsdir=fonts"


def compose_video(cut_video: Path, spec: CompositionSpec, settings: Settings, ass_path: Path | None,
                  audio_wav: Path, out_mp4: Path, encoder: str) -> Path:
    """Liest cut_video (bereits geschnitten, CFR render.fps) Frame für Frame, komponiert, pipet BGR nach
    ffmpeg (-f rawvideo -pix_fmt bgr24), brennt ASS ein (ass-Filter mit fontsdir=assets/fonts), muxt
    audio_wav, encodiert mit encoder. Gibt out_mp4 zurück."""
    r = settings.config.render
    W, H, fps = int(r.width), int(r.height), int(r.fps)
    cut_video, audio_wav, out_mp4 = Path(cut_video), Path(audio_wav), Path(out_mp4)
    if not audio_wav.is_file():
        raise RenderError(f"Audiodatei fehlt: {audio_wav}")
    info = _ff.probe(cut_video)
    vs = _ff.video_stream(info)
    if vs is None:
        raise RenderError(f"Keine Videospur in {cut_video.name}")
    sw, sh = int(vs.get("width") or 0), int(vs.get("height") or 0)
    if sw <= 0 or sh <= 0:
        raise RenderError(f"Ungültige Auflösung in {cut_video.name}: {sw}x{sh}")
    sw, sh = sw - sw % 2, sh - sh % 2

    composer = Composer(spec, settings, sw, sh, fps)
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    part = out_mp4.with_name(out_mp4.stem + ".part.mp4")

    vf: list[str] = []
    cwd = out_mp4.parent.resolve()
    if ass_path is not None and Path(ass_path).is_file():
        cwd, ass_filter = _prepare_ass(Path(ass_path), settings)
        vf.append(ass_filter)
    vf += ["scale=out_color_matrix=bt709:out_range=tv:flags=bicubic+accurate_rnd+full_chroma_int",
           "format=yuv420p"]
    cmd = [_ff.FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}", "-r", str(fps), "-i", "-",
           "-i", str(audio_wav.resolve()), "-map", "0:v:0", "-map", "1:a:0", "-vf", ",".join(vf),
           *encoder_mod.video_codec_args(encoder, r),
           "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv",
           "-c:a", "aac", "-b:a", str(r.audio_bitrate), "-ar", "48000", "-shortest", str(part.resolve())]

    t0 = time.perf_counter()
    # Frist gegen hängende ffmpeg-Prozesse (z. B. NVENC-Treiber): ohne sie blockiert der Process-Job für immer
    budget = max(MIN_DEADLINE_S, (_ff.duration(info) or 0.0) * DEADLINE_PER_S)
    deadline = time.monotonic() + budget

    def overdue() -> RenderError:
        return RenderError(f"Komposition von {cut_video.name} dauert länger als {budget:.0f} s "
                           f"(nach {frames} Frames, {encoder}) – abgebrochen")

    def wait_s() -> float:
        left = deadline - time.monotonic()
        if left <= 0:
            raise overdue()
        return min(STALL_TIMEOUT_S, left)

    def stalled(what: str) -> RenderError:
        if time.monotonic() >= deadline:
            return overdue()
        return RenderError(f"{what} seit {STALL_TIMEOUT_S:.0f} s ohne Fortschritt (nach {frames} Frames, "
                           f"{encoder}) – ffmpeg hängt, abgebrochen")

    reader = _Reader(cut_video, sw, sh, _color_matrix(info))
    writer = _Writer(cmd, cwd)
    frames, comp_s, eof = 0, 0.0, False
    try:
        while True:
            try:
                item = reader.get(timeout=wait_s())
            except queue.Empty:
                raise stalled(f"Dekodieren von {cut_video.name}") from None
            if item is None:
                eof = True
                break
            if isinstance(item, BaseException):
                raise RenderError(f"Lesen von {cut_video.name} fehlgeschlagen: {item}")
            tc = time.perf_counter()
            canvas = composer.frame(item, frames / fps)
            comp_s += time.perf_counter() - tc
            if writer.error is not None or writer.proc.poll() is not None:
                break  # Encoder ist abgestürzt -> Fehler unten mit stderr-Auszug
            try:
                writer.put(canvas, timeout=wait_s())
            except queue.Full:
                raise stalled("Video-Encoding") from None
            frames += 1
    except BaseException as e:
        writer.kill()
        reader.close(graceful=False)
        part.unlink(missing_ok=True)
        if isinstance(e, RenderError | KeyboardInterrupt):
            raise
        raise RenderError(f"Komposition fehlgeschlagen bei Frame {frames}: {e}") from e

    rc_reader = reader.close(graceful=eof)
    rc = writer.finish(timeout=max(120.0, frames / max(1, fps) * 20))
    if writer.error is not None or rc != 0:
        part.unlink(missing_ok=True)
        detail = writer.err.text() or (f"{type(writer.error).__name__}" if writer.error else f"Exit-Code {rc}")
        raise RenderError(f"Video-Encoding fehlgeschlagen ({encoder}): {detail}{_hint(detail)}")
    if frames == 0:
        part.unlink(missing_ok=True)
        detail = reader.err.text() or f"Exit-Code {rc_reader}"
        raise RenderError(f"Keine Videoframes aus {cut_video.name} gelesen: {detail}")
    if rc_reader not in (0, None):
        part.unlink(missing_ok=True)
        raise RenderError(f"Dekodieren von {cut_video.name} abgebrochen nach {frames} Frames: "
                          f"{reader.err.text() or f'Exit-Code {rc_reader}'}")
    os.replace(part, out_mp4)
    total = time.perf_counter() - t0
    log.info("Composed %d frames (%s, %s) in %.1f s – %.1f ms/frame compose", frames, composer.kind, encoder,
             total, comp_s / frames * 1000)
    return out_mp4
