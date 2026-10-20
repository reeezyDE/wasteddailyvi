from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest

from wdvi.media import dedup
from wdvi.media.dedup import is_near_duplicate, phashes


@pytest.fixture(scope="module")
def other_video(tmp_path_factory) -> Path:
    p = tmp_path_factory.mktemp("dedup") / "other.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    "mandelbrot=size=1280x720:rate=30", "-t", "6", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", str(p)], check=True)
    return p


@pytest.fixture(scope="module")
def reencoded_copy(sample_video: Path, tmp_path_factory) -> Path:
    """Gleicher Clip, kleiner skaliert, schlechter kodiert, mit schwarzen Balken (typischer Repost)."""
    p = tmp_path_factory.mktemp("dedup") / "repost.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(sample_video), "-vf",
                    "scale=640:360,pad=640:480:0:60:black", "-c:v", "libx264", "-crf", "32", "-preset",
                    "ultrafast", "-an", str(p)], check=True)
    return p


def test_phashes_are_hex_and_stable(sample_video: Path):
    a = phashes(sample_video)
    assert len(a) == 3
    assert all(len(h) == 16 and int(h, 16) >= 0 for h in a)
    assert phashes(sample_video) == a


def test_identical_and_reencoded_are_duplicates(sample_video: Path, reencoded_copy: Path):
    a = phashes(sample_video)
    assert is_near_duplicate(a, [a], max_distance=0)
    assert is_near_duplicate(phashes(reencoded_copy), [a], max_distance=10)


def test_different_video_is_not_duplicate(sample_video: Path, other_video: Path):
    a, b = phashes(sample_video), phashes(other_video)
    assert len(b) == 3
    assert not is_near_duplicate(b, [a], max_distance=10)
    assert is_near_duplicate(b, [a, b], max_distance=10)


def test_black_frames_are_ignored():
    assert dedup.frame_phash(np.zeros((360, 640, 3), dtype=np.uint8)) is None


def test_is_near_duplicate_rules():
    base = ["ffffffff00000000", "0f0f0f0f0f0f0f0f", "00000000ffffffff"]
    one_bit = ["fffffffe00000000", "aaaaaaaaaaaaaaaa", "5555555555555555"]  # nur 1 von 3 trifft
    assert not is_near_duplicate(one_bit, [base], max_distance=4)
    two = ["fffffffe00000000", "0f0f0f0f0f0f0f0e", "5555555555555555"]
    assert is_near_duplicate(two, [base], max_distance=4)
    # Reihenfolge egal (anders getrimmter Repost)
    assert is_near_duplicate(list(reversed(two)), [base], max_distance=4)
    # nur ein Hash vorhanden -> dieser eine muss treffen
    assert is_near_duplicate(["ffffffff00000001"], [base], max_distance=2)
    assert not is_near_duplicate(["aaaaaaaaaaaaaaaa"], [base], max_distance=2)
    # leer / kaputt
    assert not is_near_duplicate([], [base], max_distance=10)
    assert not is_near_duplicate(base, [], max_distance=10)
    assert not is_near_duplicate(["zz", "not-hex"], [base], max_distance=64)
    assert is_near_duplicate(base, [[], ["garbage"], base], max_distance=0)
