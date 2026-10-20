"""Formatierung für die Templates (deutsche Texte, Zeitzonen, sichere Links)."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, tzinfo
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger(__name__)

WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]

_SECRET_PARAM_RE = re.compile(
    r"(?i)\b(access_token|refresh_token|client_secret|code|upload_token|password|fb_exchange_token)"
    r"=([^&\s\"']+)"
)
_AUTH_HEADER_RE = re.compile(r"(?i)\b(Bearer|OAuth|Basic)\s+[A-Za-z0-9._~+/=\-|]+")


def as_utc(dt: datetime | None) -> datetime | None:
    """SQLite liefert naive Zeiten -> gelten als UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def load_tz(name: str) -> tuple[tzinfo, bool]:
    """(Zeitzone, ok). Fehlt die Zeitzonen-Datenbank (z. B. schlankes Docker-Image), fällt es auf UTC zurück."""
    try:
        return ZoneInfo(name), True
    except (ZoneInfoNotFoundError, ValueError) as e:
        log.warning("Timezone %r not available (%s) - falling back to UTC", name, e)
        return UTC, False


def tz_label(name: str) -> str:
    """'America/New_York' -> 'New York'."""
    return name.rsplit("/", 1)[-1].replace("_", " ") or name


def local_dt(dt: datetime | None, tz: tzinfo) -> datetime | None:
    dt = as_utc(dt)
    return dt.astimezone(tz) if dt else None


def fmt_local(dt: datetime | None, tz: tzinfo, with_day: bool = True) -> str:
    """'Di 21.10. 19:00' in der Zielzeitzone."""
    loc = local_dt(dt, tz)
    if loc is None:
        return "–"
    if not with_day:
        return loc.strftime("%H:%M")
    return f"{WEEKDAYS[loc.weekday()]} {loc.strftime('%d.%m.')} {loc.strftime('%H:%M')}"


def fmt_day(dt: datetime | None, tz: tzinfo, now: datetime | None = None) -> str:
    """Überschrift für Tagesgruppen: 'Heute', 'Morgen' oder 'Do 23.10.'."""
    loc = local_dt(dt, tz)
    if loc is None:
        return "–"
    today = (as_utc(now) or datetime.now(UTC)).astimezone(tz).date()
    delta = (loc.date() - today).days
    if delta == 0:
        return "Heute"
    if delta == 1:
        return "Morgen"
    if delta == -1:
        return "Gestern"
    return f"{WEEKDAYS[loc.weekday()]} {loc.strftime('%d.%m.')}"


def ago(dt: datetime | None, now: datetime | None = None) -> str:
    """Relative Zeit auf Deutsch: 'vor 5 Min.', 'in 3 Std.', 'gerade eben'."""
    dt = as_utc(dt)
    if dt is None:
        return "–"
    now = as_utc(now) or datetime.now(UTC)
    secs = (now - dt).total_seconds()
    future = secs < 0
    secs = abs(secs)
    if secs < 45:
        return "gleich" if future else "gerade eben"
    if secs < 3600:
        txt = f"{max(1, round(secs / 60))} Min."
    elif secs < 86400 * 2:
        txt = f"{round(secs / 3600)} Std."
    else:
        txt = f"{round(secs / 86400)} Tagen"  # ab 2 Tagen -> immer Plural
    return f"in {txt}" if future else f"vor {txt}"


def age_seconds(dt: datetime | None, now: datetime | None = None) -> float | None:
    dt = as_utc(dt)
    if dt is None:
        return None
    return ((as_utc(now) or datetime.now(UTC)) - dt).total_seconds()


def fmt_int(n: int | float | None) -> str:
    """12345 -> '12.345'; große Zahlen kompakt: 1.234.567 -> '1,2 Mio.'."""
    if n is None:
        return "–"
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "–"
    if abs(n) >= 1_000_000:
        return f"{n / 1_000_000:.1f} Mio.".replace(".", ",", 1)
    return f"{n:,}".replace(",", ".")


def fmt_duration(seconds: float | None) -> str:
    """42.3 -> '0:42'."""
    if seconds is None:
        return "–"
    s = max(0, int(round(float(seconds))))
    if s >= 3600:
        return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"
    return f"{s // 60}:{s % 60:02d}"


def safe_url(url: str | None) -> str:
    """Nur http(s)-Links durchlassen (kein 'javascript:' o. Ä. aus externen APIs)."""
    if not url or not isinstance(url, str):
        return ""
    url = url.strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return ""
    return url


def short_host(url: str | None) -> str:
    """'https://www.twitch.tv/xqc' -> 'twitch.tv/xqc' (für Anzeige)."""
    u = safe_url(url)
    if not u:
        return ""
    p = urlsplit(u)
    host = p.netloc.lower().removeprefix("www.")
    path = p.path.rstrip("/")
    return (host + path)[:60]


def redact(text: str) -> str:
    """Tokens/Codes aus Fehlertexten entfernen, bevor sie im Browser landen."""
    text = _SECRET_PARAM_RE.sub(lambda m: f"{m.group(1)}=***", text)
    return _AUTH_HEADER_RE.sub(lambda m: f"{m.group(1)} ***", text)


def score_class(score: int | float | None) -> str:
    """CSS-Klasse für das Score-Badge."""
    try:
        s = int(score or 0)
    except (TypeError, ValueError):
        s = 0
    if s >= 85:
        return "hot"
    if s >= 70:
        return "good"
    return "meh"
