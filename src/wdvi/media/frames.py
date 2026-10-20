"""Standbilder für Claude und Layout-Analyse."""

from __future__ import annotations

from pathlib import Path

from wdvi.schemas import FrameSample


def sample_frames(path: str | Path, out_dir: Path, n: int, width: int, start: float | None = None,
                  end: float | None = None, prefix: str = "f") -> list[FrameSample]:
    """n gleichmäßig verteilte Frames (JPEG, Qualität ~85, Breite `width`) aus [start, end]."""
    raise NotImplementedError
