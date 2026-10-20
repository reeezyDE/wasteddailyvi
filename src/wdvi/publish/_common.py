"""Interne Helfer der Publisher: Fehlertexte ohne Secrets, Zeit, Redirect-URIs, JSON, ffprobe."""

from __future__ import annotations

import json
import logging
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from wdvi.config import Settings
from wdvi.schemas import PostResult

log = logging.getLogger(__name__)

# Upload-Requests dürfen lange dauern (langsamer Upload am Heim-PC)
UPLOAD_TIMEOUT = httpx.Timeout(connect=30.0, read=300.0, write=900.0, pool=30.0)
API_TIMEOUT = httpx.Timeout(60.0, connect=20.0)

_SECRET_PARAM_RE = re.compile(
    r"(?i)\b(access_token|client_secret|refresh_token|code|upload_token|upload_id|fb_exchange_token)=([^&\s\"']+)"
)
_AUTH_HEADER_RE = re.compile(r"(?i)\b(Bearer|OAuth)\s+[A-Za-z0-9._~+/=\-|]+")


def redact(text: str) -> str:
    """Tokens/Secrets aus Texten entfernen (Fehlermeldungen, Logs)."""
    text = _SECRET_PARAM_RE.sub(lambda m: f"{m.group(1)}=***", text)
    return _AUTH_HEADER_RE.sub(lambda m: f"{m.group(1)} ***", text)


def as_utc(dt: datetime) -> datetime:
    """Naive Zeiten gelten als UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def utcnow() -> datetime:
    return datetime.now(UTC)


def failed(msg: str) -> PostResult:
    return PostResult(ok=False, status="failed", error=msg)


def _configured_redirect_uri(settings: Settings, platform: str) -> str:
    """WDVI_<PLATFORM>_REDIRECT_URI – einzige Quelle sind die Secrets (Umgebung bzw. `.env`)."""
    value = getattr(settings.secrets, f"{platform}_redirect_uri", None)
    return str(value or "").strip()


def https_redirect_uri(settings: Settings, platform: str) -> str:
    """Redirect-URI für Plattformen, die nur https-Redirects akzeptieren (TikTok, Instagram).

    Reihenfolge: WDVI_<PLATFORM>_REDIRECT_URI aus Umgebung oder `.env` (exakt so in der App registriert)
    > {public_base_url}/oauth/<platform>/callback, wobei http:// zu https:// wird. Läuft das Dashboard
    nur per http, landet der Browser nach dem Login auf einer Fehlerseite – die URL aus der Adresszeile
    wird dann im Dashboard unter "Redirect-URL einfügen" eingetragen (enthält code + state).
    """
    configured = _configured_redirect_uri(settings, platform)
    if configured:
        return configured
    base = settings.config.dashboard.public_base_url.strip().rstrip("/")
    if base.lower().startswith("http://"):
        base = "https://" + base[len("http://") :]
    return f"{base}/oauth/{platform}/callback"


def json_body(resp: httpx.Response) -> dict[str, Any]:
    """JSON-Body als dict (leeres dict, wenn kein/kaputtes JSON)."""
    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {"data": data}


def short_text(resp: httpx.Response, limit: int = 300) -> str:
    try:
        return redact(resp.text[:limit]).strip()
    except Exception:  # noqa: BLE001 - nur Diagnose
        return ""


def probe_duration(path: Path) -> float | None:
    """Videolänge in Sekunden per ffprobe (None, wenn ffprobe fehlt/fehlschlägt)."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        ).stdout.strip()
        return float(out) if out and out != "N/A" else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def default_client() -> httpx.Client:
    return httpx.Client(
        timeout=API_TIMEOUT, follow_redirects=True, headers={"User-Agent": "wdvi/0.1 (+WastedDailyVI)"}
    )
