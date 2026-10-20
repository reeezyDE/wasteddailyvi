"""Schnittliste (TimeMap) aus dem EditPlan."""

from __future__ import annotations

import pytest

from wdvi.render import timeline
from wdvi.schemas import EditPlan, PlatformText, TimeMap


def _plan(trim_start: float, trim_end: float, remove: list[tuple[float, float]] | None = None) -> EditPlan:
    pt = PlatformText(youtube="t", tiktok="t", instagram="t")
    return EditPlan(keep=True, virality_score=80, reasoning="r", trim_start=trim_start, trim_end=trim_end,
                    remove_ranges=remove or [], hook_text="h", titles=pt, descriptions=pt)


def test_trim_only():
    tm = timeline.build_timemap(_plan(1.0, 6.5), 10.0)
    assert tm.segments == [(1.0, 6.5)]
    assert tm.duration == pytest.approx(5.5)


def test_remove_ranges_split_segments():
    tm = timeline.build_timemap(_plan(0.0, 10.0, [(2.0, 3.0), (6.0, 6.5)]), 10.0)
    assert tm.segments == [(0.0, 2.0), (3.0, 6.0), (6.5, 10.0)]
    assert tm.duration == pytest.approx(8.5)
    assert tm.to_output(3.5) == pytest.approx(2.5)
    assert tm.to_output(2.5) is None


def test_overlapping_and_unsorted_ranges_are_merged():
    tm = timeline.build_timemap(_plan(0.0, 10.0, [(5.0, 6.0), (2.0, 4.0), (3.5, 5.5)]), 10.0)
    assert tm.segments == [(0.0, 2.0), (6.0, 10.0)]


def test_tiny_segments_are_dropped():
    # zwischen den Schnitten bleiben nur 0.1 s -> fällt weg
    tm = timeline.build_timemap(_plan(0.0, 8.0, [(2.0, 3.0), (3.1, 4.0)]), 8.0)
    assert tm.segments == [(0.0, 2.0), (4.0, 8.0)]
    assert all(e - s >= 0.2 for s, e in tm.segments)


def test_trim_clamped_to_duration():
    tm = timeline.build_timemap(_plan(-3.0, 50.0), 12.0)
    assert tm.segments == [(0.0, 12.0)]


def test_invalid_trim_falls_back_to_whole_source():
    tm = timeline.build_timemap(_plan(5.0, 5.05), 9.0)
    assert tm.segments == [(0.0, 9.0)]


def test_remove_everything_keeps_trim():
    tm = timeline.build_timemap(_plan(1.0, 4.0, [(0.0, 10.0)]), 10.0)
    assert tm.segments == [(1.0, 4.0)]


def test_map_interval_partial_and_cut():
    tm = TimeMap(segments=[(0.0, 2.0), (3.0, 6.0)])
    assert timeline.map_interval(tm, 2.5, 2.8) is None  # komplett im Schnitt
    assert timeline.map_interval(tm, 1.5, 2.5) == pytest.approx((1.5, 2.0))  # Ende geklemmt
    assert timeline.map_interval(tm, 2.5, 3.5) == pytest.approx((2.0, 2.5))  # Anfang geklemmt
    assert timeline.map_interval(tm, 1.0, 4.0) == pytest.approx((1.0, 3.0))  # über den Schnitt


def test_cap_duration():
    tm = TimeMap(segments=[(0.0, 30.0), (40.0, 80.0)])
    capped = timeline.cap_duration(tm, 59.0)
    assert capped.duration == pytest.approx(59.0)
    assert capped.segments[0] == (0.0, 30.0)
    assert timeline.cap_duration(TimeMap(segments=[(0.0, 5.0)]), 59.0).segments == [(0.0, 5.0)]
