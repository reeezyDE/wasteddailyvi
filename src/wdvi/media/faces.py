"""Gesichtserkennung (OpenCV YuNet, Fallback Haar) und Layout-Entscheidung 16:9 -> 9:16."""

from __future__ import annotations

import logging
import math
import threading
import time
from pathlib import Path
from statistics import median
from typing import Literal

import cv2
import httpx
import numpy as np

from wdvi.config import Settings
from wdvi.media import _ff
from wdvi.schemas import Box, FaceDet, LayoutHint, MediaInfo, TrackPoint

log = logging.getLogger(__name__)

YUNET_FILE = "face_detection_yunet_2023mar.onnx"
YUNET_URLS = (
    "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/" + YUNET_FILE,
    "https://huggingface.co/opencv/face_detection_yunet/resolve/main/" + YUNET_FILE,
)
HAAR_FILE = "haarcascade_frontalface_default.xml"
MIN_MODEL_BYTES = 100_000  # echtes Modell ~230 kB; Git-LFS-Pointer nur ~130 Bytes
DOWNLOAD_RETRY_S = 3600.0

DETECT_WIDTH = 640  # Erkennung auf verkleinerter Kopie
SCORE_THRESHOLD = 0.6
HAAR_SCORE = 0.6  # Haar liefert keine Konfidenz
MIN_FACE_W = 0.025  # kleinere Boxen (Anteil der Bildbreite) sind fast immer Fehltreffer

# Layout-Regeln (alle Werte normalisiert auf Bildbreite/-höhe)
FACECAM_MAX_FACE_W = 0.22  # kleiner = Facecam-Gesicht, ab hier "großes Gesicht" (IRL/Kamera)
FACECAM_MIN_SHARE = 0.40  # Anteil der Samples mit Gesicht an (fast) gleicher Stelle
FACECAM_CLUSTER_TOL = 0.06
TRACK_MIN_SHARE = 0.50
CAM_W_FACTOR = 3.0  # Webcam-Rahmen ≈ 3x Gesichtsbreite
CAM_W_MIN, CAM_W_MAX = 0.16, 0.42
EDGE_SNAP = 0.08
GAMEPLAY_MARGIN = 0.02
TRACK_OUTLIER = 0.20
TRACK_MIN_POINTS = 2  # Gesichts-Track auch ohne face_track-Layout, wenn mind. so viele stabile Punkte da sind

# Freigestellte Facecam ("cutout": Greenscreen-Streamer sitzt ohne Rahmen über dem Gameplay)
CUTOUT_MIN_FACE_H = 0.18  # Gesichtshöhe (Anteil Bildhöhe) ab der kein kleiner Webcam-Rahmen mehr passt
CUTOUT_W_FACTOR = 2.6  # Kopf + Schultern ≈ 2,6x Gesichtsbreite
CUTOUT_H_FACTOR = 2.2  # ≈ 2,2x Gesichtshöhe, überwiegend nach unten (Oberkörper)
CUTOUT_HEADROOM = 0.35  # Platz über der Stirn in Gesichtshöhen
CUTOUT_BOTTOM_SNAP = 0.25  # Oberkörper reicht bis zum unteren Bildrand, wenn der Rahmen so nah dran ist
CUTOUT_W_MAX = 0.60
CUTOUT_TOL_FACTOR = 0.5  # freigestellte Streamer bewegen sich mehr -> Cluster-Toleranz ~ halbe Gesichtsbreite

CamStyle = Literal["frame", "cutout", "none"]  # Webcam-Rahmen | freigestellter Streamer | keine Facecam

_download_lock = threading.Lock()
_download_failed_at: float | None = None


# --------------------------------------------------------------------------- Modelle


def find_yunet_model(assets_dir: Path, cache_dir: Path) -> Path | None:
    """Gleiche Suchreihenfolge wie PathsConfig.face_model_paths (doctor, 'wdvi init')."""
    for p in (assets_dir / "models" / YUNET_FILE, cache_dir / "models" / YUNET_FILE, cache_dir / YUNET_FILE):
        try:
            if p.is_file() and p.stat().st_size >= MIN_MODEL_BYTES:
                return p
        except OSError:
            continue
    return None


def download_yunet(dest_dir: Path) -> Path | None:
    """Lädt das YuNet-ONNX nach dest_dir. None bei Fehler (dann 1 h kein neuer Versuch in diesem Prozess)."""
    global _download_failed_at
    with _download_lock:
        if _download_failed_at is not None and time.monotonic() - _download_failed_at < DOWNLOAD_RETRY_S:
            return None
        dest = dest_dir / YUNET_FILE
        tmp = dest_dir / (YUNET_FILE + ".part")
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log.warning("Cannot create model cache dir %s: %s", dest_dir, e)
            return None
        for url in YUNET_URLS:
            try:
                with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30.0, connect=10.0)) as client:
                    with client.stream("GET", url) as r:
                        if r.status_code != 200:
                            log.info("YuNet download from %s: HTTP %s", url, r.status_code)
                            continue
                        with tmp.open("wb") as fh:
                            for chunk in r.iter_bytes():
                                fh.write(chunk)
                size = tmp.stat().st_size
                with tmp.open("rb") as fh:
                    head = fh.read(64)
                if size < MIN_MODEL_BYTES or head.startswith(b"version https://git-lfs"):
                    log.info("YuNet download from %s returned no real model (%d bytes)", url, size)
                    tmp.unlink(missing_ok=True)
                    continue
                tmp.replace(dest)
                log.info("Downloaded YuNet face model (%d kB) to %s", size // 1024, dest)
                return dest
            except (httpx.HTTPError, OSError) as e:
                log.info("YuNet download from %s failed: %s", url, e)
                tmp.unlink(missing_ok=True)
        _download_failed_at = time.monotonic()
        log.warning("YuNet face model unavailable - falling back to Haar cascade (less accurate). "
                    "Run 'wdvi init' or place %s in %s to fix.", YUNET_FILE, dest_dir)
        return None


def _load_haar(assets_dir: Path) -> cv2.CascadeClassifier | None:
    candidates = [assets_dir / "models" / HAAR_FILE]
    data_dir = getattr(getattr(cv2, "data", None), "haarcascades", None)
    if data_dir:
        candidates.append(Path(data_dir) / HAAR_FILE)
    for p in candidates:
        if p.is_file():
            clf = cv2.CascadeClassifier(str(p))
            if not clf.empty():
                return clf
    return None


# --------------------------------------------------------------------------- Detektor


class FaceDetector:
    def __init__(self, settings: Settings, *, allow_download: bool = True, model_path: str | Path | None = None) -> None:
        """Lädt YuNet-ONNX aus cache_dir (lädt es bei Bedarf von opencv_zoo); sonst Haar-Cascade."""
        self.settings = settings
        self.backend = "none"
        self._lock = threading.Lock()  # cv2-Detektoren sind nicht thread-sicher
        self._yunet = None
        self._haar: cv2.CascadeClassifier | None = None
        paths = settings.config.paths
        path = Path(model_path) if model_path else find_yunet_model(paths.assets_dir, paths.cache_dir)
        if path is None and allow_download:
            path = download_yunet(paths.models_dir)
        if path is not None:
            try:
                self._yunet = cv2.FaceDetectorYN.create(str(path), "", (DETECT_WIDTH, 360), SCORE_THRESHOLD, 0.3, 5000)
                self.backend = "yunet"
            except cv2.error as e:
                log.warning("YuNet model %s unusable (%s) - using Haar cascade", path, str(e).strip()[:200])
                if paths.cache_dir.resolve() in path.resolve().parents:  # kaputter Download -> neu laden
                    path.unlink(missing_ok=True)
        if self._yunet is None:
            self._haar = _load_haar(paths.assets_dir)
            if self._haar is not None:
                self.backend = "haar"
            else:
                log.error("Keine Gesichtserkennung verfügbar: weder YuNet noch %s gefunden – "
                          "Layout fällt immer auf gameplay_fit zurück.", HAAR_FILE)

    def detect(self, img_bgr: np.ndarray) -> list[FaceDet]:
        if img_bgr is None or getattr(img_bgr, "size", 0) == 0:
            return []
        img = img_bgr
        if img.ndim == 2:
            img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
        elif img.shape[2] == 4:
            img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
        h, w = img.shape[:2]
        if w > DETECT_WIDTH:
            img = cv2.resize(img, (DETECT_WIDTH, max(1, round(h * DETECT_WIDTH / w))), interpolation=cv2.INTER_AREA)
        sh, sw = img.shape[:2]
        raw: list[tuple[float, float, float, float, float]] = []
        with self._lock:
            if self._yunet is not None:
                self._yunet.setInputSize((sw, sh))
                _, faces = self._yunet.detect(img)
                if faces is not None:
                    raw = [(float(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[-1])) for r in faces]
            elif self._haar is not None:
                gray = cv2.equalizeHist(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
                min_px = max(20, round(sw * MIN_FACE_W))
                rects = self._haar.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(min_px, min_px))
                raw = [(float(x), float(y), float(bw), float(bh), HAAR_SCORE) for x, y, bw, bh in rects]
        return normalize_detections(raw, sw, sh)


def normalize_detections(raw: list[tuple[float, float, float, float, float]], width: int, height: int) -> list[FaceDet]:
    """Pixel-Boxen (x, y, w, h, score) -> normalisierte, ans Bild geklemmte FaceDets (gefiltert, nach Score)."""
    out: list[FaceDet] = []
    for x, y, bw, bh, score in raw:
        if score < SCORE_THRESHOLD:
            continue
        x0, y0 = max(0.0, x / width), max(0.0, y / height)
        x1, y1 = min(1.0, (x + bw) / width), min(1.0, (y + bh) / height)
        if x1 - x0 < MIN_FACE_W or y1 <= y0:
            continue
        out.append(FaceDet(box=Box(x=round(x0, 4), y=round(y0, 4), w=round(x1 - x0, 4), h=round(y1 - y0, 4)),
                           score=round(min(1.0, score), 4)))
    out.sort(key=lambda f: (-f.score, -f.box.w * f.box.h))
    return out


# --------------------------------------------------------------------------- Layout (rein, testbar)


def _facecam_cluster(per_sample: list[tuple[float, list[FaceDet]]]) -> dict[int, FaceDet]:
    """Größte Gruppe kleiner Gesichter an (fast) gleicher Stelle: Sample-Index -> Gesicht."""
    small = [(i, f) for i, (_, faces) in enumerate(per_sample) for f in faces if f.box.w < FACECAM_MAX_FACE_W]
    if not small:
        return {}

    def supporters(cx: float, cy: float, fw: float) -> dict[int, FaceDet]:
        tol = max(FACECAM_CLUSTER_TOL, CUTOUT_TOL_FACTOR * fw)
        res: dict[int, FaceDet] = {}
        for i, f in small:
            dx, dy = abs(f.box.cx - cx), abs(f.box.cy - cy)
            if dx <= tol and dy <= tol and 0.5 * fw <= f.box.w <= 2.0 * fw:  # ähnliche Größe
                prev = res.get(i)
                if prev is None or math.hypot(dx, dy) < math.hypot(prev.box.cx - cx, prev.box.cy - cy):
                    res[i] = f
        return res

    best = max((supporters(f.box.cx, f.box.cy, f.box.w) for _, f in small), key=len)
    # einmal um den Median nachzentrieren (stabiler als ein zufälliges Start-Gesicht)
    refined = supporters(median(f.box.cx for f in best.values()), median(f.box.cy for f in best.values()),
                         median(f.box.w for f in best.values()))
    return refined if len(refined) >= len(best) else best


def _snap(start: float, size: float, face_lo: float, face_hi: float) -> tuple[float, float]:
    """Rahmen an den Bildrand ziehen, wenn er näher als EDGE_SNAP dran ist (Webcams kleben am Rand).

    Verschieben, solange das Gesicht ganz drin bleibt, sonst den Rahmen bis zum Rand verlängern.
    Gibt (start, size) zurück, geklemmt auf [0, 1].
    """
    end = start + size
    if start < EDGE_SNAP:
        start, end = (0.0, size) if face_hi <= size else (0.0, end)
    elif 1.0 - end < EDGE_SNAP:
        start, end = (1.0 - size, 1.0) if face_lo >= 1.0 - size else (start, 1.0)
    start, end = max(0.0, start), min(1.0, end)
    return start, max(0.0, end - start)


def cam_style(members: list[FaceDet]) -> CamStyle:
    """'cutout', wenn das Facecam-Gesicht zu groß für einen kleinen Webcam-Rahmen ist (freigestellter
    Streamer über dem Gameplay), sonst 'frame'. Ohne Gesichter 'none'."""
    if not members:
        return "none"
    return "cutout" if median(f.box.h for f in members) >= CUTOUT_MIN_FACE_H else "frame"


def _cutout_box(fx: float, fy: float, fw: float, fh: float) -> Box:
    """Kopf + Schultern eines freigestellten Streamers: ~2,6x Gesichtsbreite, ~2,2x Gesichtshöhe, wenig Platz
    über dem Kopf, der Rest nach unten (Oberkörper); an Bildränder gezogen bzw. geklemmt."""
    w = min(max(fw * CUTOUT_W_FACTOR, CAM_W_MIN), CUTOUT_W_MAX)
    h = min(1.0, fh * CUTOUT_H_FACTOR)
    x0, w = _snap(fx - w / 2, w, fx - fw / 2, fx + fw / 2)
    y0 = max(0.0, fy - fh / 2 - fh * CUTOUT_HEADROOM)
    if y0 < EDGE_SNAP:
        y0 = 0.0
    y1 = min(1.0, y0 + h)
    if 1.0 - y1 < CUTOUT_BOTTOM_SNAP:  # Oberkörper geht bis zum unteren Rand weiter
        y1 = 1.0
    return Box(x=round(x0, 4), y=round(y0, 4), w=round(w, 4), h=round(y1 - y0, 4))


def _facecam_box(members: list[FaceDet], info: MediaInfo) -> Box:
    fx = median(f.box.cx for f in members)
    fy = median(f.box.cy for f in members)
    fw = median(f.box.w for f in members)
    fh = median(f.box.h for f in members)
    if cam_style(members) == "cutout":
        return _cutout_box(fx, fy, fw, fh)
    cam_w = min(max(fw * CAM_W_FACTOR, CAM_W_MIN), CAM_W_MAX)
    cam_h = min(1.0, cam_w * (info.width / info.height) * 9.0 / 16.0)  # 16:9 in Pixeln
    x0, cam_w = _snap(fx - cam_w / 2, cam_w, fx - fw / 2, fx + fw / 2)
    y0, cam_h = _snap(fy - cam_h / 2, cam_h, fy - fh / 2, fy + fh / 2)
    return Box(x=round(x0, 4), y=round(y0, 4), w=round(cam_w, 4), h=round(cam_h, 4))


def _gameplay_box() -> Box:
    """Gameplay-Panel = ganzes Bild minus schmalem Rand (Stream-Overlays).

    Bewusst simpel und robust: Der Compositor schneidet daraus per cover-crop ohnehin nur die Bildmitte
    (Panel ist etwa quadratisch), dort ist bei GTA die Action; die Facecam-Ecke fällt dabei größtenteils weg.
    Ein seitlich verschobener Ausschnitt würde dagegen die Spielfigur aus der Mitte rücken.
    """
    m = GAMEPLAY_MARGIN
    return Box(x=m, y=m, w=1.0 - 2 * m, h=1.0 - 2 * m)


def _large_face_points(per_sample: list[tuple[float, list[FaceDet]]]) -> list[tuple[float, float, float]]:
    """(t, cx, cy) des großen Gesichts je Sample; bei mehreren das nächstgelegene zum vorherigen."""
    pts: list[tuple[float, float, float]] = []
    for t, faces in sorted(per_sample, key=lambda s: s[0]):
        large = [f for f in faces if f.box.w >= FACECAM_MAX_FACE_W]
        if not large:
            continue
        if pts:
            px, py = pts[-1][1], pts[-1][2]
            f = min(large, key=lambda f: math.hypot(f.box.cx - px, f.box.cy - py))
        else:
            f = max(large, key=lambda f: f.box.w * f.box.h * f.score)
        pts.append((t, f.box.cx, f.box.cy))
    return pts


def _smooth_track(pts: list[tuple[float, float, float]]) -> list[TrackPoint]:
    """Lokale Ausreißer entfernen (Vergleich mit Nachbar-Median), dann gleitender Median über 3 Punkte."""
    keep: list[tuple[float, float, float]] = []
    for i, (t, x, y) in enumerate(pts):
        nb = pts[max(0, i - 2):i] + pts[i + 1:i + 3]
        if len(nb) >= 2:
            mx, my = median(p[1] for p in nb), median(p[2] for p in nb)
            if abs(x - mx) > TRACK_OUTLIER or abs(y - my) > TRACK_OUTLIER:
                continue
        keep.append((t, x, y))
    keep = keep or pts
    out: list[TrackPoint] = []
    for i, (t, _, _) in enumerate(keep):
        win = keep[max(0, i - 1):i + 2]
        cx = min(1.0, max(0.0, median(p[1] for p in win)))
        cy = min(1.0, max(0.0, median(p[2] for p in win)))
        out.append(TrackPoint(t=round(t, 3), cx=round(cx, 4), cy=round(cy, 4)))
    return out


def _cam_points(per_sample: list[tuple[float, list[FaceDet]]], cam: dict[int, FaceDet]) -> list[tuple[float, float, float]]:
    return sorted((per_sample[i][0], f.box.cx, f.box.cy) for i, f in cam.items())


def layout_decision(per_sample: list[tuple[float, list[FaceDet]]], info: MediaInfo) -> tuple[LayoutHint, CamStyle]:
    """Wie layout_from_detections, zusätzlich der Facecam-Stil ('frame' | 'cutout' | 'none') fürs Log.

    track wird immer gefüllt, sobald stabile Gesichter da sind (der 'face'-Shot im Renderer nutzt ihn):
    facecam_split -> Mitten des Facecam-Gesichts, face_track -> großes Gesicht, gameplay_fit -> großes
    Gesicht bzw. Facecam-Gruppe in der Minderheit (mind. TRACK_MIN_POINTS Samples)."""
    if info.height >= info.width:
        return LayoutHint(kind="vertical_native", confidence=1.0), "none"
    n = len(per_sample)
    if n == 0:
        return LayoutHint(kind="gameplay_fit", confidence=0.0), "none"

    cam = _facecam_cluster(per_sample)
    track_pts = _large_face_points(per_sample)
    cam_share, track_share = len(cam) / n, len(track_pts) / n
    style = cam_style(list(cam.values()))

    if cam_share >= FACECAM_MIN_SHARE and not (track_share >= TRACK_MIN_SHARE and track_share > cam_share):
        members = list(cam.values())
        return LayoutHint(kind="facecam_split", facecam_box=_facecam_box(members, info),
                          gameplay_box=_gameplay_box(), track=_smooth_track(_cam_points(per_sample, cam)),
                          confidence=round(cam_share, 3)), style
    if track_share >= TRACK_MIN_SHARE:
        return LayoutHint(kind="face_track", track=_smooth_track(track_pts),
                          confidence=round(track_share, 3)), "none"
    pts = track_pts if len(track_pts) >= TRACK_MIN_POINTS else _cam_points(per_sample, cam)
    track = _smooth_track(pts) if len(pts) >= TRACK_MIN_POINTS else []
    return LayoutHint(kind="gameplay_fit", track=track,
                      confidence=round(1.0 - max(cam_share, track_share), 3)), "none"


def layout_from_detections(per_sample: list[tuple[float, list[FaceDet]]], info: MediaInfo) -> LayoutHint:
    """Reine Layout-Entscheidung aus (Quellzeit, Gesichter)-Samples – Regeln siehe analyze_layout."""
    return layout_decision(per_sample, info)[0]


def analyze_layout(path: str | Path, info: MediaInfo, detector: FaceDetector, samples: int = 16) -> LayoutHint:
    """Entscheidet das Layout:
    - Höhe >= Breite -> vertical_native
    - kleines Gesicht (Breite < ~22 % des Bildes) stabil an ähnlicher Position in >=40 % der Samples
      -> facecam_split; facecam_box = geschätzter Webcam-Rahmen um das Gesicht (Seitenverhältnis ~16:9
      oder 4:3, an Bildrand geklemmt); gameplay_box = Bildbereich ohne Facecam-Ecke (mittig, 16:9).
      Freigestellte Streamer ohne Rahmen ("cutout", Gesicht >= 18 % der Bildhöhe): Box um Kopf + Schultern
      (~2,6x Gesichtsbreite, ~2,2x Gesichtshöhe, nach unten verlängert, an Ränder gezogen)
    - großes Gesicht in den meisten Samples -> face_track (track = geglättete Gesichtsmitten)
    - sonst gameplay_fit
    track enthält immer die geglätteten Gesichtsmitten, sobald stabile Gesichter gefunden wurden.
    """
    if info.height >= info.width:
        return LayoutHint(kind="vertical_native", confidence=1.0)
    per_sample: list[tuple[float, list[FaceDet]]] = []
    for t, frame in _ff.iter_frames(path, _ff.even_times(0.0, info.duration, samples)):
        if frame is None:
            continue
        per_sample.append((round(t, 3), detector.detect(frame)))
    if len(per_sample) < max(1, samples // 2):
        log.warning("Layout analysis: only %d/%d frames readable in %s", len(per_sample), samples, Path(path).name)
    hint, style = layout_decision(per_sample, info)
    log.info("Layout %s (confidence %.2f, cam_style %s, %d track points, detector %s) for %s", hint.kind,
             hint.confidence, style, len(hint.track), detector_backend(detector), Path(path).name)
    return hint


def detector_backend(detector: object) -> str:
    return str(getattr(detector, "backend", type(detector).__name__))
