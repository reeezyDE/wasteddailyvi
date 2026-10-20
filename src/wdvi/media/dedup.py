"""Perceptual-Hash-Dedup (Reposts desselben Clips über Quellen hinweg)."""

from __future__ import annotations

from pathlib import Path


def phashes(path: str | Path, n: int = 3) -> list[str]:
    """pHash-Hexstrings von n Frames bei 25/50/75 %."""
    raise NotImplementedError


def is_near_duplicate(hashes: list[str], known: list[list[str]], max_distance: int) -> bool:
    """True, wenn mind. 2 der Hashes (bzw. alle, falls <2) einen bekannten Satz mit Hamming-Distanz
    <= max_distance treffen."""
    raise NotImplementedError
