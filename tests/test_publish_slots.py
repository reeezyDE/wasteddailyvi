"""Posting-Slots: lokale Wanduhrzeiten, Sommerzeit, Mindestabstand, Tageslimit."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from wdvi.publish.base import PublishError
from wdvi.publish.slots import next_slot, parse_slot

NY = ZoneInfo("America/New_York")


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


@pytest.fixture
def cfg(settings):
    settings.config.publish.timezone = "America/New_York"
    settings.config.publish.min_gap_minutes = 90
    settings.config.publish.youtube.slots = ["09:00", "12:30", "16:00", "19:00", "22:00"]
    settings.config.publish.tiktok.slots = ["09:00", "12:30", "16:00", "19:00", "22:00"]
    return settings


def test_parse_slot():
    assert parse_slot("9:05").hour == 9
    assert parse_slot("21:30").minute == 30
    assert parse_slot("25:00") is None
    assert parse_slot("abc") is None


def test_first_slot_is_local_wall_clock(cfg):
    # 12:00 UTC = 08:00 EDT -> 09:00 EDT = 13:00 UTC
    s = next_slot("youtube", [], utc(2026, 10, 20, 12, 0), cfg)
    assert s == utc(2026, 10, 20, 13, 0)
    assert s.tzinfo is not None and s.utcoffset() == timedelta(0)


def test_lead_time_of_ten_minutes(cfg):
    # 12:55 UTC: 09:00 EDT (13:00 UTC) liegt nur 5 min entfernt -> nächster Slot 12:30 EDT
    s = next_slot("youtube", [], utc(2026, 10, 20, 12, 55), cfg)
    assert s == utc(2026, 10, 20, 16, 30)
    s = next_slot("youtube", [], utc(2026, 10, 20, 12, 50), cfg)
    assert s == utc(2026, 10, 20, 13, 0)


def test_taken_and_min_gap(cfg):
    now = utc(2026, 10, 20, 12, 0)
    # Slot belegt
    assert next_slot("tiktok", [utc(2026, 10, 20, 13, 0)], now, cfg) == utc(2026, 10, 20, 16, 30)
    # 30 min neben dem Slot -> Mindestabstand 90 min verletzt
    assert next_slot("tiktok", [utc(2026, 10, 20, 13, 30)], now, cfg) == utc(2026, 10, 20, 16, 30)
    # genau 90 min Abstand ist ok
    assert next_slot("tiktok", [utc(2026, 10, 20, 11, 30)], now, cfg) == utc(2026, 10, 20, 13, 0)


def test_dst_fall_back_keeps_local_time(cfg):
    cfg.config.publish.youtube.slots = ["09:00"]
    # 31.10.2026 09:00 EDT = 13:00 UTC; am 01.11. gilt EST -> 09:00 EST = 14:00 UTC
    s1 = next_slot("youtube", [], utc(2026, 10, 31, 12, 0), cfg)
    assert s1 == utc(2026, 10, 31, 13, 0)
    s2 = next_slot("youtube", [s1], utc(2026, 10, 31, 12, 0), cfg)
    assert s2 == utc(2026, 11, 1, 14, 0)
    assert s2.astimezone(NY).hour == 9


def test_dst_spring_forward_nonexistent_time(cfg):
    cfg.config.publish.tiktok.slots = ["02:30"]
    # 08.03.2026: 02:00 -> 03:00 EDT; 02:30 existiert nicht -> wird zu 03:30 EDT (07:30 UTC)
    s = next_slot("tiktok", [], utc(2026, 3, 7, 12, 0), cfg)
    assert s == utc(2026, 3, 8, 7, 30)
    assert s.astimezone(NY).hour == 3
    # Tag danach wieder normal: 02:30 EDT = 06:30 UTC
    s2 = next_slot("tiktok", [s], utc(2026, 3, 7, 12, 0), cfg)
    assert s2 == utc(2026, 3, 9, 6, 30)


def test_youtube_daily_limit_per_local_day(cfg):
    cfg.config.publish.youtube.max_uploads_per_day = 2
    cfg.config.publish.min_gap_minutes = 0
    now = utc(2026, 10, 20, 12, 0)
    # 13:00 UTC (09:00 EDT) und 03:30 UTC am 21. (= 23:30 EDT am 20.) zählen beide zum lokalen 20.10.
    taken = [utc(2026, 10, 20, 13, 0), utc(2026, 10, 21, 3, 30)]
    s = next_slot("youtube", taken, now, cfg)
    assert s == utc(2026, 10, 21, 13, 0)  # 21.10. 09:00 EDT
    # TikTok hat kein Tageslimit -> gleicher Tag, nächster Slot
    assert next_slot("tiktok", taken, now, cfg) == utc(2026, 10, 20, 16, 30)


def test_naive_now_is_utc(cfg):
    assert next_slot("youtube", [], datetime(2026, 10, 20, 12, 0), cfg) == utc(2026, 10, 20, 13, 0)


def test_mixed_naive_taken(cfg):
    s = next_slot("youtube", [datetime(2026, 10, 20, 13, 0)], utc(2026, 10, 20, 12, 0), cfg)
    assert s == utc(2026, 10, 20, 16, 30)


def test_no_fixed_slots_uses_earliest_with_gap(cfg):
    cfg.config.publish.instagram.slots = []
    now = utc(2026, 10, 20, 12, 0)
    assert next_slot("instagram", [], now, cfg) == utc(2026, 10, 20, 12, 10)
    taken = [utc(2026, 10, 20, 12, 0), utc(2026, 10, 20, 13, 0)]
    assert next_slot("instagram", taken, now, cfg) == utc(2026, 10, 20, 14, 30)


def test_no_slot_within_14_days_raises(cfg):
    cfg.config.publish.youtube.slots = ["09:00"]
    now = utc(2026, 10, 20, 12, 0)
    taken: list[datetime] = []
    for _ in range(15):  # heute + 14 Tage, je ein Slot
        taken.append(next_slot("youtube", taken, now, cfg))
    assert taken[-1] == utc(2026, 11, 3, 14, 0)
    with pytest.raises(PublishError):
        next_slot("youtube", taken, now, cfg)


def test_unknown_timezone_raises(cfg):
    cfg.config.publish.timezone = "Mars/Olympus"
    with pytest.raises(PublishError, match="Zeitzone"):
        next_slot("youtube", [], utc(2026, 10, 20, 12, 0), cfg)
