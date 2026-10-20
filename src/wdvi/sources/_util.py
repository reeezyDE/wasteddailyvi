"""Kleine Helfer, die mehrere Quellen teilen (Zeitstempel, defensive Typumwandlung, 429-Wartezeit)."""

from __future__ import annotations

import math
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

# Höchstens so lange warten wir auf ein Rate-Limit-Reset, bevor wir aufgeben (Job darf nicht hängen).
MAX_RATELIMIT_WAIT_S = 60.0

DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=10.0)


def as_utc(dt: datetime) -> datetime:
    """Naive Zeitstempel gelten als UTC; aware werden nach UTC konvertiert."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def iso_z(dt: datetime) -> str:
    """RFC3339 mit 'Z' (sekundengenau), wie Twitch/YouTube es erwarten."""
    return as_utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


_FRAC_RE = re.compile(r"(\.\d+)")


def parse_dt(value: Any) -> datetime | None:
    """ISO-8601-String / Unix-Zeit -> tz-aware UTC. Unbrauchbar -> None (nie Exception)."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return as_utc(value)
    if isinstance(value, int | float) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value), UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    s = value.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    # Python < 3.11 / exotische Quellen: Nanosekunden auf Mikrosekunden kürzen
    s = _FRAC_RE.sub(lambda m: m.group(1)[:7], s, count=1)
    s = s.replace(" ", "T", 1) if "T" not in s and " " in s else s
    try:
        return as_utc(datetime.fromisoformat(s))
    except ValueError:
        return None


def to_int(value: Any) -> int | None:
    """'1234' / 1234.0 / 1234 -> 1234; alles andere -> None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return int(f)


def to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def hours_between(now: datetime, then: datetime) -> float:
    return (as_utc(now) - as_utc(then)) / timedelta(hours=1)


def wait_from_headers(resp: httpx.Response, *, epoch_header: str | None = None,
                      seconds_header: str | None = None, default: float = 5.0) -> float:
    """Wartezeit nach einem 429 aus den Antwort-Headern (Unix-Reset oder Sekunden), begrenzt."""
    wait: float | None = None
    if epoch_header and (v := to_float(resp.headers.get(epoch_header))) is not None:
        wait = v - time.time()
    if wait is None and seconds_header and (v := to_float(resp.headers.get(seconds_header))) is not None:
        wait = v
    if wait is None and (v := to_float(resp.headers.get("Retry-After"))) is not None:
        wait = v
    if wait is None:
        wait = default
    return max(1.0, min(wait, MAX_RATELIMIT_WAIT_S))


def json_or_none(resp: Any) -> Any:
    """resp.json() ohne Exception (HTML-Fehlerseiten, Cloudflare-Challenges, ...)."""
    try:
        return resp.json()
    except Exception:  # httpx/curl_cffi werfen unterschiedliche Typen
        return None
