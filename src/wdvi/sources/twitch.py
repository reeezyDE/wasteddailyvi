"""Twitch: meistgesehene Clips der GTA-6-Kategorie über die offizielle Helix-API (App-Access-Token)."""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta
from typing import Any

import httpx

from wdvi.config import Settings
from wdvi.schemas import CandidateIn, SourceKind
from wdvi.sources._util import (
    DEFAULT_TIMEOUT,
    as_utc,
    iso_z,
    json_or_none,
    parse_dt,
    to_float,
    to_int,
    wait_from_headers,
)
from wdvi.sources.base import SourceError

log = logging.getLogger(__name__)

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
API_BASE = "https://api.twitch.tv/helix"

_TOKEN_SAFETY_S = 300.0  # Token 5 min vor Ablauf erneuern
_MAX_PAGES = 10  # Sicherheitsgrenze pro Spiel und Poll (je 100 Clips)
_MAX_429_RETRIES = 2
_LOGIN_RE = re.compile(r"^[a-zA-Z0-9_]{2,25}$")


class TwitchSource:
    name = "twitch"

    def __init__(self, settings: Settings, http: httpx.Client | None = None) -> None:
        self.settings = settings
        self.cfg = settings.config.sources.twitch
        self._client_id = (settings.secrets.twitch_client_id or "").strip()
        self._client_secret = (settings.secrets.twitch_client_secret or "").strip()
        self._http = http or httpx.Client(timeout=DEFAULT_TIMEOUT, headers={"User-Agent": "wdvi/0.1"})
        self._token: str | None = None
        self._token_valid_until = 0.0  # time.monotonic()
        self._game_ids: dict[str, str] = {}  # Spielname (lowercase) -> game_id
        self._logins: dict[str, str] = {}  # broadcaster_id -> login (lowercase)
        # injizierbar für Tests
        self._sleep = time.sleep
        self._clock = time.monotonic

    # ------------------------------------------------------------------ öffentlich

    def is_configured(self) -> bool:
        return bool(self._client_id and self._client_secret)

    def discover(self, now: datetime) -> list[CandidateIn]:
        """Clips der Spiele aus config.sources.twitch.game_names, Fenster [now-lookback, now].

        - extra muss enthalten: broadcaster_id, broadcaster_login (lowercase, falls ableitbar),
          video_id, vod_offset, clip_creator_name, is_featured
        - creator_name = broadcaster_name, creator_url = https://twitch.tv/<login>
        - Filter: languages, min_views
        """
        if not self.is_configured():
            raise SourceError("Twitch ist nicht konfiguriert: WDVI_TWITCH_CLIENT_ID und "
                              "WDVI_TWITCH_CLIENT_SECRET in der .env eintragen.")
        if not self.cfg.game_names:
            raise SourceError("Twitch: keine Spiele konfiguriert (config.yaml: sources.twitch.game_names).")

        now = as_utc(now)
        started = now - timedelta(hours=max(1, self.cfg.lookback_hours))
        game_ids = self._resolve_games()
        budget = max(1, self.cfg.max_clips_per_poll)

        clips: dict[str, dict] = {}
        for game_id in game_ids:
            if len(clips) >= budget:
                break
            for clip in self._fetch_clips(game_id, started, now, budget - len(clips)):
                clips.setdefault(str(clip.get("id")), clip)

        self._resolve_logins({str(c.get("broadcaster_id")) for c in clips.values() if c.get("broadcaster_id")})
        out = [cand for c in clips.values() if (cand := self._to_candidate(c)) is not None]
        out.sort(key=lambda c: c.view_count or 0, reverse=True)
        log.info("twitch: %d clips kept (games=%s)", len(out), ",".join(game_ids))
        return out[:budget]

    def clip_broadcasters(self, slugs: list[str]) -> dict[str, tuple[str, str | None]]:
        """Clip-Slug -> (broadcaster_name, login) über /helix/clips?id=… (je 100 Slugs pro Aufruf).

        Für Reddit-Posts, die auf einen Clip verweisen, dessen URL den Kanal nicht nennt
        (clips.twitch.tv/<slug>). Unbekannte/gelöschte Clips fehlen im Ergebnis; API-Fehler -> SourceError.
        """
        if not self.is_configured():
            return {}
        uniq = list(dict.fromkeys(s.strip() for s in slugs if s and s.strip()))
        clips: list[dict] = []
        for i in range(0, len(uniq), 100):
            body = self._api_get("clips", [("id", s) for s in uniq[i:i + 100]])
            clips.extend(c for c in (body.get("data") or []) if isinstance(c, dict) and c.get("id"))
        self._resolve_logins({str(c.get("broadcaster_id")) for c in clips if c.get("broadcaster_id")})
        out: dict[str, tuple[str, str | None]] = {}
        for c in clips:
            login = self._login_for(c)
            name = str(c.get("broadcaster_name") or login or "").strip()
            if name:
                out[str(c["id"])] = (name, login)
        return out

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------ Auth

    def _get_token(self) -> str:
        if self._token and self._clock() < self._token_valid_until:
            return self._token
        try:
            r = self._http.post(TOKEN_URL, data={"client_id": self._client_id,
                                                 "client_secret": self._client_secret,
                                                 "grant_type": "client_credentials"})
        except httpx.HTTPError as e:
            raise SourceError(f"Twitch-Anmeldung nicht erreichbar ({type(e).__name__}) – "
                              "Internetverbindung prüfen.") from e
        body = json_or_none(r)
        if r.status_code != 200 or not isinstance(body, dict) or not body.get("access_token"):
            detail = body.get("message") if isinstance(body, dict) else None
            raise SourceError(f"Twitch-Anmeldung fehlgeschlagen (HTTP {r.status_code}"
                              f"{': ' + str(detail) if detail else ''}). Bitte WDVI_TWITCH_CLIENT_ID und "
                              "WDVI_TWITCH_CLIENT_SECRET prüfen (dev.twitch.tv/console).")
        expires_in = to_float(body.get("expires_in")) or 3600.0
        self._token = str(body["access_token"])
        self._token_valid_until = self._clock() + max(60.0, expires_in - _TOKEN_SAFETY_S)
        log.debug("twitch: new app access token (valid %.0f s)", expires_in)
        return self._token

    # ------------------------------------------------------------------ HTTP

    def _api_get(self, path: str, params: list[tuple[str, str]] | dict[str, Any]) -> dict:
        """GET auf Helix mit Token-Refresh (einmal bei 401), 429-Wartezeit und einem Retry bei 5xx/Netz."""
        url = f"{API_BASE}/{path}"
        refreshed = False
        ratelimit_retries = 0
        transient_retries = 0
        while True:
            headers = {"Client-Id": self._client_id, "Authorization": f"Bearer {self._get_token()}"}
            try:
                r = self._http.get(url, params=params, headers=headers)
            except httpx.HTTPError as e:
                if transient_retries < 1:
                    transient_retries += 1
                    self._sleep(2.0)
                    continue
                raise SourceError(f"Twitch-API nicht erreichbar ({type(e).__name__}) – "
                                  "Internetverbindung prüfen.") from e

            if r.status_code == 401 and not refreshed:
                log.info("twitch: 401 on %s, refreshing token", path)
                self._token = None
                refreshed = True
                continue
            if r.status_code == 429 and ratelimit_retries < _MAX_429_RETRIES:
                ratelimit_retries += 1
                wait = wait_from_headers(r, epoch_header="Ratelimit-Reset")
                log.warning("twitch: rate limited, waiting %.0f s", wait)
                self._sleep(wait)
                continue
            if r.status_code >= 500 and transient_retries < 1:
                transient_retries += 1
                self._sleep(2.0)
                continue

            body = json_or_none(r)
            if r.status_code == 401:
                raise SourceError("Twitch lehnt den Zugriff ab (HTTP 401) – Client-ID/Secret prüfen.")
            if r.status_code == 429:
                raise SourceError("Twitch-Rate-Limit erreicht (HTTP 429) – nächster Versuch beim nächsten Poll.")
            if r.status_code >= 400:
                detail = body.get("message") if isinstance(body, dict) else None
                raise SourceError(f"Twitch-API-Fehler bei /{path} (HTTP {r.status_code}"
                                  f"{': ' + str(detail) if detail else ''}).")
            if not isinstance(body, dict):
                raise SourceError(f"Twitch-API lieferte keine gültige JSON-Antwort für /{path}.")
            return body

    # ------------------------------------------------------------------ Spiele / Clips / Nutzer

    def _resolve_games(self) -> list[str]:
        """Spielnamen -> game_ids (gecacht). Reine Ziffernfolgen gelten direkt als game_id."""
        missing: list[str] = []
        for name in self.cfg.game_names:
            n = name.strip()
            if not n:
                continue
            if n.isdigit():
                self._game_ids[n.lower()] = n
            elif n.lower() not in self._game_ids:
                missing.append(n)
        for i in range(0, len(missing), 100):
            chunk = missing[i:i + 100]
            body = self._api_get("games", [("name", n) for n in chunk])
            for g in body.get("data") or []:
                if isinstance(g, dict) and g.get("id") and g.get("name"):
                    self._game_ids[str(g["name"]).lower()] = str(g["id"])
        ids: list[str] = []
        for name in self.cfg.game_names:
            gid = self._game_ids.get(name.strip().lower())
            if gid is None:
                log.warning("twitch: game %r not found on Twitch", name)
            elif gid not in ids:
                ids.append(gid)
        if not ids:
            raise SourceError("Twitch kennt keines der konfigurierten Spiele "
                              f"({', '.join(self.cfg.game_names)}) – Schreibweise in "
                              "sources.twitch.game_names prüfen (exakter Kategoriename).")
        return ids

    def _fetch_clips(self, game_id: str, started: datetime, ended: datetime, limit: int) -> list[dict]:
        """Clips (Helix sortiert absteigend nach Views) mit Cursor-Paginierung bis `limit` passende Clips."""
        out: list[dict] = []
        cursor: str | None = None
        for _page in range(_MAX_PAGES):
            params: dict[str, Any] = {"game_id": game_id, "started_at": iso_z(started),
                                      "ended_at": iso_z(ended), "first": 100}
            if cursor:
                params["after"] = cursor
            body = self._api_get("clips", params)
            data = [c for c in (body.get("data") or []) if isinstance(c, dict) and c.get("id")]
            out.extend(c for c in data if self._passes_filters(c))
            if len(out) >= limit:
                return out[:limit]
            cursor = (body.get("pagination") or {}).get("cursor")
            if not cursor or not data:
                break
            # sortiert nach Views: unter min_views kommt nichts Brauchbares mehr
            if (to_int(data[-1].get("view_count")) or 0) < self.cfg.min_views:
                break
        return out

    def _passes_filters(self, clip: dict) -> bool:
        if (to_int(clip.get("view_count")) or 0) < self.cfg.min_views:
            return False
        langs = [x.lower().split("-")[0] for x in self.cfg.languages if x]
        if langs:
            lang = str(clip.get("language") or "").lower().split("-")[0]
            if lang not in langs:
                return False
        return True

    def _resolve_logins(self, broadcaster_ids: set[str]) -> None:
        """broadcaster_id -> login über /helix/users (je 100 IDs, gecacht). Fehler sind nicht fatal."""
        missing = sorted(b for b in broadcaster_ids if b not in self._logins)
        for i in range(0, len(missing), 100):
            chunk = missing[i:i + 100]
            try:
                body = self._api_get("users", [("id", b) for b in chunk])
            except SourceError as e:
                log.warning("twitch: login lookup failed (%s) – falling back to display names", e)
                return
            for u in body.get("data") or []:
                if isinstance(u, dict) and u.get("id") and u.get("login"):
                    self._logins[str(u["id"])] = str(u["login"]).lower()

    def _login_for(self, clip: dict) -> str | None:
        bid = str(clip.get("broadcaster_id") or "")
        if bid in self._logins:
            return self._logins[bid]
        # Fallback: Anzeigename ist meist der Login mit anderer Groß-/Kleinschreibung
        name = str(clip.get("broadcaster_name") or "")
        return name.lower() if _LOGIN_RE.match(name) else None

    def _to_candidate(self, clip: dict) -> CandidateIn | None:
        created = parse_dt(clip.get("created_at"))
        url = clip.get("url") or (f"https://clips.twitch.tv/{clip['id']}" if clip.get("id") else None)
        if created is None or not url:
            log.debug("twitch: skipping malformed clip %r", clip.get("id"))
            return None
        login = self._login_for(clip)
        broadcaster_name = str(clip.get("broadcaster_name") or login or "Twitch-Streamer")
        language = str(clip.get("language") or "").lower() or None
        return CandidateIn(
            source=SourceKind.twitch_clip,
            source_id=str(clip["id"]),
            url=str(url),
            title=str(clip.get("title") or "").strip() or f"Clip von {broadcaster_name}",
            creator_name=broadcaster_name,
            creator_handle=login,
            creator_url=f"https://twitch.tv/{login}" if login else None,
            platform_label="Twitch",
            created_at=created,
            duration_s=to_float(clip.get("duration")),
            view_count=to_int(clip.get("view_count")),
            language=language if language and language != "other" else None,
            thumbnail_url=clip.get("thumbnail_url") or None,
            extra={
                "broadcaster_id": str(clip.get("broadcaster_id") or ""),
                "broadcaster_login": login,
                "video_id": str(clip.get("video_id") or ""),
                "vod_offset": to_int(clip.get("vod_offset")),
                "clip_creator_name": clip.get("creator_name"),
                "is_featured": bool(clip.get("is_featured")),
                "game_id": str(clip.get("game_id") or ""),
                "embed_url": clip.get("embed_url"),
            },
        )
