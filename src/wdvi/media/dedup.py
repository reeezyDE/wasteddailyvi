"""Perceptual-Hash-Dedup (Reposts desselben Clips über Quellen hinweg)."""

from __future__ import annotations

import logging
from pathlib import Path

import cv2
import imagehash
import numpy as np
from PIL import Image

from wdvi.media import _ff
from wdvi.media import probe as probe_mod

log = logging.getLogger(__name__)

UNIFORM_STD = 4.0  # (fast) einfarbige Frames (Schwarzbild) haben alle denselben Hash -> ignorieren


def _trim_borders(img: np.ndarray, thresh: int = 20) -> np.ndarray:
    """Schwarze Balken (Letterbox/Pillarbox) abschneiden, damit Reposts mit Rändern noch passen."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    rows = np.where((gray > thresh).mean(axis=1) > 0.02)[0]
    cols = np.where((gray > thresh).mean(axis=0) > 0.02)[0]
    if rows.size == 0 or cols.size == 0:
        return img
    y0, y1, x0, x1 = rows[0], rows[-1] + 1, cols[0], cols[-1] + 1
    h, w = gray.shape
    if (y1 - y0) < 0.3 * h or (x1 - x0) < 0.3 * w:  # zu viel weg -> lieber ungeschnitten
        return img
    return img[y0:y1, x0:x1]


def frame_phash(img_bgr: np.ndarray) -> str | None:
    """pHash eines BGR-Frames als Hex; None für (fast) einfarbige Frames."""
    img = _trim_borders(img_bgr)
    if float(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).std()) < UNIFORM_STD:
        return None
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    return str(imagehash.phash(Image.fromarray(rgb)))


def phashes(path: str | Path, n: int = 3) -> list[str]:
    """pHash-Hexstrings von n Frames bei 25/50/75 %."""
    if n <= 0:
        return []
    duration = probe_mod.probe(path).duration
    times = [duration * (i + 1) / (n + 1) for i in range(n)]
    out: list[str] = []
    for _, frame in _ff.iter_frames(path, times):
        if frame is None:
            continue
        h = frame_phash(frame)
        if h:
            out.append(h)
    return out


def _parse(h: str) -> imagehash.ImageHash | None:
    try:
        return imagehash.hex_to_hash(h)
    except (ValueError, TypeError):
        return None


def is_near_duplicate(hashes: list[str], known: list[list[str]], max_distance: int) -> bool:
    """True, wenn mind. 2 der Hashes (bzw. alle, falls <2) einen bekannten Satz mit Hamming-Distanz
    <= max_distance treffen."""
    mine = [p for p in (_parse(h) for h in hashes or []) if p is not None]
    if not mine:
        return False
    need = min(2, len(mine))
    for other in known or []:
        theirs = [p for p in (_parse(h) for h in other or []) if p is not None]
        if not theirs:
            continue
        hits = 0
        for a in mine:  # reihenfolgeunabhängig: anders getrimmte Reposts verschieben die 25/50/75-%-Punkte
            try:
                if min(a - b for b in theirs) <= max_distance:
                    hits += 1
            except (TypeError, ValueError):  # unterschiedliche Hash-Größen
                continue
        if hits >= need:
            return True
    return False
