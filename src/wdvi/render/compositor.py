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
"""

from __future__ import annotations

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

from wdvi.config import Settings
from wdvi.render import _ff
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
BG_DARKEN = 0.42
BG_SIGMA = 7.0  # Weichzeichner auf 1/4 Auflösung (~28 px bei voller Auflösung)
SPRING_OMEGA = 3.2  # Face-Track-Feder (rad/s): ~1 s Einschwingzeit, kein Überschwingen
TRACK_DEADZONE = 0.025  # Anteil der Quellbreite; kleinere Bewegungen werden ignoriert
NATIVE_COVER_MAX_ASPECT = 0.8  # breitere Hochkant-/Quadratquellen -> Fit statt Cover (sonst zu viel Beschnitt)
QUEUE_FRAMES = 6
STALL_TIMEOUT_S = 120.0  # so lange ohne Fortschritt (kein Frame gelesen/angenommen) -> ffmpeg hängt
MIN_DEADLINE_S = 300.0  # Gesamtfrist der Komposition: max(MIN_DEADLINE_S, Dauer * DEADLINE_PER_S)
DEADLINE_PER_S = 30.0


@dataclass
class TimedOverlay:
    png: Path
    start: float  # Ausgabezeit
    end: float
    x: int  # linke obere Ecke im Ausgabebild (vor Pop-Skalierung, Pop skaliert um die Mitte)
    y: int
    pop: bool = True
    fade_out: float = 0.15


@dataclass
class ZoomOut:
    start: float  # Ausgabezeit
    end: float
    scale: float
    focus: str


@dataclass
class CompositionSpec:
    layout: LayoutHint
    zooms: list[ZoomOut] = field(default_factory=list)
    overlays: list[TimedOverlay] = field(default_factory=list)


# --------------------------------------------------------------------------- Mathe-Helfer


def ease_in_out(x: float) -> float:
    x = min(1.0, max(0.0, x))
    return x * x * (3.0 - 2.0 * x)


def ease_out_back(x: float, s: float = 1.70158) -> float:
    x = min(1.0, max(0.0, x)) - 1.0
    return 1.0 + (s + 1.0) * x ** 3 + s * x ** 2


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


# --------------------------------------------------------------------------- Panels


@dataclass
class _Panel:
    name: str  # "face" | "gameplay" | "fg" | "full"
    dst: tuple[int, int, int, int]  # x, y, w, h im Ausgabebild
    src: tuple[float, float, float, float]  # Quellrechteck (px), gleiches Seitenverhältnis wie dst
    focus: tuple[float, float] | None = None  # Zoom-Fokus in Quell-px (None = Mitte)

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


def _draw_panel(img: np.ndarray, canvas: np.ndarray, p: _Panel, zoom: float = 1.0,
                src: tuple[float, float, float, float] | None = None, focus: tuple[float, float] | None = None) -> None:
    """Quellrechteck -> Zielregion (eine Resampling-Stufe; Zoom/Subpixel über warpAffine)."""
    dx, dy, dw, dh = p.dst
    region = canvas[dy:dy + dh, dx:dx + dw]
    sx, sy, sw, sh = src if src is not None else p.src
    dynamic = src is not None
    if zoom <= 1.0005 and not dynamic:
        x0, y0, w0, h0 = p.int_crop()
        crop = img[y0:y0 + h0, x0:x0 + w0]
        cv2.resize(crop, (dw, dh), dst=region, interpolation=_resize_interp(dw / w0))
        return
    fx, fy = focus if focus is not None else (p.focus or (sx + sw / 2, sy + sh / 2))
    fx, fy = min(max(fx, sx), sx + sw), min(max(fy, sy), sy + sh)
    zx, zy = fx + (sx - fx) / zoom, fy + (sy - fy) / zoom  # Zoom um den Fokuspunkt (bleibt stehen)
    kx, ky = dw / (sw / zoom), dh / (sh / zoom)
    if kx >= 0.8:
        # Pixelmitten-Konvention wie cv2.resize (sonst springt das Bild beim Zoomstart um ~0.5 px)
        m = np.float32([[kx, 0, kx * (0.5 - zx) - 0.5], [0, ky, ky * (0.5 - zy) - 0.5]])
        cv2.warpAffine(img, m, (dw, dh), dst=region, flags=_warp_interp(kx), borderMode=cv2.BORDER_REPLICATE)
        return
    # starke Verkleinerung: erst sauber (INTER_AREA) aufs Panel, dann dort zoomen
    x0, y0, w0, h0 = p.int_crop()
    base = cv2.resize(img[y0:y0 + h0, x0:x0 + w0], (dw, dh), interpolation=cv2.INTER_AREA)
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
    if ov.pop and dt < POP_S:
        p = dt / POP_S
        if ov.start <= 0.001:  # Overlay ab Frame 0: voll deckend (TikTok nimmt Frame 0 als Cover)
            s = 0.94 + 0.06 * ease_out_back(p)
        else:
            s = POP_FROM + (1.0 - POP_FROM) * ease_out_back(p)
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


# --------------------------------------------------------------------------- Komposition


class Composer:
    """Baut aus einem Quellframe (BGR) das fertige Ausgabebild (ohne Captions – die macht libass)."""

    def __init__(self, spec: CompositionSpec, settings: Settings, src_w: int, src_h: int, fps: float) -> None:
        r = settings.config.render
        self.W, self.H, self.fps = int(r.width), int(r.height), float(fps)
        self.sw, self.sh = int(src_w), int(src_h)
        self.accent = _hex_bgr(settings.config.brand.color_accent)
        hint = spec.layout
        kind = hint.kind
        if kind == "facecam_split" and hint.facecam_box is None:
            log.info("facecam_split without facecam_box -> gameplay_fit")
            kind = "gameplay_fit"
        self.fit_zoom = FG_ZOOM
        aspect = self.sw / self.sh
        if kind == "vertical_native" and aspect > NATIVE_COVER_MAX_ASPECT:
            kind, self.fit_zoom = "gameplay_fit", 1.0  # quadratisch/breit: nichts abschneiden
        elif kind == "gameplay_fit" and aspect <= NATIVE_COVER_MAX_ASPECT:
            kind = "vertical_native"  # Quelle ist schon hochkant -> Cover
        self.kind = kind
        self.zooms = [z for z in spec.zooms if z.end - z.start > 0.05 and z.scale > 1.001]
        self.panels: list[_Panel] = []
        self.divider: tuple[int, int] | None = None
        self._track: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None
        self._spring: list[float] | None = None
        self._spring_target: float | None = None
        self._bg: dict | None = None
        getattr(self, f"_setup_{kind}")(hint, settings)
        self.overlays = [a for a in (_load_overlay(o) for o in spec.overlays if o.end > o.start) if a is not None]

    # -- Setup je Layout

    def _setup_facecam_split(self, hint: LayoutHint, settings: Settings) -> None:
        top_h = int(round(settings.config.render.split_facecam_ratio * self.H))
        top_h = min(max(top_h, self.H // 5), self.H - self.H // 5)
        bx, by, bw, bh = _box_px(hint.facecam_box, self.sw, self.sh)  # type: ignore[arg-type]
        bx, by, bw, bh = bx + bw * FACECAM_INSET, by + bh * FACECAM_INSET, bw * (1 - 2 * FACECAM_INSET), bh * (
            1 - 2 * FACECAM_INSET)
        face_src = cover_rect(bx, by, bw, bh, self.W / top_h)
        gx, gy, gw, gh = _box_px(hint.gameplay_box or Box(x=0, y=0, w=1, h=1), self.sw, self.sh)
        game_src = cover_rect(gx, gy, gw, gh, self.W / (self.H - top_h))
        self.panels = [_Panel("face", (0, 0, self.W, top_h), face_src),
                       _Panel("gameplay", (0, top_h, self.W, self.H - top_h), game_src)]
        self.divider = (top_h - DIVIDER_PX // 2, top_h - DIVIDER_PX // 2 + DIVIDER_PX)

    def _setup_face_track(self, hint: LayoutHint, settings: Settings) -> None:
        aspect = self.W / self.H
        self.win = cover_rect(0, 0, self.sw, self.sh, aspect)
        self.horizontal = self.sw / self.sh > aspect
        pts = sorted(hint.track, key=lambda p: p.t)
        if pts:
            self._track = (np.array([p.t for p in pts], np.float64), np.array([p.cx for p in pts], np.float64),
                           np.array([p.cy for p in pts], np.float64))
        self.panels = [_Panel("full", (0, 0, self.W, self.H), self.win)]

    def _setup_gameplay_fit(self, hint: LayoutHint, settings: Settings) -> None:
        W, H = self.W, self.H
        z = self.fit_zoom
        fg_h = int(round(W * z * self.sh / self.sw))
        fg_h = min(fg_h - fg_h % 2, H)
        src_w = self.sw / z
        fg_src = cover_rect((self.sw - src_w) / 2, 0, src_w, self.sh, W / fg_h)
        fg_y = (H - fg_h) // 2
        self.panels = [_Panel("fg", (0, fg_y, W, fg_h), fg_src)]
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
        self._bg = {"src": cover_rect(0, 0, self.sw, self.sh, W / H), "size": (sw4, sh4), "dark": dark}

    def _setup_vertical_native(self, hint: LayoutHint, settings: Settings) -> None:
        self.panels = [_Panel("full", (0, 0, self.W, self.H), cover_rect(0, 0, self.sw, self.sh, self.W / self.H))]

    # -- Zoom-Zuordnung

    def _targets(self, focus: str) -> tuple[str, ...]:
        if self.kind == "facecam_split":
            return {"face": ("face",), "gameplay": ("gameplay",)}.get(focus, ("face", "gameplay"))
        if self.kind == "gameplay_fit":
            return ("canvas",) if focus == "center" else ("fg",)
        return ("full",)

    def _zoom_for(self, target: str, t: float) -> tuple[float, str]:
        best, foc = 1.0, "center"
        for z in self.zooms:
            if target in self._targets(z.focus):
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

    def _window(self, t: float) -> tuple[tuple[float, float, float, float], tuple[float, float] | None]:
        wx, wy, ww, wh = self.win
        pt = self._track_at(t)
        if pt is None:
            return self.win, None
        fx, fy = pt[0] * self.sw, pt[1] * self.sh
        raw = fx if self.horizontal else fy
        dead = TRACK_DEADZONE * (self.sw if self.horizontal else self.sh)
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
        if self.horizontal:
            wx = min(max(c - ww / 2, 0.0), self.sw - ww)
            fx = c  # Zoom-Fokus ebenfalls geglättet (kein Zittern beim Gesichts-Zoom)
        else:
            wy = min(max(c - wh / 2, 0.0), self.sh - wh)
            fy = c
        return (wx, wy, ww, wh), (fx, fy)

    # -- Frame

    def frame(self, img: np.ndarray, t: float, out: np.ndarray | None = None) -> np.ndarray:
        if img.shape[0] != self.sh or img.shape[1] != self.sw:
            img = cv2.resize(img, (self.sw, self.sh), interpolation=cv2.INTER_AREA)
        canvas = out if out is not None else np.empty((self.H, self.W, 3), np.uint8)

        if self._bg is not None:
            bx, by, bw, bh = self._bg["src"]
            x0, y0 = int(round(bx)), int(round(by))
            crop = img[y0:y0 + int(round(bh)), x0:x0 + int(round(bw))]
            small = cv2.resize(crop, self._bg["size"], interpolation=cv2.INTER_AREA)
            small = cv2.GaussianBlur(small, (0, 0), BG_SIGMA)
            small = (small.astype(np.float32) * self._bg["dark"]).astype(np.uint8)
            cv2.resize(small, (self.W, self.H), dst=canvas, interpolation=cv2.INTER_LINEAR)

        if self.kind == "face_track":
            win, face = self._window(t)
            z, focus = self._zoom_for("full", t)
            _draw_panel(img, canvas, self.panels[0], z, src=win, focus=face if focus == "face" else None)
        else:
            for p in self.panels:
                z, _ = self._zoom_for(p.name, t)
                _draw_panel(img, canvas, p, z)

        if self.divider is not None:
            y0, y1 = self.divider
            canvas[max(0, y0):y1] = self.accent

        zc, _ = self._zoom_for("canvas", t) if self.kind == "gameplay_fit" else (1.0, "")
        if zc > 1.0005:
            canvas = cv2.warpAffine(canvas, _zoom_matrix(zc, self.W / 2, self.H / 2), (self.W, self.H),
                                    flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)

        for a in self.overlays:
            _apply_overlay(canvas, a, t)
        return canvas


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
