"""ffprobe-Wrapper."""

from __future__ import annotations

from pathlib import Path

from wdvi.schemas import MediaInfo


def probe(path: str | Path) -> MediaInfo:
    """Dauer, Auflösung (nach Rotation), fps (avg_frame_rate), hat Audio."""
    raise NotImplementedError
