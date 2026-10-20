"""Posting-Slots: feste Uhrzeiten je Plattform in publish.timezone, Mindestabstand, Tageslimit."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from wdvi.config import Settings
from wdvi.publish.base import PublishError

log = logging.getLogger(__name__)

LEAD_TIME = timedelta(minutes=10)  # frühester Slot = now + 10 min
SEARCH_DAYS = 14


def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def parse_slot(value: str) -> time | None:
    """ "09:00" / "9:00" / "21:30" -> time; ungültig -> None."""
    try:
        hh, mm = str(value).strip().split(":")
        return time(int(hh), int(mm))
    except (ValueError, TypeError):
        return None


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise PublishError(f"Unbekannte Zeitzone in publish.timezone: {name!r}") from e


def _local_to_utc(day: date, t: time, tz: ZoneInfo) -> datetime:
    """Lokale Wanduhrzeit -> UTC. Nicht existierende Zeiten (Sommerzeit-Sprung) werden nach vorn
    verschoben (02:30 -> 03:30), doppelte Zeiten (Winterzeit) nehmen das erste Vorkommen."""
    return datetime.combine(day, t, tzinfo=tz).astimezone(UTC)


def next_slot(platform: str, taken: list[datetime], now: datetime, settings: Settings) -> datetime:
    """Nächster freier Slot (UTC, tz-aware) >= now + 10 min, der nicht belegt ist, min_gap_minutes zu
    allen taken einhält und (YouTube) max_uploads_per_day pro lokalem Kalendertag nicht überschreitet.
    Sucht bis zu 14 Tage voraus."""
    pub = settings.config.publish
    pcfg = getattr(pub, platform, None)
    if pcfg is None:
        raise PublishError(f"Unbekannte Plattform für Slots: {platform!r}")
    tz = _zone(pub.timezone)
    gap = timedelta(minutes=max(0, pub.min_gap_minutes))
    earliest = _as_utc(now) + LEAD_TIME
    horizon = earliest + timedelta(days=SEARCH_DAYS)
    booked = sorted(_as_utc(t) for t in taken)
    daily_max = getattr(pcfg, "max_uploads_per_day", None) if platform == "youtube" else None

    per_day: dict[date, int] = {}
    for t in booked:
        d = t.astimezone(tz).date()
        per_day[d] = per_day.get(d, 0) + 1

    def conflicts(cand: datetime) -> list[datetime]:
        return [t for t in booked if t == cand or abs(t - cand) < gap]

    def day_full(cand: datetime) -> bool:
        return bool(daily_max) and per_day.get(cand.astimezone(tz).date(), 0) >= daily_max

    times = sorted({t for t in (parse_slot(s) for s in pcfg.slots) if t is not None})
    if len(times) < len(pcfg.slots):
        log.warning("Ignoring invalid slot entries for %s: %s", platform, pcfg.slots)

    if not times:
        # Ohne feste Slots: frühestmöglicher Zeitpunkt, der Abstand und Tageslimit einhält
        cand = earliest
        while cand <= horizon:
            if day_full(cand):
                nxt = cand.astimezone(tz).date() + timedelta(days=1)
                cand = max(cand, _local_to_utc(nxt, time(0, 0), tz))
                continue
            hits = conflicts(cand)
            if not hits:
                return cand
            cand = max(t + gap for t in hits) if gap else cand + timedelta(minutes=1)
        raise PublishError(f"Kein freier Zeitpunkt für {platform} in den nächsten {SEARCH_DAYS} Tagen.")

    start_day = earliest.astimezone(tz).date()
    for offset in range(SEARCH_DAYS + 1):
        day = start_day + timedelta(days=offset)
        for t in times:
            cand = _local_to_utc(day, t, tz)
            if cand < earliest:
                continue
            if day_full(cand) or conflicts(cand):
                continue
            return cand
    raise PublishError(
        f"Kein freier {platform}-Slot in den nächsten {SEARCH_DAYS} Tagen – mehr Slots in config.yaml "
        "(publish.<plattform>.slots) eintragen oder min_gap_minutes verringern."
    )
