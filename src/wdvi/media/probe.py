"""ffprobe-Wrapper."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from wdvi.media import _ff
from wdvi.schemas import MediaInfo

log = logging.getLogger(__name__)


class ProbeError(RuntimeError):
    pass


def _parse_rate(rate: str | None) -> float:
    """"30000/1001" -> 29.97; "0/0" / Unsinn -> 0.0."""
    if not rate:
        return 0.0
    try:
        if "/" in rate:
            num, den = rate.split("/", 1)
            d = float(den)
            return float(num) / d if d else 0.0
        return float(rate)
    except (ValueError, ZeroDivisionError):
        return 0.0


def _to_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _rotation(stream: dict) -> int:
    """Rotation in Grad (0..359) aus Display-Matrix-Side-Data oder altem rotate-Tag."""
    for sd in stream.get("side_data_list") or []:
        if "rotation" in sd:
            try:
                return int(round(float(sd["rotation"]))) % 360
            except (TypeError, ValueError):
                pass
    rot = (stream.get("tags") or {}).get("rotate")
    if rot is not None:
        try:
            return int(round(float(rot))) % 360
        except (TypeError, ValueError):
            pass
    return 0


def probe(path: str | Path) -> MediaInfo:
    """Dauer, Auflösung (nach Rotation), fps (avg_frame_rate), hat Audio."""
    p = Path(path)
    if not p.is_file():
        raise ProbeError(f"Videodatei nicht gefunden: {p}")
    try:
        out = _ff.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format",
                       str(p)], timeout=60)
        data = json.loads(out.decode("utf-8", "replace") or "{}")
    except _ff.FFmpegError as e:
        raise ProbeError(f"Video konnte nicht gelesen werden ({p.name}): {e}") from e
    except json.JSONDecodeError as e:
        raise ProbeError(f"ffprobe lieferte unlesbare Daten für {p.name}") from e

    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not (s.get("disposition") or {}).get("attached_pic")), None)
    if video is None:
        raise ProbeError(f"Keine Videospur in {p.name}")

    width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    if width <= 0 or height <= 0:
        raise ProbeError(f"Ungültige Auflösung in {p.name}: {width}x{height}")
    if _rotation(video) % 180 == 90:  # Handyvideos: hochkant gespeichert über Display-Matrix
        width, height = height, width

    fps = _parse_rate(video.get("avg_frame_rate"))
    if not 1.0 <= fps <= 240.0:
        fps = _parse_rate(video.get("r_frame_rate"))
    if not 1.0 <= fps <= 240.0:
        log.warning("Unplausible frame rate in %s, assuming 30 fps", p.name)
        fps = 30.0

    fmt = data.get("format") or {}
    duration = _to_float(fmt.get("duration")) or _to_float(video.get("duration"))
    if duration <= 0:
        nb = _to_float(video.get("nb_frames"))
        duration = nb / fps if nb > 0 else 0.0
    if duration <= 0:
        raise ProbeError(f"Dauer von {p.name} unbekannt (Datei unvollständig?)")

    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    return MediaInfo(path=str(p), duration=round(duration, 3), width=width, height=height,
                     fps=round(fps, 3), has_audio=has_audio)
