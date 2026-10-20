"""YouTube: neue Videos von Kanälen (RSS, ohne API-Key) + optionale Suche (Data API).

Lange Videos werden als SourceKind.youtube_video gemeldet und später per `fetch_video_context`
+ brain.editor.mine_moments in youtube_moment-Kandidaten (Ausschnitte) zerlegt.

Quota: search.list kostet 100 Einheiten, videos.list 1 Einheit (Standard: 10 000/Tag pro Projekt,
Upload = 1600). Die Suche läuft deshalb höchstens alle `SEARCH_MIN_INTERVAL` (Zustand in
cache_dir/youtube_search_state.json) und pausiert nach `quotaExceeded` bis Mitternacht (Pazifikzeit).
Tipp: für den API-Key ein eigenes Google-Cloud-Projekt nehmen, damit Uploads nicht leiden.
"""

from __future__ import annotations

import html
import json
import logging
import re
import time as time_mod
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import feedparser
import httpx
from pydantic import BaseModel, Field

from wdvi.config import Settings
from wdvi.schemas import CandidateIn, Segment, SourceKind, Transcript, Word
from wdvi.sources._util import DEFAULT_TIMEOUT, as_utc, iso_z, json_or_none, parse_dt, to_float, to_int
from wdvi.sources.base import SourceError

log = logging.getLogger(__name__)

RSS_URL = "https://www.youtube.com/feeds/videos.xml"
API_BASE = "https://www.googleapis.com/youtube/v3"
SEARCH_MIN_INTERVAL = timedelta(hours=6)
_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/131.0.0.0 Safari/537.36")
_CAPTION_LANGS = ("en-orig", "en")  # automatische Untertitel, in dieser Reihenfolge
_MANUAL_LANGS = ("en", "en-US", "en-GB")
_MAX_WORD_S = 1.5
_MAX_DESCRIPTION = 5000


class HeatPoint(BaseModel):
    start: float
    end: float
    value: float  # 0..1 ("Most replayed")


class VideoContext(BaseModel):
    """Alles, was Claude braucht, um in einem langen Video Momente zu finden – ohne Video-Download."""

    video_id: str
    title: str
    channel: str
    duration: float
    description: str = ""
    chapters: list[dict] = Field(default_factory=list)  # [{"start_time":..,"end_time":..,"title":..}]
    heatmap: list[HeatPoint] = Field(default_factory=list)
    transcript: Transcript | None = None  # aus automatischen Untertiteln (Segment-Ebene genügt)
    view_count: int | None = None


class YouTubeSource:
    name = "youtube"

    def __init__(self, settings: Settings, http: httpx.Client | None = None) -> None:
        self.settings = settings
        self.cfg = settings.config.sources.youtube
        self._api_key = (settings.secrets.youtube_api_key or "").strip()
        self._http = http or httpx.Client(timeout=DEFAULT_TIMEOUT, follow_redirects=True,
                                          headers={"User-Agent": _BROWSER_UA,
                                                   "Accept-Language": "en-US,en;q=0.9"})
        self._details: dict[str, dict] = {}  # video_id -> {duration_s, views, likes, comments}
        self._state_path: Path = settings.config.paths.cache_dir / "youtube_search_state.json"
        self._warned_no_key = False
        self._warned_ids: set[str] = set()
        self._sleep = time_mod.sleep  # injizierbar für Tests

    # ------------------------------------------------------------------ öffentlich

    def is_configured(self) -> bool:
        return bool(self._channel_ids() or (self.cfg.search_queries and self._api_key))

    def discover(self, now: datetime) -> list[CandidateIn]:
        """RSS https://www.youtube.com/feeds/videos.xml?channel_id=... je Kanal (+ search.list wenn Key).

        Nur Videos jünger als max_age_hours. duration ggf. unbekannt (None) – Mining prüft später.
        Shorts (/shorts/ URL oder < min_video_minutes) werden NICHT gemeldet.
        """
        if not self.is_configured():
            raise SourceError("YouTube ist nicht konfiguriert: sources.youtube.channel_ids (UC…-IDs) eintragen "
                              "oder search_queries + WDVI_YOUTUBE_API_KEY setzen.")
        now = as_utc(now)
        cutoff = now - timedelta(hours=max(1, self.cfg.max_age_hours))
        found: dict[str, CandidateIn] = {}
        errors: list[SourceError] = []
        any_ok = False

        for ch in self._channel_ids():
            try:
                cands = self._fetch_rss(ch)
            except SourceError as e:
                log.warning("youtube: RSS for %s failed: %s", ch, e)
                errors.append(e)
                continue
            any_ok = True
            for c in cands:
                if c.created_at >= cutoff:
                    found.setdefault(c.source_id, c)

        if self._api_key and found:
            self._enrich(found)

        if self.cfg.search_queries:
            if not self._api_key:
                if not self._warned_no_key:
                    log.warning("youtube: search_queries set but WDVI_YOUTUBE_API_KEY missing – search skipped")
                    self._warned_no_key = True
            elif self._search_due(now):
                try:
                    for c in self._search_all(now):
                        if c.created_at >= cutoff:
                            found.setdefault(c.source_id, c)
                    any_ok = True
                except SourceError as e:
                    log.warning("youtube: search failed: %s", e)
                    errors.append(e)
            else:
                any_ok = any_ok or not self._channel_ids()

        if not any_ok and errors:
            raise errors[0]
        out = [c for c in found.values() if self._duration_ok(c.duration_s)]
        out.sort(key=lambda c: c.created_at, reverse=True)
        log.info("youtube: %d videos kept", len(out))
        return out

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------ RSS

    def _channel_ids(self) -> list[str]:
        out = []
        for ch in self.cfg.channel_ids:
            ch = (ch or "").strip()
            if not ch:
                continue
            if not ch.startswith("UC"):
                if ch not in self._warned_ids:
                    log.warning("youtube: channel id %r is not a UC… channel id – skipped", ch)
                    self._warned_ids.add(ch)
                continue
            out.append(ch)
        return out

    def _fetch_rss(self, channel_id: str) -> list[CandidateIn]:
        r: httpx.Response | None = None
        for attempt in range(2):  # der RSS-Endpunkt liefert gelegentlich kurzzeitig 5xx
            try:
                r = self._http.get(RSS_URL, params={"channel_id": channel_id})
            except httpx.HTTPError as e:
                if attempt == 0:
                    self._sleep(2.0)
                    continue
                raise SourceError(f"YouTube-RSS nicht erreichbar ({type(e).__name__}) – "
                                  "Internetverbindung prüfen.") from e
            if r.status_code >= 500 and attempt == 0:
                self._sleep(2.0)
                continue
            break
        if r is None:  # pragma: no cover – Schleife setzt r oder wirft
            raise SourceError(f"YouTube-RSS für Kanal {channel_id} nicht abrufbar.")
        if r.status_code == 404:
            raise SourceError(f"YouTube-Kanal {channel_id} nicht gefunden (RSS 404) – channel_id prüfen "
                              "(UC…-ID aus der Kanal-URL; YouTube liefert selten auch vorübergehend 404).")
        if r.status_code >= 400:
            raise SourceError(f"YouTube-RSS für Kanal {channel_id} nicht abrufbar (HTTP {r.status_code}).")
        feed = feedparser.parse(r.content)
        if feed.get("bozo") and not feed.get("entries"):
            raise SourceError(f"YouTube-RSS für Kanal {channel_id} ist kein gültiger Feed.")
        feed_author = str((feed.get("feed") or {}).get("title") or "")
        out: list[CandidateIn] = []
        for e in feed.get("entries") or []:
            cand = self._rss_entry(e, channel_id, feed_author)
            if cand is not None:
                out.append(cand)
        return out

    def _rss_entry(self, e: Any, channel_id: str, feed_author: str) -> CandidateIn | None:
        link = str(e.get("link") or "")
        if "/shorts/" in link:
            return None
        vid = str(e.get("yt_videoid") or "").strip() or _video_id_from_url(link)
        if not vid:
            vid = str(e.get("id") or "").removeprefix("yt:video:").strip()
        created = parse_dt(e.get("published"))
        if created is None and e.get("published_parsed"):
            created = datetime(*e["published_parsed"][:6], tzinfo=UTC)
        if not vid or created is None:
            return None
        stats = e.get("media_statistics") or {}
        rating = e.get("media_starrating") or {}
        thumbs = e.get("media_thumbnail") or []
        thumb = thumbs[0].get("url") if thumbs and isinstance(thumbs[0], dict) else None
        likes = to_float(rating.get("count"))
        author = str(e.get("author") or feed_author or "YouTube")
        return CandidateIn(
            source=SourceKind.youtube_video,
            source_id=vid,
            url=f"https://www.youtube.com/watch?v={vid}",
            title=str(e.get("title") or "").strip() or "YouTube-Video",
            creator_name=author,
            creator_handle=None,
            creator_url=f"https://www.youtube.com/channel/{channel_id}",
            platform_label="YouTube",
            created_at=created,
            view_count=to_int(stats.get("views")),
            engagement={"likes": likes} if likes is not None else {},
            thumbnail_url=thumb or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
            extra={"channel_id": channel_id, "via": "rss"},
        )

    # ------------------------------------------------------------------ Data API

    def _api_get(self, endpoint: str, params: dict[str, Any]) -> dict:
        """GET auf die Data API. Achtung: URL enthält den Key – nie URL/Exception-Text loggen."""
        try:
            r = self._http.get(f"{API_BASE}/{endpoint}", params={**params, "key": self._api_key})
        except httpx.HTTPError as e:
            raise SourceError(f"YouTube-Data-API nicht erreichbar ({type(e).__name__}).") from None
        body = json_or_none(r)
        if r.status_code >= 400:
            err = body.get("error") if isinstance(body, dict) else None
            reasons = [str(x.get("reason")) for x in (err or {}).get("errors") or [] if isinstance(x, dict)]
            if any(x in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded") for x in reasons):
                raise _QuotaError("YouTube-API-Kontingent aufgebraucht (quotaExceeded) – Suche pausiert bis "
                                  "Mitternacht (Pazifikzeit).")
            if any(x in ("keyInvalid", "keyExpired") for x in reasons) or (
                    r.status_code == 400 and "api key" in str((err or {}).get("message", "")).lower()):
                raise SourceError("YouTube-API-Key ungültig – WDVI_YOUTUBE_API_KEY prüfen.")
            if any(x in ("accessNotConfigured", "SERVICE_DISABLED") for x in reasons):
                raise SourceError("YouTube Data API v3 ist im Google-Cloud-Projekt des Keys nicht aktiviert.")
            raise SourceError(f"YouTube-Data-API-Fehler bei {endpoint} (HTTP {r.status_code}"
                              f"{': ' + ', '.join(reasons) if reasons else ''}).")
        if not isinstance(body, dict):
            raise SourceError(f"YouTube-Data-API lieferte keine gültige JSON-Antwort ({endpoint}).")
        return body

    def _video_details(self, ids: list[str]) -> dict[str, dict]:
        """videos.list(contentDetails,statistics) für bis zu 50 IDs je Aufruf (1 Einheit), gecacht."""
        missing = [i for i in dict.fromkeys(ids) if i not in self._details]
        for i in range(0, len(missing), 50):
            chunk = missing[i:i + 50]
            body = self._api_get("videos", {"part": "contentDetails,statistics", "id": ",".join(chunk),
                                            "maxResults": 50})
            for item in body.get("items") or []:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                stats = item.get("statistics") or {}
                self._details[str(item["id"])] = {
                    "duration_s": parse_iso_duration((item.get("contentDetails") or {}).get("duration")),
                    "views": to_int(stats.get("viewCount")),
                    "likes": to_float(stats.get("likeCount")),
                    "comments": to_float(stats.get("commentCount")),
                }
        return {i: self._details[i] for i in ids if i in self._details}

    def _enrich(self, found: dict[str, CandidateIn]) -> None:
        """RSS-Videos um Dauer/Views ergänzen (billig, 1 Einheit je 50 Videos). Fehler sind nicht fatal."""
        try:
            details = self._video_details(list(found))
        except SourceError as e:
            log.warning("youtube: videos.list enrichment failed: %s", e)
            return
        for vid, d in details.items():
            found[vid] = _apply_details(found[vid], d)

    def _search_all(self, now: datetime) -> list[CandidateIn]:
        state = self._load_state()
        state["last_search"] = iso_z(now)
        try:
            out: list[CandidateIn] = []
            for q in self.cfg.search_queries:
                if q and q.strip():
                    out.extend(self._search(q.strip(), now))
            return out
        except _QuotaError:
            state["paused_until"] = iso_z(_next_pacific_midnight(now))
            raise
        finally:
            self._save_state(state)

    def _search(self, query: str, now: datetime) -> list[CandidateIn]:
        params: dict[str, Any] = {
            "part": "snippet", "q": query, "type": "video", "order": "viewCount",
            "publishedAfter": iso_z(now - timedelta(hours=max(1, self.cfg.max_age_hours))), "maxResults": 25,
        }
        langs = [x for x in self.settings.config.filters.languages if x]
        if langs:
            params["relevanceLanguage"] = langs[0]
        body = self._api_get("search", params)
        snippets: dict[str, dict] = {}
        for item in body.get("items") or []:
            if not isinstance(item, dict):
                continue
            vid = (item.get("id") or {}).get("videoId")
            sn = item.get("snippet") or {}
            if not vid or sn.get("liveBroadcastContent") in ("live", "upcoming"):
                continue
            snippets[str(vid)] = sn
        if not snippets:
            return []
        details = self._video_details(list(snippets))
        out: list[CandidateIn] = []
        for vid, sn in snippets.items():
            created = parse_dt(sn.get("publishedAt"))
            if created is None:
                continue
            ch = str(sn.get("channelId") or "")
            thumbs = sn.get("thumbnails") or {}
            thumb = next((thumbs[k].get("url") for k in ("high", "medium", "default")
                          if isinstance(thumbs.get(k), dict) and thumbs[k].get("url")), None)
            cand = CandidateIn(
                source=SourceKind.youtube_video,
                source_id=vid,
                url=f"https://www.youtube.com/watch?v={vid}",
                title=html.unescape(str(sn.get("title") or "")).strip() or "YouTube-Video",
                creator_name=html.unescape(str(sn.get("channelTitle") or "")).strip() or "YouTube",
                creator_url=f"https://www.youtube.com/channel/{ch}" if ch else None,
                platform_label="YouTube",
                created_at=created,
                thumbnail_url=thumb or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
                extra={"channel_id": ch, "via": "search", "query": query},
            )
            d = details.get(vid)
            if d is None or d.get("duration_s") is None:
                continue  # Suche: ohne bekannte Dauer kein Shorts-Filter möglich
            out.append(_apply_details(cand, d))
        return out

    def _duration_ok(self, duration_s: float | None) -> bool:
        if duration_s is None:
            return True
        return self.cfg.min_video_minutes * 60 <= duration_s <= self.cfg.max_video_minutes * 60

    # ------------------------------------------------------------------ Such-Zustand (Quota-Schutz)

    def _search_due(self, now: datetime) -> bool:
        state = self._load_state()
        paused = parse_dt(state.get("paused_until"))
        if paused and now < paused:
            log.info("youtube: search paused until %s (quota)", paused.isoformat())
            return False
        last = parse_dt(state.get("last_search"))
        return not (last and now - last < SEARCH_MIN_INTERVAL)

    def _load_state(self) -> dict:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_state(self, state: dict) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(json.dumps(state), encoding="utf-8")
        except OSError as e:
            log.warning("youtube: could not persist search state: %s", e)


class _QuotaError(SourceError):
    pass


def _apply_details(c: CandidateIn, d: dict) -> CandidateIn:
    eng = dict(c.engagement)
    for k in ("likes", "comments"):
        if d.get(k) is not None:
            eng[k] = d[k]
    return c.model_copy(update={
        "duration_s": d.get("duration_s") if d.get("duration_s") is not None else c.duration_s,
        "view_count": d.get("views") if d.get("views") is not None else c.view_count,
        "engagement": eng,
    })


def _next_pacific_midnight(now: datetime) -> datetime:
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("America/Los_Angeles")
        local = as_utc(now).astimezone(tz)
        nxt = datetime.combine(local.date() + timedelta(days=1), time(0, 5), tzinfo=tz)
        return nxt.astimezone(UTC)
    except Exception:  # tzdata fehlt (z. B. Windows ohne tzdata-Paket)
        return as_utc(now) + timedelta(hours=12)


_DUR_RE = re.compile(r"^P(?:(\d+(?:\.\d+)?)W)?(?:(\d+(?:\.\d+)?)D)?"
                     r"(?:T(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?)?$")


def parse_iso_duration(value: Any) -> float | None:
    """ISO-8601-Dauer ('PT1H2M3S', 'P0D', 'P1DT2H') -> Sekunden; unbrauchbar -> None."""
    if not isinstance(value, str) or not value.startswith("P"):
        return None
    m = _DUR_RE.match(value.strip())
    if not m or value.strip() in ("P", "PT"):
        return None
    w, d, h, mi, s = (float(x) if x else 0.0 for x in m.groups())
    return w * 604800 + d * 86400 + h * 3600 + mi * 60 + s


def _video_id_from_url(url: str) -> str | None:
    try:
        p = urlparse(url)
    except ValueError:
        return None
    host = (p.hostname or "").lower()
    if host.endswith("youtu.be"):
        return p.path.strip("/").split("/")[0] or None
    if "youtube.com" in host:
        if p.path == "/watch":
            return (parse_qs(p.query).get("v") or [None])[0]
        parts = [x for x in p.path.split("/") if x]
        if len(parts) >= 2 and parts[0] in ("shorts", "live", "embed", "v"):
            return parts[1]
    return None


# --------------------------------------------------------------------------- Video-Kontext (yt-dlp)


def fetch_video_context(url: str, settings: Settings) -> VideoContext:
    """yt-dlp-Metadaten (download=False): Dauer, Kapitel, heatmap, automatische Untertitel (en, json3/vtt)."""
    import yt_dlp

    # ignore_no_formats_error: wir brauchen nur Metadaten – auch wenn YouTube keine ladbaren Formate liefert
    opts = {"skip_download": True, "quiet": True, "no_warnings": True, "noplaylist": True,
            "ignore_no_formats_error": True, "logger": _YdlLogger()}
    from wdvi.media.download import cookie_file

    cookies = cookie_file(settings)  # gegen "Sign in to confirm you're not a bot"
    if cookies:
        opts["cookiefile"] = cookies
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if isinstance(info, dict) and info.get("entries"):
                info = next((x for x in info["entries"] if isinstance(x, dict)), info)
            if not isinstance(info, dict):
                raise SourceError(f"YouTube-Video nicht abrufbar: keine Metadaten für {url}.")
            transcript = _load_transcript(info, ydl)
    except SourceError:
        raise
    except Exception as e:  # yt_dlp.utils.DownloadError & Co.
        msg = str(e).removeprefix("ERROR: ").split("; please report this issue")[0].strip() or type(e).__name__
        raise SourceError(f"YouTube-Video nicht abrufbar: {msg[:300]}") from e

    duration = to_float(info.get("duration"))
    if duration is None:
        if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
            raise SourceError("YouTube-Video läuft noch live bzw. ist geplant – später erneut versuchen.")
        log.warning("youtube: %s has no duration", url)
        duration = 0.0
    chapters = []
    for ch in info.get("chapters") or []:
        if isinstance(ch, dict) and to_float(ch.get("start_time")) is not None:
            chapters.append({"start_time": to_float(ch.get("start_time")),
                             "end_time": to_float(ch.get("end_time")),
                             "title": str(ch.get("title") or "")})
    heat: list[HeatPoint] = []
    for hp in info.get("heatmap") or []:
        if not isinstance(hp, dict):
            continue
        s, e, v = to_float(hp.get("start_time")), to_float(hp.get("end_time")), to_float(hp.get("value"))
        if s is not None and e is not None and v is not None:
            heat.append(HeatPoint(start=s, end=e, value=v))
    return VideoContext(
        video_id=str(info.get("id") or _video_id_from_url(url) or ""),
        title=str(info.get("title") or ""),
        channel=str(info.get("channel") or info.get("uploader") or ""),
        duration=duration,
        description=str(info.get("description") or "")[:_MAX_DESCRIPTION],
        chapters=chapters,
        heatmap=heat,
        transcript=transcript,
        view_count=to_int(info.get("view_count")),
    )


class _YdlLogger:
    """yt-dlp-Ausgaben ins Python-Logging statt auf stderr (Fehler kommen ohnehin als Exception)."""

    def debug(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def info(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def warning(self, msg: str) -> None:
        log.debug("yt-dlp warning: %s", msg)

    def error(self, msg: str) -> None:
        log.debug("yt-dlp error: %s", msg)


def _pick_caption_track(info: dict) -> tuple[str, dict] | None:
    """(Sprache, Format-Eintrag) – automatische en-orig/en vor manuellen en-Untertiteln; json3 vor vtt."""
    auto = info.get("automatic_captions") or {}
    manual = info.get("subtitles") or {}
    choices: list[tuple[str, Any]] = [(lang, auto.get(lang)) for lang in _CAPTION_LANGS]
    choices += [(lang, manual.get(lang)) for lang in _MANUAL_LANGS]
    for lang, fmts in choices:
        if not isinstance(fmts, list):
            continue
        for ext in ("json3", "vtt"):
            for f in fmts:
                if isinstance(f, dict) and f.get("ext") == ext and f.get("url"):
                    return lang.removesuffix("-orig"), f
    return None


def _load_transcript(info: dict, ydl: Any = None) -> Transcript | None:
    track = _pick_caption_track(info)
    if track is None:
        return None
    lang, fmt = track
    raw = _download_caption(str(fmt["url"]), ydl)
    if raw is None:
        log.warning("youtube: captions for %s could not be downloaded", info.get("id"))
        return None
    try:
        if fmt.get("ext") == "json3":
            return parse_json3(json.loads(raw), language=lang)
        return parse_vtt(raw.decode("utf-8", errors="replace"), language=lang)
    except (ValueError, TypeError, KeyError) as e:
        log.warning("youtube: captions for %s unparsable: %s", info.get("id"), e)
        return None


def _download_caption(url: str, ydl: Any = None) -> bytes | None:
    """Erst httpx; falls YouTube blockt, über yt-dlp (Cookies/Impersonation) – nie Exception."""
    try:
        with httpx.Client(timeout=DEFAULT_TIMEOUT, follow_redirects=True,
                          headers={"User-Agent": _BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"}) as c:
            r = c.get(url)
        if r.status_code == 200 and r.content.strip():
            return r.content
        log.info("youtube: caption download via httpx returned HTTP %s", r.status_code)
    except httpx.HTTPError as e:
        log.info("youtube: caption download via httpx failed: %s", type(e).__name__)
    if ydl is not None and hasattr(ydl, "urlopen"):
        try:
            data = ydl.urlopen(url).read()
            return data if data and data.strip() else None
        except Exception as e:
            log.info("youtube: caption download via yt-dlp failed: %s", type(e).__name__)
    return None


def parse_json3(data: dict, language: str = "en") -> Transcript | None:
    """YouTube-json3 (events mit tStartMs/dDurationMs, segs mit utf8 + tOffsetMs) -> Transcript.

    Wort-Zeitstempel nur, wo tOffsetMs vorhanden ist (automatische Untertitel); sonst Segment-Ebene.
    """
    rows: list[tuple[float, float, list[tuple[float | None, str]]]] = []
    for ev in data.get("events") or []:
        if not isinstance(ev, dict) or not isinstance(ev.get("segs"), list):
            continue
        t0 = to_float(ev.get("tStartMs"))
        if t0 is None:
            continue
        dur = to_float(ev.get("dDurationMs")) or 0.0
        segs = [(to_float(s.get("tOffsetMs")), str(s.get("utf8") or "")) for s in ev["segs"] if isinstance(s, dict)]
        if not "".join(t for _, t in segs).strip():
            continue  # reine Zeilenumbruch-Events ("aAppend")
        rows.append((t0 / 1000.0, (t0 + dur) / 1000.0, segs))
    rows.sort(key=lambda r: r[0])

    segments: list[Segment] = []
    for i, (start, end, segs) in enumerate(rows):
        nxt = rows[i + 1][0] if i + 1 < len(rows) else None
        if nxt is not None and nxt > start:
            end = min(end, nxt) if end > start else nxt
        if end <= start:
            end = start + 1.0
        text = re.sub(r"\s+", " ", "".join(t for _, t in segs)).strip()
        words: list[Word] = []
        if len(segs) > 1 and any(off is not None for off, _ in segs[1:]):
            items = [(start + (off or 0.0) / 1000.0, t.strip()) for off, t in segs if t.strip()]
            for j, (ws, wt) in enumerate(items):
                we = items[j + 1][0] if j + 1 < len(items) else end
                ws = min(max(ws, start), end)
                we = max(min(we, end, ws + _MAX_WORD_S), ws)
                words.append(Word(text=wt, start=round(ws, 3), end=round(we, 3)))
        segments.append(Segment(text=text, start=round(start, 3), end=round(end, 3), words=words))
    if not segments:
        return None
    return Transcript(language=language, segments=segments)


_VTT_TIME = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{3})\s*-->\s*(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{3})")


def parse_vtt(text: str, language: str = "en") -> Transcript | None:
    """Einfacher WebVTT-Parser (Segment-Ebene). YouTube-Auto-VTT wiederholt Zeilen -> werden entfernt."""

    def secs(h: str | None, m: str, s: str, ms: str) -> float:
        return int(h or 0) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0

    segments: list[Segment] = []
    last_line = ""
    for block in re.split(r"\r?\n\r?\n", text):
        lines = block.strip().splitlines()
        idx = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if idx is None:
            continue
        m = _VTT_TIME.search(lines[idx])
        if not m:
            continue
        g = m.groups()
        start, end = secs(*g[:4]), secs(*g[4:])
        new_lines = []
        for ln in lines[idx + 1:]:
            clean = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", ln))).strip()
            if clean and clean != last_line:
                new_lines.append(clean)
                last_line = clean
        if new_lines and end > start:
            segments.append(Segment(text=" ".join(new_lines), start=round(start, 3), end=round(end, 3)))
    if not segments:
        return None
    return Transcript(language=language, segments=segments)


# --------------------------------------------------------------------------- Momente


def make_moment_candidate(parent: CandidateIn, start: float, end: float, score: int, why: str) -> CandidateIn:
    """youtube_moment-Kandidat aus einem Elternvideo (source_id = video_id, start_s/end_s gesetzt,
    parent_key = parent.key, extra.moment_score/why)."""
    start = max(0.0, float(start))
    end = float(end)
    if parent.duration_s:
        end = min(end, float(parent.duration_s))
    if end <= start:
        raise ValueError(f"Ungültiger Ausschnitt: Ende ({end:.1f} s) muss nach dem Start ({start:.1f} s) liegen.")
    extra = {k: v for k, v in parent.extra.items() if k not in ("moment_score", "moment_why")}
    extra.update({"moment_score": int(score), "moment_why": str(why), "parent_duration_s": parent.duration_s})
    return CandidateIn(
        source=SourceKind.youtube_moment,
        source_id=parent.source_id,
        url=parent.url,
        title=parent.title,
        creator_name=parent.creator_name,
        creator_handle=parent.creator_handle,
        creator_url=parent.creator_url,
        platform_label=parent.platform_label,
        created_at=parent.created_at,
        duration_s=round(end - start, 3),
        view_count=parent.view_count,
        engagement=dict(parent.engagement),
        language=parent.language,
        thumbnail_url=parent.thumbnail_url,
        start_s=start,
        end_s=end,
        parent_key=parent.key,
        extra=extra,
    )
