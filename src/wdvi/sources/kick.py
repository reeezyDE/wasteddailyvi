"""Kick: Top-Clips einer Kategorie (inoffizielle Web-Endpunkte, Cloudflare -> curl_cffi). Experimentell.

Kick hat keine offizielle Clip-API. Die Web-App nutzt `/api/v2/categories/<slug>/clips`; ältere
Varianten `/api/v2/clips?category=<slug>`. Beide werden versucht, die Antwort wird defensiv gelesen
(Feldnamen ändern sich gelegentlich). Kandidaten-URL: https://kick.com/<kanal>/clips/<clip_id> (yt-dlp).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from wdvi.config import Settings
from wdvi.schemas import CandidateIn, SourceKind
from wdvi.sources._util import json_or_none, parse_dt, to_float, to_int
from wdvi.sources.base import SourceError

log = logging.getLogger(__name__)

KICK_BASE = "https://kick.com"
_MAX_PAGES = 3
_TIMEOUT_S = 20
_BLOCKED_MSG = ("Kick blockiert die Anfrage (Cloudflare) – später erneut versuchen oder Kick in "
                "config.yaml deaktivieren (sources.kick.enabled: false).")


class _Blocked(Exception):
    pass


class _Failed(Exception):
    pass


class KickSource:
    name = "kick"

    def __init__(self, settings: Settings, session=None) -> None:
        self.settings = settings
        self.cfg = settings.config.sources.kick
        self._session = session  # curl_cffi.requests.Session (lazy, damit Tests ohne curl auskommen)

    def is_configured(self) -> bool:
        return True

    def discover(self, now: datetime) -> list[CandidateIn]:
        slugs = [s.strip() for s in self.cfg.category_slugs if s and s.strip()]
        if not slugs:
            raise SourceError("Kick: keine Kategorien konfiguriert (config.yaml: sources.kick.category_slugs).")

        found: dict[str, CandidateIn] = {}
        errors: list[SourceError] = []
        for slug in slugs:
            try:
                raw = self._fetch_category(slug)
            except SourceError as e:
                log.warning("kick: category %s failed: %s", slug, e)
                errors.append(e)
                continue
            for clip in raw:
                cand = self._to_candidate(clip, slug)
                if cand is not None and (cand.view_count or 0) >= self.cfg.min_views:
                    found.setdefault(cand.source_id, cand)
        if errors and len(errors) == len(slugs):
            raise errors[0]
        out = sorted(found.values(), key=lambda c: c.view_count or 0, reverse=True)
        log.info("kick: %d clips kept", len(out))
        return out

    # ------------------------------------------------------------------ HTTP

    def _get_session(self):
        if self._session is None:
            try:
                from curl_cffi import requests as cffi_requests
            except ImportError as e:  # pragma: no cover – ist eine Pflicht-Abhängigkeit
                raise SourceError("Kick braucht das Paket curl_cffi (pip install curl_cffi).") from e
            self._session = cffi_requests.Session(impersonate="chrome")
        return self._session

    def _endpoints(self, slug: str) -> list[tuple[str, dict[str, Any]]]:
        t = self.cfg.time_range
        return [
            (f"{KICK_BASE}/api/v2/categories/{slug}/clips", {"cursor": "0", "sort": "view", "time": t}),
            (f"{KICK_BASE}/api/v2/clips", {"category": slug, "sort": "view", "time": t}),
        ]

    def _fetch_category(self, slug: str) -> list[dict]:
        blocked = False
        last_err = ""
        for url, params in self._endpoints(slug):
            try:
                return self._paginate(url, params)
            except _Blocked:
                blocked = True
                log.info("kick: blocked on %s", url)
            except _Failed as e:
                last_err = str(e)
                log.info("kick: endpoint %s failed: %s", url, e)
        if blocked:
            raise SourceError(_BLOCKED_MSG)
        raise SourceError(f"Kick-Clips für Kategorie '{slug}' nicht abrufbar ({last_err}). "
                          "Kategorie-Slug prüfen (z. B. grand-theft-auto-vi).")

    def _paginate(self, url: str, params: dict[str, Any]) -> list[dict]:
        clips: list[dict] = []
        for page in range(_MAX_PAGES):
            body = self._get_json(url, params)
            batch, cursor = _extract_clips(body)
            if page == 0 and batch is None:
                raise _Failed("unbekanntes Antwortformat")
            batch = batch or []
            clips.extend(batch)
            if not cursor or not batch:
                break
            # nach Views sortiert: unter min_views lohnt keine weitere Seite
            if (_views(batch[-1]) or 0) < self.cfg.min_views:
                break
            params = {**params, "cursor": str(cursor)}
        return clips

    def _get_json(self, url: str, params: dict[str, Any]) -> Any:
        session = self._get_session()
        try:
            r = session.get(url, params=params, timeout=_TIMEOUT_S,
                            headers={"Accept": "application/json", "Referer": f"{KICK_BASE}/"})
        except Exception as e:  # curl_cffi.requests.RequestsError, Timeouts, ...
            raise SourceError(f"Kick nicht erreichbar ({type(e).__name__}) – Internetverbindung prüfen.") from e
        status = int(getattr(r, "status_code", 0) or 0)
        text = _text(r)
        if status == 403 or _looks_like_cloudflare(r, text):
            raise _Blocked()
        if status == 429:
            raise SourceError("Kick-Rate-Limit erreicht (HTTP 429) – nächster Versuch beim nächsten Poll.")
        if status >= 400:
            raise _Failed(f"HTTP {status}")
        body = json_or_none(r)
        if body is None:
            raise _Failed("keine JSON-Antwort")
        return body

    # ------------------------------------------------------------------ Parsing

    def _to_candidate(self, clip: dict, category_slug: str) -> CandidateIn | None:
        if not isinstance(clip, dict):
            return None
        clip_id = str(clip.get("id") or "").strip()
        if not clip_id:
            return None
        channel = clip.get("channel") if isinstance(clip.get("channel"), dict) else {}
        creator = clip.get("creator") if isinstance(clip.get("creator"), dict) else {}
        slug = str(channel.get("slug") or "").strip() or None
        username = str(channel.get("username") or slug or "").strip()
        media_url = clip.get("clip_url") or clip.get("video_url") or None
        if slug:
            url = f"{KICK_BASE}/{slug}/clips/{clip_id}"
        elif media_url:
            url = str(media_url)
        else:
            return None
        created = parse_dt(clip.get("created_at")) or parse_dt(clip.get("started_at"))
        if created is None:
            return None
        likes = to_float(clip.get("likes") if clip.get("likes") is not None else clip.get("likes_count"))
        category = clip.get("category") if isinstance(clip.get("category"), dict) else {}
        return CandidateIn(
            source=SourceKind.kick_clip,
            source_id=clip_id,
            url=url,
            title=str(clip.get("title") or "").strip() or f"Clip von {username or 'Kick'}",
            creator_name=username or "Kick-Streamer",
            creator_handle=slug.lower() if slug else None,
            creator_url=f"{KICK_BASE}/{slug}" if slug else None,
            platform_label="Kick",
            created_at=created,
            duration_s=to_float(clip.get("duration")),
            view_count=_views(clip),
            engagement={"likes": likes} if likes is not None else {},
            thumbnail_url=clip.get("thumbnail_url") or None,
            extra={
                "media_url": media_url,
                "video_url": clip.get("video_url"),
                "channel_id": str(channel.get("id") or clip.get("channel_id") or ""),
                "channel_slug": slug,
                "clip_creator_name": creator.get("username"),
                "livestream_id": clip.get("livestream_id"),
                "category_slug": category.get("slug") or category_slug,
                "is_mature": bool(clip.get("is_mature")),
            },
        )


def _views(clip: dict) -> int | None:
    v = to_int(clip.get("views"))
    return v if v is not None else to_int(clip.get("view_count"))


def _extract_clips(body: Any) -> tuple[list[dict] | None, Any]:
    """Liste + nächster Cursor aus den bekannten Antwortformen ({clips:[..], nextCursor}, {data:..}, [..])."""
    if isinstance(body, list):
        return [c for c in body if isinstance(c, dict)], None
    if not isinstance(body, dict):
        return None, None
    cursor = body.get("nextCursor") or body.get("next_cursor") or body.get("cursor")
    for key in ("clips", "data"):
        val = body.get(key)
        if isinstance(val, list):
            return [c for c in val if isinstance(c, dict)], cursor
        if isinstance(val, dict):
            inner, inner_cursor = _extract_clips(val)
            if inner is not None:
                return inner, inner_cursor or cursor
    return None, None


def _text(resp: Any) -> str:
    try:
        return str(resp.text or "")[:2000]
    except Exception:
        return ""


def _looks_like_cloudflare(resp: Any, text: str) -> bool:
    status = int(getattr(resp, "status_code", 0) or 0)
    if status not in (403, 503, 520, 521, 522, 523, 524, 525, 526, 200):
        return False
    lowered = text.lower()
    challenge = "just a moment" in lowered or "cf-chl" in lowered or "challenge-platform" in lowered
    if status == 200:
        return challenge and "<html" in lowered
    headers = getattr(resp, "headers", {}) or {}
    server = str(headers.get("server", "") or "").lower()
    return challenge or (status == 503 and "cloudflare" in server)
