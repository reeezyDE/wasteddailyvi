"""Reddit: virale Video-Posts aus r/GTA6 (OAuth application-only). Verweist ein Post auf einen
Twitch-/Kick-/YouTube-Clip, wird der ORIGINAL-Creator gutgeschrieben (platform_label = verlinkte
Plattform, Kanal aus der Link-URL bzw. per Twitch-API/YouTube-oEmbed); der Reddit-Poster bleibt in
extra.via. Nach dem Download kann die Pipeline mit credit_linked_creator(c, info) nachschärfen."""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from wdvi.config import Settings
from wdvi.schemas import CandidateIn, SourceKind
from wdvi.sources._util import DEFAULT_TIMEOUT, json_or_none, parse_dt, to_float, to_int, wait_from_headers
from wdvi.sources.base import SourceError
from wdvi.sources.twitch import TwitchSource

log = logging.getLogger(__name__)

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
OAUTH_BASE = "https://oauth.reddit.com"
PUBLIC_BASE = "https://www.reddit.com"

OEMBED_URL = "https://www.youtube.com/oembed"

_TOKEN_SAFETY_S = 300.0
_MAX_429_RETRIES = 1
_LEAK_TITLE_RE = re.compile(r"\bleak(?:s|ed|er)?\b", re.IGNORECASE)

# Plattformen, deren Original-Creator statt des Reddit-Posters gutgeschrieben wird (Streamable: kein Creator)
CREDIT_PLATFORMS = ("Twitch", "Kick", "YouTube")
_MAX_OEMBED_PER_POLL = 25  # Discovery muss schnell bleiben; der Rest kommt beim nächsten Poll
_CREATOR_CACHE_MAX = 2000
_TWITCH_LOGIN_RE = re.compile(r"^[A-Za-z0-9_]{2,25}$")
_TWITCH_SLUG_RE = re.compile(r"^[A-Za-z0-9_-]{3,120}$")  # ungültige IDs würden den Helix-Batch scheitern lassen
_KICK_SLUG_RE = re.compile(r"^[A-Za-z0-9_-]{2,40}$")
_KICK_NOT_CHANNELS = {"video", "videos", "clip", "clips", "categories", "category", "browse", "search",
                      "following", "dashboard", "settings"}
_YT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

LinkedCreator = tuple[str, str | None, str | None]  # (Anzeigename, Handle, Kanal-URL)


class RedditSource:
    name = "reddit"

    def __init__(self, settings: Settings, http: httpx.Client | None = None) -> None:
        self.settings = settings
        self.cfg = settings.config.sources.reddit
        self._client_id = (settings.secrets.reddit_client_id or "").strip()
        self._client_secret = (settings.secrets.reddit_client_secret or "").strip()
        self._http = http or httpx.Client(timeout=DEFAULT_TIMEOUT, follow_redirects=True)
        self._token: str | None = None
        self._token_valid_until = 0.0
        self._sleep = time.sleep
        self._clock = time.monotonic
        self._twitch: TwitchSource | None = None
        # "twitch:<slug>" / "youtube:<id>" -> Creator (None = endgültig unbekannt); Polls wiederholen Posts
        self._creator_cache: dict[str, LinkedCreator | None] = {}

    # ------------------------------------------------------------------ öffentlich

    def is_configured(self) -> bool:
        # ohne OAuth-Credentials wird die öffentliche JSON-Schnittstelle genutzt
        return bool([s for s in self.cfg.subreddits if s and s.strip()])

    @property
    def _use_oauth(self) -> bool:
        return bool(self._client_id and self._client_secret)

    def discover(self, now: datetime) -> list[CandidateIn]:
        subs = [s.strip().removeprefix("r/").removeprefix("/r/") for s in self.cfg.subreddits if s and s.strip()]
        if not subs:
            raise SourceError("Reddit: keine Subreddits konfiguriert (config.yaml: sources.reddit.subreddits).")
        listings = list(dict.fromkeys(self.cfg.listings)) or ["hot"]

        found: dict[str, CandidateIn] = {}
        errors: list[SourceError] = []
        for sub in subs:
            for listing in listings:
                try:
                    posts = self._fetch_listing(sub, listing)
                except SourceError as e:
                    log.warning("reddit: r/%s/%s failed: %s", sub, listing, e)
                    errors.append(e)
                    if isinstance(e, _AuthError):
                        raise
                    continue
                for post in posts:
                    cand = self._to_candidate(post)
                    if cand is not None:
                        found.setdefault(cand.source_id, cand)
        if errors and len(errors) == len(subs) * len(listings):
            raise errors[0]
        out = sorted(self._credit_linked(list(found.values())),
                     key=lambda c: c.engagement.get("score", 0.0), reverse=True)
        log.info("reddit: %d posts kept (oauth=%s)", len(out), self._use_oauth)
        return out

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------ HTTP

    def _get_token(self) -> str:
        if self._token and self._clock() < self._token_valid_until:
            return self._token
        try:
            r = self._http.post(TOKEN_URL, data={"grant_type": "client_credentials"},
                                auth=(self._client_id, self._client_secret),
                                headers={"User-Agent": self.cfg.user_agent})
        except httpx.HTTPError as e:
            raise SourceError(f"Reddit-Anmeldung nicht erreichbar ({type(e).__name__}) – "
                              "Internetverbindung prüfen.") from e
        body = json_or_none(r)
        if r.status_code != 200 or not isinstance(body, dict) or not body.get("access_token"):
            detail = body.get("error") if isinstance(body, dict) else None
            raise _AuthError(f"Reddit-Anmeldung fehlgeschlagen (HTTP {r.status_code}"
                             f"{': ' + str(detail) if detail else ''}). Bitte WDVI_REDDIT_CLIENT_ID und "
                             "WDVI_REDDIT_CLIENT_SECRET prüfen (App-Typ 'script' oder 'web app').")
        expires_in = to_float(body.get("expires_in")) or 3600.0
        self._token = str(body["access_token"])
        self._token_valid_until = self._clock() + max(60.0, expires_in - _TOKEN_SAFETY_S)
        return self._token

    def _fetch_listing(self, sub: str, listing: str) -> list[dict]:
        params: dict[str, Any] = {"limit": 100, "raw_json": 1}
        if listing == "top":
            params["t"] = self.cfg.top_time
        oauth = self._use_oauth
        url = f"{OAUTH_BASE}/r/{sub}/{listing}" if oauth else f"{PUBLIC_BASE}/r/{sub}/{listing}.json"

        refreshed = False
        ratelimit_retries = 0
        transient_retries = 0
        while True:
            headers = {"User-Agent": self.cfg.user_agent}
            if oauth:
                headers["Authorization"] = f"Bearer {self._get_token()}"
            try:
                r = self._http.get(url, params=params, headers=headers)
            except httpx.HTTPError as e:
                if transient_retries < 1:
                    transient_retries += 1
                    self._sleep(2.0)
                    continue
                raise SourceError(f"Reddit nicht erreichbar ({type(e).__name__}) – Internetverbindung prüfen.") \
                    from e
            if r.status_code == 401 and oauth and not refreshed:
                self._token = None
                refreshed = True
                continue
            if r.status_code == 429 and ratelimit_retries < _MAX_429_RETRIES:
                ratelimit_retries += 1
                wait = wait_from_headers(r, seconds_header="x-ratelimit-reset")
                log.warning("reddit: rate limited, waiting %.0f s", wait)
                self._sleep(wait)
                continue
            if r.status_code >= 500 and transient_retries < 1:
                transient_retries += 1
                self._sleep(2.0)
                continue
            if r.status_code in (403, 429) and not oauth:
                raise SourceError(f"Reddit blockiert anonyme Anfragen (HTTP {r.status_code}) – bitte "
                                  "WDVI_REDDIT_CLIENT_ID/WDVI_REDDIT_CLIENT_SECRET eintragen (reddit.com/prefs/apps).")
            if r.status_code == 404:
                raise SourceError(f"Subreddit r/{sub} nicht gefunden (HTTP 404).")
            if r.status_code == 403:
                raise SourceError(f"Kein Zugriff auf r/{sub} (HTTP 403 – privat/gesperrt?).")
            if r.status_code == 429:
                raise SourceError("Reddit-Rate-Limit erreicht (HTTP 429) – nächster Versuch beim nächsten Poll.")
            if r.status_code >= 400:
                raise SourceError(f"Reddit-Fehler bei r/{sub}/{listing} (HTTP {r.status_code}).")
            body = json_or_none(r)
            children = ((body or {}).get("data") or {}).get("children") if isinstance(body, dict) else None
            if not isinstance(children, list):
                raise SourceError(f"Reddit lieferte keine gültige Listing-Antwort für r/{sub}/{listing}.")
            return [c["data"] for c in children
                    if isinstance(c, dict) and c.get("kind") == "t3" and isinstance(c.get("data"), dict)]

    # ------------------------------------------------------------------ Filter / Mapping

    def _to_candidate(self, post: dict) -> CandidateIn | None:
        pid = str(post.get("id") or "").strip()
        author = str(post.get("author") or "").strip()
        if not pid or not author or author == "[deleted]":
            return None
        if post.get("over_18") or post.get("stickied") or post.get("removed_by_category"):
            return None
        score = to_int(post.get("score")) or 0
        if score < self.cfg.min_score:
            return None
        flair = str(post.get("link_flair_text") or "")
        if "leak" in flair.lower():
            log.debug("reddit: skipping %s (leak flair)", pid)
            return None  # Leaks -> Take-Two-Takedowns
        title = str(post.get("title") or "").strip()
        if self.settings.config.filters.leak_policy == "reject" and _LEAK_TITLE_RE.search(title):
            log.debug("reddit: skipping %s (leak in title)", pid)
            return None

        # Crossposts: Video steckt im Original-Post
        media_post = post
        parents = post.get("crosspost_parent_list")
        if isinstance(parents, list) and parents and isinstance(parents[0], dict):
            media_post = parents[0]

        reddit_video = _reddit_video(media_post) or _reddit_video(post)
        is_hosted_video = bool(
            reddit_video
            or media_post.get("is_video") or post.get("is_video")
            or media_post.get("post_hint") == "hosted:video"
            or str(media_post.get("domain") or "").lower() == "v.redd.it"
        )
        link = str(media_post.get("url_overridden_by_dest") or media_post.get("url") or post.get("url") or "")
        linked_platform = _linked_platform(link)
        if not is_hosted_video and linked_platform is None:
            return None

        created = parse_dt(post.get("created_utc"))
        if created is None:
            return None
        permalink = str(post.get("permalink") or f"/comments/{pid}/")
        page_url = f"https://www.reddit.com{permalink}" if permalink.startswith("/") else permalink

        extra: dict[str, Any] = {
            "subreddit": post.get("subreddit"),
            "permalink": page_url,
            "domain": media_post.get("domain") or post.get("domain"),
            "flair": flair or None,
            "post_hint": media_post.get("post_hint"),
            "author": author,
        }
        if reddit_video:
            extra["media_url"] = reddit_video.get("hls_url") or reddit_video.get("fallback_url")
            extra["fallback_url"] = reddit_video.get("fallback_url")
        if not is_hosted_video and linked_platform:
            extra["linked_url"] = link
            if linked_platform != "Streamable":  # nur echte Creator-Plattformen werden gutgeschrieben
                extra["linked_platform"] = linked_platform

        cand = CandidateIn(
            source=SourceKind.reddit_post,
            source_id=pid,
            url=page_url,
            title=title or "Reddit-Clip",
            creator_name=f"u/{author}",
            creator_handle=author,
            creator_url=f"https://reddit.com/u/{author}",
            platform_label="Reddit",
            created_at=created,
            duration_s=to_float(reddit_video.get("duration")) if reddit_video else None,
            view_count=None,
            engagement={
                "score": float(score),
                "num_comments": float(to_int(post.get("num_comments")) or 0),
                "upvote_ratio": to_float(post.get("upvote_ratio")) or 0.0,
            },
            thumbnail_url=_thumbnail(post) or _thumbnail(media_post),
            extra=extra,
        )
        return credit_linked_creator(cand)  # Kanal steht in der Link-URL? -> Original-Creator

    # ------------------------------------------------------------------ Original-Creator verlinkter Clips

    def _credit_linked(self, cands: list[CandidateIn]) -> list[CandidateIn]:
        """Original-Creator per API nachschlagen, wo die Link-URL ihn nicht (genau) nennt:
        Twitch-Clips über Helix (nur mit Twitch-Credentials, liefert auch den Anzeigenamen),
        YouTube über oEmbed (ohne API-Key). Fehler sind nicht fatal – dann bleibt der bisherige Credit."""
        linked = [c for c in cands if (c.extra or {}).get("linked_platform") in CREDIT_PLATFORMS]
        if not linked:
            return cands
        self._lookup_twitch([s for c in linked if (s := twitch_clip_slug(str(c.extra.get("linked_url") or "")))])
        self._lookup_youtube([v for c in linked if c.platform_label == "Reddit"
                              and (v := youtube_video_id(str(c.extra.get("linked_url") or "")))])
        out: list[CandidateIn] = []
        for c in cands:
            platform = (c.extra or {}).get("linked_platform")
            url = str((c.extra or {}).get("linked_url") or "")
            key = None
            if platform == "Twitch" and (slug := twitch_clip_slug(url)):
                key = f"twitch:{slug}"
            elif platform == "YouTube" and (vid := youtube_video_id(url)):
                key = f"youtube:{vid}"
            creator = self._creator_cache.get(key) if key else None
            out.append(_with_creator(c, str(platform), creator) if creator else c)
        return out

    def _remember(self, key: str, creator: LinkedCreator | None) -> None:
        if len(self._creator_cache) >= _CREATOR_CACHE_MAX:
            self._creator_cache.clear()
        self._creator_cache[key] = creator

    def _lookup_twitch(self, slugs: list[str]) -> None:
        missing = [s for s in dict.fromkeys(slugs) if f"twitch:{s}" not in self._creator_cache]
        if not missing:
            return
        if self._twitch is None:
            self._twitch = TwitchSource(self.settings, http=self._http)
            self._twitch._sleep = self._sleep
            self._twitch._clock = self._clock
        if not self._twitch.is_configured():
            return
        try:
            found = self._twitch.clip_broadcasters(missing)
        except SourceError as e:
            log.warning("reddit: twitch clip lookup failed (%s) – keeping link/Reddit credit", e)
            return
        by_lower = {k.lower(): v for k, v in found.items()}
        for slug in missing:
            hit = found.get(slug) or by_lower.get(slug.lower())
            creator: LinkedCreator | None = None
            if hit:
                name, login = hit
                creator = (name, login, f"https://twitch.tv/{login}" if login else None)
            self._remember(f"twitch:{slug}", creator)  # nicht gefunden = gelöscht -> nicht erneut fragen

    def _lookup_youtube(self, video_ids: list[str]) -> None:
        missing = [v for v in dict.fromkeys(video_ids) if f"youtube:{v}" not in self._creator_cache]
        for vid in missing[:_MAX_OEMBED_PER_POLL]:
            try:
                r = self._http.get(OEMBED_URL, params={"url": f"https://www.youtube.com/watch?v={vid}",
                                                       "format": "json"},
                                   headers={"User-Agent": self.cfg.user_agent})
            except httpx.HTTPError as e:
                log.warning("reddit: youtube oembed for %s failed (%s)", vid, type(e).__name__)
                return  # Netz weg -> nicht 25x in den Timeout laufen
            body = json_or_none(r)
            if r.status_code == 200 and isinstance(body, dict):
                self._remember(f"youtube:{vid}", _creator_from_oembed(body))
            elif r.status_code in (400, 401, 403, 404):  # privat/gelöscht/Einbetten verboten
                self._remember(f"youtube:{vid}", None)
            else:  # 429/5xx: beim nächsten Poll erneut versuchen
                log.warning("reddit: youtube oembed for %s -> HTTP %s", vid, r.status_code)
                return


# --------------------------------------------------------------------------- Credit an den Original-Creator


def credit_linked_creator(c: CandidateIn, info: dict | None = None) -> CandidateIn:
    """Reddit-Post, der auf einen Twitch-/Kick-/YouTube-Clip verweist -> Credit an den ORIGINAL-Creator
    (creator_* und platform_label der verlinkten Plattform); der Reddit-Poster bleibt in extra.via.

    Quelle: yt-dlp-Infos des heruntergeladenen Clips (`info`, falls übergeben) vor dem Kanal in der
    Link-URL (twitch.tv/<login>/clip/…, kick.com/<slug>/clips/…). Ist keiner ermittelbar, bleibt der
    Kandidat unverändert. Alle anderen Kandidaten kommen unverändert zurück.
    """
    extra = c.extra or {}
    platform = extra.get("linked_platform")
    if c.source != SourceKind.reddit_post or platform not in CREDIT_PLATFORMS:
        return c
    creator = linked_creator_from_info(str(platform), info) if info else None
    if creator is not None and c.platform_label == platform:
        # Info ohne Login (z. B. nicht-lateinischer Anzeigename): bekannten Handle/Kanal-Link behalten
        creator = (creator[0], creator[1] or c.creator_handle, creator[2] or c.creator_url)
    if creator is None:
        if c.platform_label != "Reddit":
            return c  # schon gutgeschrieben (evtl. mit genauerem Anzeigenamen aus der API)
        creator = linked_creator_from_url(str(extra.get("linked_url") or ""))
    return _with_creator(c, str(platform), creator) if creator else c


def linked_creator_from_url(url: str) -> LinkedCreator | None:
    """Kanal aus der Clip-URL selbst; None, wenn die URL ihn nicht nennt (clips.twitch.tv/<slug>, YouTube)."""
    parts = _split_url(url)
    if parts is None:
        return None
    host, segs, query = parts
    if host == "twitch.tv" and len(segs) >= 3 and segs[1].lower() == "clip" and _TWITCH_LOGIN_RE.match(segs[0]):
        login = segs[0].lower()
        return segs[0], login, f"https://twitch.tv/{login}"
    if host == "kick.com" and segs and segs[0].lower() not in _KICK_NOT_CHANNELS and _KICK_SLUG_RE.match(segs[0]):
        if (len(segs) >= 3 and segs[1].lower() in ("clips", "videos")) or "clip" in query:
            slug = segs[0].lower()
            return segs[0], slug, f"https://kick.com/{slug}"
    return None


def linked_creator_from_info(platform: str, info: dict | None) -> LinkedCreator | None:
    """Original-Creator aus den yt-dlp-Infos des heruntergeladenen Clips.

    Achtung: bei Twitch-/Kick-Clips ist yt-dlps 'uploader' der CLIPPER, nicht der Streamer – dort zählt
    nur 'channel' (Twitch: Anzeigename, Kick: Slug). Bei YouTube: channel/uploader, uploader_id = @handle.
    """
    if not isinstance(info, dict):
        return None
    plat = (platform or "").strip().lower()
    channel = _str(info.get("channel"))
    if plat == "twitch":
        creators = info.get("creators")
        name = channel or (_str(creators[0]) if isinstance(creators, list) and creators else "") \
            or _str(info.get("creator"))
        if not name:
            return None
        login = name.lower() if _TWITCH_LOGIN_RE.match(name) else None
        return name, login, f"https://twitch.tv/{login}" if login else None
    if plat == "kick":
        if not channel:
            return None
        slug = channel.lower() if _KICK_SLUG_RE.match(channel) else None
        return channel, slug, f"https://kick.com/{slug}" if slug else None
    if plat == "youtube":
        name = channel or _str(info.get("uploader"))
        if not name:
            return None
        uploader_id = _str(info.get("uploader_id"))
        handle = uploader_id[1:].lower() if uploader_id.startswith("@") and len(uploader_id) > 1 else None
        uploader_url = _str(info.get("uploader_url"))
        url = uploader_url if "/@" in uploader_url else (_str(info.get("channel_url")) or uploader_url)
        return name, handle, url or None
    return None


def twitch_clip_slug(url: str) -> str | None:
    """clips.twitch.tv/<slug>, clips.twitch.tv/embed?clip=<slug>, twitch.tv/<login>/clip/<slug> -> slug."""
    parts = _split_url(url)
    if parts is None:
        return None
    host, segs, query = parts
    slug: str | None = None
    if host == "clips.twitch.tv" and segs:
        slug = (query.get("clip") or [None])[0] if segs[0].lower() == "embed" else segs[0]
    elif host == "twitch.tv" and "clip" in [s.lower() for s in segs]:
        i = [s.lower() for s in segs].index("clip")
        slug = segs[i + 1] if len(segs) > i + 1 else None
    slug = (slug or "").strip()
    return slug if _TWITCH_SLUG_RE.match(slug) else None


def youtube_video_id(url: str) -> str | None:
    """youtu.be/<id>, youtube.com/watch?v=<id>, youtube.com/shorts/<id> -> Video-ID."""
    parts = _split_url(url)
    if parts is None:
        return None
    host, segs, query = parts
    vid: str | None = None
    if host == "youtu.be" and segs:
        vid = segs[0]
    elif host == "youtube.com" and segs[:1] == ["watch"]:
        vid = (query.get("v") or [None])[0]
    elif host == "youtube.com" and len(segs) >= 2 and segs[0] == "shorts":
        vid = segs[1]
    return vid if vid and _YT_ID_RE.match(vid) else None


def _with_creator(c: CandidateIn, platform: str, creator: LinkedCreator) -> CandidateIn:
    name, handle, url = creator
    extra = dict(c.extra or {})
    author = str(extra.get("author") or (c.creator_handle if c.platform_label == "Reddit" else "") or "").strip()
    if author:
        extra["via"] = f"u/{author}"  # Reddit-Poster, z. B. für "(via u/x on Reddit)"
        extra["via_url"] = f"https://reddit.com/u/{author}"
    return c.model_copy(update={"creator_name": name, "creator_handle": handle, "creator_url": url,
                                "platform_label": platform, "extra": extra})


def _creator_from_oembed(body: dict) -> LinkedCreator | None:
    name = _str(body.get("author_name"))
    if not name:
        return None
    url = _str(body.get("author_url"))
    m = re.search(r"youtube\.com/@([^/?#]+)", url)
    return name, (m.group(1).lower() if m else None), url or None


def _split_url(url: str) -> tuple[str, list[str], dict[str, list[str]]] | None:
    try:
        p = urlparse(url.strip())
    except (ValueError, AttributeError):
        return None
    host = (p.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    if not host:
        return None
    return host, [s for s in (p.path or "").split("/") if s], parse_qs(p.query or "")


def _str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


class _AuthError(SourceError):
    pass


def _reddit_video(post: dict) -> dict | None:
    for key in ("secure_media", "media"):
        media = post.get(key)
        if isinstance(media, dict) and isinstance(media.get("reddit_video"), dict):
            return media["reddit_video"]
    return None


def _linked_platform(url: str) -> str | None:
    """Link auf einen Clip/ein Video bei Twitch/Kick/YouTube/Streamable -> Plattformname; sonst None.

    Bewusst NICHT: Kanal-/Live-Links (würden einen endlosen Livestream laden) und Twitch-VODs.
    """
    try:
        p = urlparse(url)
    except ValueError:
        return None
    host = (p.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    path = p.path or ""
    query = p.query or ""
    if (host == "clips.twitch.tv" and path.strip("/")) or (host == "twitch.tv" and "/clip/" in path):
        return "Twitch"
    if host == "kick.com" and ("/clips/" in path or "clip=" in query or "/videos/" in path):
        return "Kick"
    if host == "youtu.be" and path.strip("/"):
        return "YouTube"
    if host == "youtube.com" and ((path == "/watch" and "v=" in query) or path.startswith("/shorts/")):
        return "YouTube"
    if host == "streamable.com" and path.strip("/"):
        return "Streamable"
    return None


def _thumbnail(post: dict) -> str | None:
    preview = post.get("preview")
    if isinstance(preview, dict):
        images = preview.get("images")
        if isinstance(images, list) and images and isinstance(images[0], dict):
            src = images[0].get("source") or {}
            if isinstance(src, dict) and str(src.get("url") or "").startswith("http"):
                return str(src["url"])
    thumb = str(post.get("thumbnail") or "")
    return thumb if thumb.startswith("http") else None
