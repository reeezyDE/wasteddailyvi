from __future__ import annotations

from pathlib import Path

import cv2
import pytest

from wdvi.media import frames as frames_mod
from wdvi.media.frames import frame_times, sample_frames


def test_sample_frames_whole_clip(sample_video: Path, tmp_path: Path):
    frames = sample_frames(sample_video, tmp_path / "frames", n=4, width=320, prefix="clip")
    assert [Path(f.path).name for f in frames] == [f"clip_{i}.jpg" for i in range(4)]
    assert [f.t for f in frames] == pytest.approx([1.0, 3.0, 5.0, 7.0], abs=0.01)
    for f in frames:
        img = cv2.imread(f.path)
        assert img is not None
        assert img.shape[1] == 320 and img.shape[0] == 180
        assert f.faces == []


def test_sample_frames_range(sample_video: Path, tmp_path: Path):
    frames = sample_frames(sample_video, tmp_path, n=2, width=160, start=2.0, end=4.0)
    assert [f.t for f in frames] == pytest.approx([2.5, 3.5], abs=0.01)
    assert all(Path(f.path).is_file() for f in frames)


def test_sample_frames_vertical_and_zero(vertical_video: Path, tmp_path: Path):
    # Hochkant: lange Kante (Höhe) = width -> 720x1280 wird 360x640 statt 640x1138 (Tokenkosten)
    frames = sample_frames(vertical_video, tmp_path, n=3, width=640)
    assert len(frames) == 3
    img = cv2.imread(frames[0].path)
    assert img.shape[:2] == (640, 360)
    assert sample_frames(vertical_video, tmp_path, n=0, width=360) == []


def test_sample_frames_past_end_is_skipped(sample_video: Path, tmp_path: Path):
    # Bereich komplett hinter dem Videoende -> keine Frames, aber kein Fehler
    assert sample_frames(sample_video, tmp_path, n=2, width=160, start=50.0, end=60.0) == []


def test_sample_frames_at_loudness_peaks(sample_video: Path, tmp_path: Path, monkeypatch):
    seen: list[float] = []
    real = frames_mod._extract

    def spy(path, t, width, out):
        seen.append(round(t, 3))
        return real(path, t, width, out)

    monkeypatch.setattr(frames_mod, "_extract", spy)
    frames = sample_frames(sample_video, tmp_path, n=8, width=320, prefix="p", focus=[5.25])
    assert len(frames) == 8
    ts = [f.t for f in frames]
    assert ts == sorted(ts) and seen == ts  # aufsteigend, mit Quellzeit beschriftet
    assert 5.25 in ts and 5.75 in ts  # Einschlag + Payoff
    assert [Path(f.path).name for f in frames] == [f"p_{i}.jpg" for i in range(8)]
    assert ts[0] < 1.0 and ts[-1] > 7.0  # Rest deckt weiterhin den ganzen Clip ab


def test_frame_times_without_focus_is_even():
    assert frame_times(0.0, 8.0, 4) == pytest.approx([1.0, 3.0, 5.0, 7.0])
    assert frame_times(0.0, 8.0, 4, focus=[]) == frame_times(0.0, 8.0, 4)
    assert frame_times(0.0, 8.0, 0, focus=[3.0]) == []
    assert frame_times(0.0, 30.0, 1, focus=[10.0]) == pytest.approx([15.0])  # n=1: kein Peak-Budget


def test_frame_times_half_at_peaks_rest_fills_gaps():
    # Peaks nach Lautstärke sortiert; Budget n//2 = 4 -> die zwei stärksten, je Peak + Payoff
    ts = frame_times(0.0, 30.0, 8, focus=[12.0, 25.0, 3.0])
    assert len(ts) == 8 and ts == sorted(ts)
    assert {12.0, 12.5, 25.0, 25.5} <= set(ts)
    assert 3.0 not in ts
    rest = [t for t in ts if t not in {12.0, 12.5, 25.0, 25.5}]
    assert len(rest) == 4
    # kein Füll-Frame klebt an einem Peak-Frame, und die großen Lücken werden genutzt
    assert all(min(abs(t - p) for p in (12.0, 12.5, 25.0, 25.5)) >= 2.0 for t in rest)
    assert rest[0] < 5.0 and any(15.0 < t < 22.0 for t in rest)


def test_frame_times_focus_dedup_and_bounds():
    # zu nah (<1 s) am stärkeren Peak, außerhalb des Bereichs -> ignoriert
    ts = frame_times(0.0, 20.0, 8, focus=[10.0, 10.6, 40.0, -1.0, 4.0])
    assert {10.0, 10.5, 4.0, 4.5} <= set(ts) and 10.6 not in ts and len(ts) == 8
    # Payoff direkt am Clipende fällt weg, der Peak selbst bleibt
    ts = frame_times(0.0, 10.0, 4, focus=[9.9])
    assert 9.9 in ts and len(ts) == 4 and max(ts) <= 10.0
    # ungerades Budget: nur der Peak, kein Payoff
    ts = frame_times(0.0, 30.0, 3, focus=[10.0])
    assert 10.0 in ts and 10.5 not in ts and len(ts) == 3
    # Bereich mit start > 0 (z. B. Teilstück)
    ts = frame_times(2.0, 4.0, 2, focus=[10.0])
    assert ts == pytest.approx([2.5, 3.5])


def test_scale_filter_uses_long_edge():
    assert frames_mod.scale_filter(640) == "scale='if(gt(iw,ih),640,-2)':'if(gt(iw,ih),-2,640)'"
