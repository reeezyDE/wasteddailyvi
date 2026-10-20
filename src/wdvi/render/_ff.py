"""Interne ffmpeg-/ffprobe-Helfer fürs Rendering (Prozesse starten, stderr-Auszüge, Probe)."""

from __future__ import annotations

import json
import logging
import subprocess
import threading
from collections import deque
from pathlib import Path

log = logging.getLogger(__name__)

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"


class RenderError(RuntimeError):
    """Fehler beim Rendern (Meldung auf Deutsch, mit ffmpeg-stderr-Auszug)."""


def tail(text: str | bytes, n: int = 6) -> str:
    """Letzte n nicht-leere Zeilen (für Fehlermeldungen)."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    return " | ".join(lines[-n:])


def run(cmd: list[str], *, timeout: float = 600.0, cwd: Path | None = None, input: bytes | None = None,
        what: str = "ffmpeg") -> subprocess.CompletedProcess:
    """Startet ffmpeg/ffprobe synchron. Fehler -> RenderError mit verständlicher Meldung + stderr-Auszug."""
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False, cwd=cwd, input=input,
                              stdin=None if input is not None else subprocess.DEVNULL)
    except FileNotFoundError as e:
        raise RenderError(f"'{cmd[0]}' nicht gefunden – ist ffmpeg installiert und im PATH?") from e
    except subprocess.TimeoutExpired as e:
        raise RenderError(f"{what}: ffmpeg hat nach {timeout:.0f} s nicht geantwortet (Abbruch)") from e
    if proc.returncode != 0:
        detail = tail(proc.stderr) or f"Exit-Code {proc.returncode}"
        raise RenderError(f"{what} fehlgeschlagen: {detail}")
    return proc


def probe(path: Path) -> dict:
    """ffprobe-JSON (streams + format). RenderError, wenn die Datei nicht lesbar ist."""
    p = Path(path)
    if not p.is_file():
        raise RenderError(f"Datei nicht gefunden: {p}")
    proc = run([FFPROBE, "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(p)],
               timeout=60, what=f"ffprobe ({p.name})")
    try:
        return json.loads(proc.stdout.decode("utf-8", "replace") or "{}")
    except json.JSONDecodeError as e:
        raise RenderError(f"ffprobe lieferte unlesbare Daten für {p.name}") from e


def video_stream(info: dict) -> dict | None:
    return next((s for s in info.get("streams") or [] if s.get("codec_type") == "video"
                 and not (s.get("disposition") or {}).get("attached_pic")), None)


def display_size(info: dict) -> tuple[int, int]:
    """Breite/Höhe der Videospur nach Rotation (Handyvideos mit Display-Matrix); (0, 0) wenn unbekannt."""
    vs = video_stream(info) or {}
    w, h = int(vs.get("width") or 0), int(vs.get("height") or 0)
    rot = 0
    for sd in vs.get("side_data_list") or []:
        if "rotation" in sd:
            try:
                rot = int(round(float(sd["rotation"])))
            except (TypeError, ValueError):
                pass
    if not rot:
        try:
            rot = int(round(float((vs.get("tags") or {}).get("rotate") or 0)))
        except (TypeError, ValueError):
            rot = 0
    return (h, w) if rot % 180 == 90 else (w, h)


def has_audio(info: dict) -> bool:
    return any(s.get("codec_type") == "audio" for s in info.get("streams") or [])


def duration(info: dict) -> float:
    """Containerdauer, sonst Dauer der Videospur; 0.0 wenn unbekannt."""
    for v in ((info.get("format") or {}).get("duration"), (video_stream(info) or {}).get("duration")):
        try:
            d = float(v)
            if d > 0:
                return d
        except (TypeError, ValueError):
            continue
    return 0.0


class StderrCollector:
    """Liest stderr eines Prozesses in einem Thread (verhindert Deadlocks), behält die letzten Zeilen."""

    def __init__(self, stream, keep: int = 40) -> None:
        self._lines: deque[str] = deque(maxlen=keep)
        self._thread = threading.Thread(target=self._pump, args=(stream,), daemon=True)
        self._thread.start()

    def _pump(self, stream) -> None:
        try:
            for raw in iter(stream.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip()
                if line:
                    self._lines.append(line)
        except (OSError, ValueError):
            pass

    def join(self, timeout: float = 5.0) -> None:
        self._thread.join(timeout)

    def text(self, n: int = 6) -> str:
        return " | ".join(list(self._lines)[-n:])
