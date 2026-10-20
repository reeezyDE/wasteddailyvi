"""Interne ffmpeg-/OpenCV-Helfer für das media-Paket (Audio dekodieren, einzelne Frames lesen)."""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Iterable, Iterator
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000


class FFmpegError(RuntimeError):
    pass


def run(cmd: list[str], timeout: float = 300.0) -> bytes:
    """Startet ffmpeg/ffprobe, gibt stdout zurück. Fehler -> FFmpegError mit kurzer Meldung."""
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False, stdin=subprocess.DEVNULL)
    except FileNotFoundError as e:
        raise FFmpegError(f"'{cmd[0]}' nicht gefunden – ist ffmpeg installiert und im PATH?") from e
    except subprocess.TimeoutExpired as e:
        raise FFmpegError(f"{cmd[0]} hat nach {timeout:.0f} s nicht geantwortet (Abbruch)") from e
    if proc.returncode != 0:
        lines = [ln for ln in proc.stderr.decode("utf-8", "replace").strip().splitlines() if ln.strip()]
        tail = " | ".join(lines[-3:]) if lines else f"Exit-Code {proc.returncode}"
        raise FFmpegError(f"{cmd[0]} fehlgeschlagen: {tail}")
    return proc.stdout


def decode_audio(path: str | Path, sr: int = SAMPLE_RATE, start: float | None = None,
                 duration: float | None = None, timeout: float = 600.0) -> np.ndarray:
    """Erste Audiospur als Mono-float32 mit `sr` Hz. Datei ohne Audio -> leeres Array."""
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error"]
    if start:
        cmd += ["-ss", f"{max(0.0, start):.3f}"]
    cmd += ["-i", str(path)]
    if duration:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"]
    try:
        raw = run(cmd, timeout=timeout)
    except FFmpegError as e:
        msg = str(e)
        # keine Audiospur (Meldung je nach ffmpeg-Version unterschiedlich)
        if any(s in msg for s in ("matches no streams", "'0:a:0' for option 'map'", "does not contain any stream")):
            return np.zeros(0, dtype=np.float32)
        raise
    n = len(raw) // 4
    return np.frombuffer(raw[: n * 4], dtype="<f4").astype(np.float32, copy=False)


def read_frame_ffmpeg(path: str | Path, t: float, width: int | None = None) -> np.ndarray | None:
    """Ein Frame bei Quellzeit t als BGR-Array (über ffmpeg, PNG-Pipe). None, wenn dort kein Frame ist."""
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-ss", f"{max(0.0, t):.3f}",
           "-i", str(path), "-frames:v", "1"]
    if width:
        cmd += ["-vf", f"scale={int(width)}:-2"]
    cmd += ["-f", "image2pipe", "-vcodec", "png", "-"]
    try:
        data = run(cmd, timeout=120)
    except FFmpegError as e:
        if "nicht gefunden" in str(e):
            raise
        log.debug("ffmpeg frame read failed at %.2fs: %s", t, e)
        return None
    if not data:
        return None
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    return img if img is not None and img.size else None


def iter_frames(path: str | Path, times: Iterable[float]) -> Iterator[tuple[float, np.ndarray | None]]:
    """Frames zu den Zeitpunkten (aufsteigend sortiert) per cv2.VideoCapture-Seek; Fallback ffmpeg."""
    cap = cv2.VideoCapture(str(path))
    opened = cap.isOpened()
    try:
        for t in times:
            frame = None
            if opened:
                cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, t) * 1000.0)
                ok, img = cap.read()
                if ok and img is not None and img.size:
                    frame = img
            if frame is None:
                frame = read_frame_ffmpeg(path, t)
            yield t, frame
    finally:
        cap.release()


def even_times(start: float, end: float, n: int) -> list[float]:
    """n Zeitpunkte gleichmäßig in [start, end] (Mitte der n Teilstücke, also nie genau Anfang/Ende)."""
    if n <= 0:
        return []
    span = max(0.0, end - start)
    return [start + (i + 0.5) / n * span for i in range(n)]
