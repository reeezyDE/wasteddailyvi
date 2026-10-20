"""Posting-Slots: feste Uhrzeiten je Plattform in publish.timezone, Mindestabstand, Tageslimit."""

from __future__ import annotations

from datetime import datetime

from wdvi.config import Settings


def next_slot(platform: str, taken: list[datetime], now: datetime, settings: Settings) -> datetime:
    """Nächster freier Slot (UTC, tz-aware) >= now + 10 min, der nicht belegt ist, min_gap_minutes zu
    allen taken einhält und (YouTube) max_uploads_per_day pro lokalem Kalendertag nicht überschreitet.
    Sucht bis zu 14 Tage voraus."""
    raise NotImplementedError
