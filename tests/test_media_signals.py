from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest

from tests.conftest import make_video
from wdvi.media import signals
from wdvi.media.signals import heatmap_peaks, loudness_peaks


def test_loudness_peak_near_five_seconds(sample_video: Path):
    peaks = loudness_peaks(sample_video)
    assert peaks, "expected at least one loud moment"
    assert 4.9 <= peaks[0] <= 5.7
    # nur eine laute Stelle im Testvideo -> keine weiteren Spitzen über Median+6 dB
    assert len(peaks) == 1


def test_loudness_peaks_without_audio(tmp_path: Path):
    silent = make_video(tmp_path / "noaudio.mp4", seconds=2, size="320x240", audio=False)
    assert loudness_peaks(silent) == []


def test_loudness_peaks_respects_gap_and_order(monkeypatch):
    sr = 16000
    audio = np.full(sr * 20, 0.05, dtype=np.float32)
    audio[sr * 3: int(sr * 3.6)] = 0.5  # laut
    audio[sr * 4: int(sr * 4.6)] = 0.9  # am lautesten, aber nur 1 s neben der ersten Spitze
    audio[sr * 12: int(sr * 12.6)] = 0.7
    monkeypatch.setattr(signals._ff, "decode_audio", lambda path, sr=16000: audio)
    peaks = loudness_peaks("dummy.mp4", top_k=5, min_gap_s=2.0)
    assert len(peaks) == 2
    assert 4.0 <= peaks[0] <= 4.6  # lauteste zuerst
    assert 12.0 <= peaks[1] <= 12.6


def test_heatmap_peaks_with_dicts():
    heat = [{"start_time": i * 10.0, "end_time": (i + 1) * 10.0, "value": v}
            for i, v in enumerate([0.1, 0.2, 0.9, 0.85, 0.3, 0.2, 0.1, 0.6, 0.2, 0.1])]
    peaks = heatmap_peaks(heat, top_k=3, min_gap_s=30.0)
    assert peaks[0] == (20.0, 30.0, 0.9)
    # 30-40 (0.85) liegt zu nah an der stärksten Spitze -> 70-80 (0.6) kommt als nächstes
    assert peaks[1] == (70.0, 80.0, 0.6)
    assert all(isinstance(p, tuple) and len(p) == 3 for p in peaks)


@dataclass
class HeatPoint:  # wie wdvi.sources.youtube.HeatPoint (nicht importiert: fremdes Modul)
    start: float
    end: float
    value: float


def test_heatmap_peaks_with_objects():
    heat = [HeatPoint(start=0, end=5, value=0.2), HeatPoint(start=100, end=105, value=1.0),
            HeatPoint(start=200, end=205, value=0.5)]
    assert heatmap_peaks(heat, top_k=2) == [(100.0, 105.0, 1.0), (200.0, 205.0, 0.5)]


def test_heatmap_peaks_empty_and_bad_entries():
    assert heatmap_peaks([]) == []
    assert heatmap_peaks([{"start_time": "x", "value": None}, {"start": 1, "end": 2, "value": 0.4}]) == [
        (1.0, 2.0, 0.4)]


def test_rms_db_short_audio():
    c, db = signals.rms_db(np.zeros(100, dtype=np.float32), 16000)
    assert c.size == 0 and db.size == 0


@pytest.mark.parametrize("top_k", [0, -1])
def test_loudness_peaks_top_k_zero(sample_video: Path, top_k: int):
    assert loudness_peaks(sample_video, top_k=top_k) == []
