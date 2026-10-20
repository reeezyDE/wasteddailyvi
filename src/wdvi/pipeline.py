"""Orchestrierung: Discover -> Verarbeiten (Download, Analyse, Claude, Render) -> Review -> Slots -> Posten -> Stats.

Alle Methoden sind synchron und idempotent; der Scheduler ruft sie periodisch auf. Schwere Arbeit
(Whisper/Render) läuft seriell in `process_cycle` (ein Worker, GPU).
"""

from __future__ import annotations

import errno
import logging
import os
import re
import shutil
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlmodel import col, or_, select

from wdvi import db
from wdvi.brain import claude as brain_claude
from wdvi.brain import editor
from wdvi.config import Settings
from wdvi.media import analyze as media_analyze
from wdvi.media import dedup as media_dedup
from wdvi.media import download as media_download
from wdvi.media import probe as media_probe
from wdvi.media.transcribe import TranscribeError
from wdvi.models import Candidate, Clip, CreatorRule, JobRun, Metric, Post
from wdvi.publish import slots, text
from wdvi.render import renderer
from wdvi.schemas import (
    CandidateIn,
    EditPlan,
    MediaAnalysis,
    PerformanceExample,
    PostRequest,
    PostResult,
    SourceKind,
)
from wdvi.sources import scoring
from wdvi.sources import youtube as yt_source
from wdvi.sources.base import SourceError

log = logging.getLogger(__name__)

PLATFORMS = ("youtube", "tiktok", "instagram")
PLATFORM_NAMES = {"youtube": "YouTube", "tiktok": "TikTok", "instagram": "Instagram"}
POST_STATUS_NAMES = {
    "scheduled": "geplant",
    "uploading": "lädt hoch",
    "posted": "gepostet",
    "draft_sent": "als Entwurf gesendet",
    "failed": "fehlgeschlagen",
    "canceled": "abgebrochen",
}
SPEECH_MIN_RATIO = 0.15  # darunter wird die erkannte Sprache ignoriert (kaum Sprache = unzuverlässig)
LANGUAGE_MIN_PROB = 0.5  # unsichere Spracherkennung -> kein Ablehnungsgrund
RULE_BONUS = {"allow": 0.5, "boost": 1.5}  # Prescore-Bonus aus CreatorRule
# Posten: echte Fehler (Plattform lehnt dieses Video ab, Unbekanntes) zählen als Versuch
MAX_POST_ATTEMPTS = 4
RETRY_STEP = timedelta(minutes=15)  # erster Backoff
POST_RETRY_DELAYS = (RETRY_STEP, timedelta(hours=2), timedelta(hours=12))  # nach Versuch 1, 2, 3
# vorübergehende Fehler (Netz, 5xx, Kontingent, Rate-Limit, nicht verbunden) zählen NICHT als Versuch
TRANSIENT_POST_DELAYS = (timedelta(minutes=15), timedelta(hours=1), timedelta(hours=4), timedelta(hours=12))
# so lange nach dem Einplanen wird bei vorübergehenden Fehlern weiter versucht
POST_MAX_AGE = timedelta(days=5)
YT_EARLY_WINDOW = timedelta(
    hours=24
)  # YouTube native_schedule: frühestens 24 h vor dem Slot hochladen (Quota)
EARLY_RETRY = timedelta(minutes=30)
# nativ bei YouTube geplante Videos: so lange nach publishAt die Sichtbarkeit prüfen (check_status) – ein nicht
# geprüftes API-Projekt sperrt Uploads auf "privat", publishAt macht sie dann NICHT öffentlich
REMOTE_CHECK_AFTER = timedelta(minutes=10)
REMOTE_CHECK_UNTIL = timedelta(minutes=30)  # danach ohne Ergebnis als veröffentlicht zählen (mit Hinweis)
REMOTE_CHECK_RETRY = timedelta(minutes=5)
REMOTE_UNCHECKED = (
    "Sichtbarkeit bei {name} konnte nach dem geplanten Termin nicht geprüft werden (Netz/Kontingent) – bitte "
    "selbst nachsehen, ob das Video wirklich öffentlich ist."
)
REMOTE_MISSING = (
    "Video ist bei {name} nicht (mehr) vorhanden (gelöscht?) – zum geplanten Termin wurde nichts "
    "veröffentlicht. Neu hochladen: unter „Gepostet“ → „Erneut einplanen“."
)
LLM_PAUSE = timedelta(minutes=15)  # nach Rate-Limit/Überlast keine neuen Claude-Aufrufe
FAIL_STREAK_MAX = (
    3  # so viele Fehlschläge in Folge -> Verarbeitung pausieren (Netz weg, Key falsch, Platte voll)
)
TRANSIENT_STREAK_MAX = 2  # so viele vorübergehende Fehler in Folge -> ebenfalls pausieren (Internet weg)
FAIL_PAUSE = timedelta(minutes=10)  # wächst bei wiederholten Pausen exponentiell bis MAX_PAUSE
MAX_PAUSE = timedelta(hours=2)
CANDIDATE_RETRY_BASE = timedelta(minutes=15)  # Kandidat nach vorübergehendem Fehler: 15, 30, 60 … min
MAX_CANDIDATE_RETRIES = 5  # danach endgültig 'failed'
# Download/Whisper mit retryable=True (Netz/DNS, HTTP 429/5xx, Bot-Check, Modell-Download): Kandidat zurück
# auf 'new' (extra.media_retries) und die Verarbeitung pausiert sofort – trifft sonst jeden weiteren Kandidaten
MAX_MEDIA_ATTEMPTS = 4  # Versuche je Kandidat, danach 'failed'
MEDIA_PAUSE = timedelta(minutes=15)  # wächst bei Pausen in Folge ohne Erfolg bis MAX_PAUSE
MEDIA_ERRORS = (media_download.DownloadError, TranscribeError)
MAX_LLM_RETRIES = 3  # derselbe Kandidat scheitert so oft an Claude (Timeout o. Ä.) -> 'failed'
# Candidate.extra-Einträge der Wiederholungslogik (werden bei manuellem Neustart per add_candidate gelöscht)
RETRY_BOOKKEEPING = (
    "next_try_at",
    "media_retries",
    "retries",
    "llm_retries",
    "process_resets",
    "streak_reverts",
)
LIVE_RETRY = timedelta(hours=1)  # YouTube-Livestream/Premiere: später erneut
LENGTH_TOLERANCE_S = 2.0  # gemessene Dauer darf die Grenzen so weit überschreiten (Container-Rundung)
STALE_PROCESSING = timedelta(hours=3)  # "processing"/"queued" älter -> Absturz, zurück auf "new"
MAX_PROCESS_RESETS = 2
WORK_MAX_AGE = timedelta(days=2)
REJECTED_VIDEO_MAX_AGE = timedelta(days=7)
JOBRUN_MAX_AGE = timedelta(days=14)
STATS_GRACE = timedelta(hours=24)  # verpasste Metrik-Zeitpunkte nur so lange nachholen
POST_DONE = ("posted", "draft_sent")
OPEN_CLIP_STATUSES = ("pending_review", "approved", "scheduled")
CLIP_STATUS_NAMES = {"pending_review": "im Review", "approved": "freigegeben", "scheduled": "eingeplant"}
SLOT_TAKEN = ("scheduled", "uploading", "posted", "draft_sent")
BLOCKED_NOTE = "Creator blockiert (Opt-out)"
MUSIC_UNCHECKED = (
    "Musik nicht geprüft (Musik-Erkennung nicht verfügbar) – bitte selbst auf Hintergrundmusik achten."
)
INTERRUPTED_UPLOAD = (
    "Upload wurde unterbrochen (Neustart/Absturz) und wird nicht automatisch wiederholt, damit nichts "
    "doppelt erscheint. Bitte auf der Plattform prüfen; fehlt das Video dort: 'wdvi requeue <Clip-ID>'."
)

# Fehlertexte, die auf vorübergehende Probleme hindeuten (Netz, Server, Limits, volle Platte, Modell-Download)
_TRANSIENT_HINTS = (
    "netzwerk",
    "network",
    "internetverbindung",
    "timed out",
    "timeout",
    "time out",
    "temporary failure",
    "name resolution",
    "getaddrinfo",
    "could not resolve",
    "failed to resolve",
    "unable to resolve",
    "connection reset",
    "connection refused",
    "connection aborted",
    "connection error",
    "connectionerror",
    "remote end closed",
    "network is unreachable",
    "no route to host",
    "too many requests",
    "zu viele anfragen",
    "http error 429",
    "(http 429)",
    "service unavailable",
    "bad gateway",
    "gateway time",
    "sign in to confirm",
    "not a bot",
    "no space left",
    "errno 28",
    "konnte nicht geladen werden",  # Whisper-/Musik-Modell (huggingface.co) beim ersten Start
)
_TRANSIENT_RE = re.compile(r"http(?: error)? 5\d\d\b", re.I)
_LIVE_HINTS = (
    "läuft noch live",
    "ist geplant",
    "live event will begin",
    "premieres in",
    "premiere will begin",
    "is_upcoming",
    "is_live",
    "this live stream",
)
# Claude: echte Kapazitätssignale pausieren Claude für alle Kandidaten
_LLM_CAPACITY_HINTS = (
    "rate-limit",
    "rate limit",
    "429",
    "529",
    "overload",
    "überlast",
    "stundenlimit",
    "usage limit",
    "limit reached",
    "serverfehler",
)
# Posten: Klassen vorübergehender Fehler
_POST_AUTH_HINTS = ("nicht verbunden", "neu verbinden", "anmeldung ungültig", "token ungültig")
_POST_QUOTA_HINTS = (
    "quota",
    "kontingent",
    "tageslimit",
    "upload-limit",
    "rate-limit",
    "rate limit",
    "ratelimit",
    "zu viele",
    "too many",
    "429",
    "spam_risk_too_many",
    "limit erreicht",
    "limit reached",
)
_POST_NET_HINTS = (
    "netzwerk",
    "network",
    "timeout",
    "timed out",
    "temporary failure",
    "name resolution",
    "connection",
    "später erneut",
    "erneut versucht",
    "serverfehler",
    "server error",
    "internal error",
    "interner",
    "service unavailable",
    "bad gateway",
)

_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]+")
_HANDLE_URL_RES = (
    re.compile(r"(?:^|//)(?:www\.|m\.)?twitch\.tv/([A-Za-z0-9_]{2,25})(?:[/?#]|$)", re.I),
    re.compile(r"(?:^|//)(?:www\.)?kick\.com/([A-Za-z0-9_-]{2,40})(?:[/?#]|$)", re.I),
    re.compile(r"(?:^|//)(?:www\.|m\.)?youtube\.com/@([A-Za-z0-9_.-]{2,60})", re.I),
    re.compile(r"(?:^|//)(?:www\.|m\.)?youtube\.com/channel/(UC[A-Za-z0-9_-]{10,})", re.I),
    re.compile(r"(?:^|//)(?:www\.|old\.)?reddit\.com/u(?:ser)?/([A-Za-z0-9_-]{2,40})", re.I),
)
_NOT_HANDLES = {"videos", "directory", "clip", "clips", "watch", "shorts", "embed", "p", "search"}


# --------------------------------------------------------------------------- Helfer


def utcnow() -> datetime:
    return datetime.now(UTC)


def _utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _short(msg: object, limit: int = 300) -> str:
    s = " ".join(str(msg).split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def safe_name(key: str) -> str:
    """Candidate-Key -> Datei-/Ordnername."""
    return _SAFE_RE.sub("_", key).strip("_")[:120] or "clip"


def _norm_handle(value: object) -> str:
    s = str(value or "").strip().lower()
    for prefix in ("@", "u/", "/u/", "r/"):
        s = s.removeprefix(prefix)
    return s.strip()


def _handle_from_url(url: str | None) -> str | None:
    if not url:
        return None
    for rx in _HANDLE_URL_RES:
        m = rx.search(url)
        if m and m.group(1).lower() not in _NOT_HANDLES:
            return m.group(1).lower()
    return None


def candidate_to_in(row: Candidate) -> CandidateIn:
    """DB-Zeile -> CandidateIn (für Quellen/Media/Brain/Render)."""
    return CandidateIn(
        source=SourceKind(row.source),
        source_id=row.source_id,
        url=row.url,
        title=row.title,
        creator_name=row.creator_name,
        creator_handle=row.creator_handle,
        creator_url=row.creator_url,
        platform_label=row.platform_label,
        created_at=_utc(row.created_at) or utcnow(),
        duration_s=row.duration_s,
        view_count=row.view_count,
        engagement=dict(row.engagement or {}),
        language=row.language,
        thumbnail_url=row.thumbnail_url,
        start_s=row.start_s,
        end_s=row.end_s,
        parent_key=row.parent_key,
        extra=dict(row.extra or {}),
    )


def candidate_from_in(
    c: CandidateIn,
    *,
    prescore: float = 0.0,
    status: str = "new",
    reason: str | None = None,
    now: datetime | None = None,
) -> Candidate:
    """CandidateIn -> neue DB-Zeile."""
    now = _utc(now) or utcnow()
    return Candidate(
        key=c.key,
        source=c.source.value,
        source_id=c.source_id,
        url=c.url,
        title=c.title,
        creator_name=c.creator_name,
        creator_handle=c.creator_handle,
        creator_url=c.creator_url,
        platform_label=c.platform_label,
        created_at=_utc(c.created_at) or now,
        discovered_at=now,
        duration_s=c.duration_s,
        view_count=c.view_count,
        engagement=dict(c.engagement or {}),
        language=c.language,
        thumbnail_url=c.thumbnail_url,
        start_s=c.start_s,
        end_s=c.end_s,
        parent_key=c.parent_key,
        extra=dict(c.extra or {}),
        prescore=float(prescore),
        status=status,
        status_reason=reason,
        updated_at=now,
    )


def creator_keys(c: CandidateIn) -> set[tuple[str, str]]:
    """Alle (plattform, handle)-Paare, unter denen ein Creator in CreatorRule stehen könnte. Bei Reddit-Posts
    zählt zusätzlich der Reddit-Poster ("reddit", name) – auch wenn der Post auf einen Twitch-/Kick-/
    YouTube-Clip verlinkt und dem Original-Creator gutgeschrieben ist (extra.via / extra.author)."""
    plat = (c.platform_label or "").strip().lower()
    extra = c.extra or {}
    reddit = c.source == SourceKind.reddit_post
    names = [
        c.creator_handle,
        c.creator_name,
        extra.get("broadcaster_login"),
        extra.get("channel_id"),
        None if reddit else extra.get("author"),  # Reddit-Poster gehört zu "reddit", nicht zu Twitch & Co.
        extra.get("channel"),
        _handle_from_url(c.creator_url),
    ]
    keys = {(plat, _norm_handle(n)) for n in names if n and _norm_handle(n)}
    if reddit:
        for poster in (extra.get("via"), extra.get("author"), _handle_from_url(extra.get("via_url"))):
            if poster and _norm_handle(poster):
                keys.add(("reddit", _norm_handle(poster)))
    linked = str(extra.get("linked_platform") or "").strip().lower()
    linked_handle = _handle_from_url(extra.get("linked_url"))
    if linked and linked_handle:  # Reddit-Post mit Original-Clip von Twitch/Kick/YouTube
        keys.add((linked, linked_handle))
    return keys


_TWITCH_CLIP_RES = (
    re.compile(r"//clips\.twitch\.tv/(?:embed\?clip=)?([A-Za-z0-9_-]{4,120})", re.I),
    re.compile(r"//(?:www\.|m\.)?twitch\.tv/(?:[A-Za-z0-9_]+/)?clip/([A-Za-z0-9_-]{4,120})", re.I),
)
_KICK_CLIP_RES = (
    re.compile(r"//(?:www\.)?kick\.com/[A-Za-z0-9_-]+/clips/([A-Za-z0-9_-]{4,80})", re.I),
    re.compile(r"//(?:www\.)?kick\.com/[^?#]*\?(?:[^#]*&)?clip=([A-Za-z0-9_-]{4,80})", re.I),
)
_YT_WATCH_RE = re.compile(r"//(?:www\.|m\.)?youtube\.com/watch\?(?:[^#]*&)?v=([A-Za-z0-9_-]{11})", re.I)
_YT_SHORT_RE = re.compile(r"//youtu\.be/([A-Za-z0-9_-]{11})", re.I)


def linked_clip_key(url: str | None) -> str | None:
    """Verlinkter Original-Clip (Reddit-Post) -> Candidate-Key derselben Aufnahme aus einer anderen Quelle."""
    if not url:
        return None
    for rx in _TWITCH_CLIP_RES:
        m = rx.search(url)
        if m:
            return f"{SourceKind.twitch_clip.value}:{m.group(1)}"
    for rx in _KICK_CLIP_RES:
        m = rx.search(url)
        if m:
            return f"{SourceKind.kick_clip.value}:{m.group(1)}"
    for rx in (_YT_WATCH_RE, _YT_SHORT_RE):
        m = rx.search(url)
        if m:
            return f"{SourceKind.youtube_video.value}:{m.group(1)}"
    return None


def _error_text(e: BaseException) -> str:
    """Fehlertext inkl. Ursache (yt-dlp/httpx verpacken den eigentlichen Grund oft in __cause__)."""
    parts = [str(e)]
    cause = e.__cause__ or e.__context__
    if cause is not None and cause is not e:
        parts.append(f"{type(cause).__name__}: {cause}")
    return " ".join(parts).lower()


def is_transient_error(e: BaseException) -> bool:
    """Vorübergehender Fehler (Netz/DNS, HTTP 429/5xx, Bot-Check, volle Platte, Modell-Download)?
    Download-/Whisper-Fehler ordnen sich selbst ein (`retryable`); sonst entscheidet der Fehlertext."""
    flag = getattr(e, "retryable", None)
    if flag is True:
        return True
    if flag is False and isinstance(e, MEDIA_ERRORS):
        return False
    for err in (e, e.__cause__):
        if isinstance(err, OSError) and err.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)):
            return True
        if isinstance(err, TimeoutError | ConnectionError):
            return True
    low = _error_text(e)
    return any(h in low for h in _TRANSIENT_HINTS) or bool(_TRANSIENT_RE.search(low))


def _is_live_error(e: BaseException) -> bool:
    low = _error_text(e)
    return any(h in low for h in _LIVE_HINTS)


def _is_llm_capacity(e: BaseException) -> bool:
    """Rate-Limit/Überlast/Stundenlimit (betrifft alle Aufrufe) – im Gegensatz zu einem einzelnen Timeout."""
    if isinstance(e, _RetryLater):
        return True
    low = str(e).lower()
    return any(h in low for h in _LLM_CAPACITY_HINTS)


def post_error_kind(result: PostResult, connected: bool = True) -> str | None:
    """Fehler eines Uploads einordnen: 'auth' (nicht/abgelaufen verbunden), 'quota' (Kontingent/Limit/Spam-
    Bremse), 'transient' (Netz/Server) oder None (Plattform lehnt dieses Video ab / unbekannt -> Versuch)."""
    if not connected:
        return "auth"
    hint = getattr(result, "retryable", None)
    if hint is False:
        return None
    low = (result.error or "").lower()
    if any(h in low for h in _POST_AUTH_HINTS):
        return "auth"
    if any(h in low for h in _POST_QUOTA_HINTS) or getattr(result, "retry_after_s", None):
        return "quota"
    if hint is True or any(h in low for h in _POST_NET_HINTS) or _TRANSIENT_RE.search(low):
        return "transient"
    return None


class _RetryLater(Exception):
    """Vorübergehender Fehler (Claude-Limit/Überlast): Kandidat bleibt 'new'."""


class _JobCtx:
    def __init__(self) -> None:
        self.ok = True
        self.message: str | None = None

    def fail(self, message: str) -> None:
        self.ok = False
        self.message = message


# --------------------------------------------------------------------------- Pipeline


class Pipeline:
    def __init__(
        self, settings: Settings, *, llm=None, sources=None, publishers=None, transcriber=None, detector=None
    ) -> None:
        """Abhängigkeiten injizierbar (Tests); sonst lazy aus settings gebaut."""
        self.settings = settings
        self._llm = llm
        self._sources: dict[str, Any] | None = None
        if sources is not None:
            self._sources = dict(sources) if isinstance(sources, dict) else {s.name: s for s in sources}
        self._publishers: dict[str, Any] | None = dict(publishers) if publishers is not None else None
        self._transcriber = transcriber
        self._detector = detector
        self._process_lock = threading.Lock()
        self._publish_lock = threading.Lock()
        self._init_lock = threading.Lock()
        self._stop = threading.Event()
        self._llm_pause_until: datetime | None = None
        self._fail_streak = 0
        self._fail_streak_ids: list[int] = []
        self._transient_ids: set[int] = set()  # verschiedene Kandidaten mit vorübergehendem Fehler in Folge
        self._pause_level = 0  # Anzahl Pausen in Folge ohne Erfolg dazwischen (exponentieller Backoff)
        self._fail_pause_until: datetime | None = None
        self._media_pause_until: datetime | None = None  # nach vorübergehendem Download-/Whisper-Fehler
        self._last_failure: str | None = None
        # candidate_id -> "failed" | "transient" | "deferred" | "capacity" | "paused"
        self._outcome: dict[int, str] = {}
        self._early_retry_after: dict[int, datetime] = {}
        self._remote_check_after: dict[int, datetime] = {}  # post_id -> nächste Sichtbarkeitsprüfung
        self._remote_check_since: dict[int, datetime] = {}  # post_id -> erste Prüfung ohne Ergebnis
        self._post_transient: dict[int, int] = {}  # post_id -> vorübergehende Fehler in Folge
        self._uploads_recovered = False

    # ------------------------------------------------------------------ lazy Abhängigkeiten

    @property
    def llm(self):
        if self._llm is None:
            self._llm = brain_claude.build_llm(self.settings)
        return self._llm

    @property
    def sources(self) -> dict[str, Any]:
        if self._sources is None:
            from wdvi.sources.base import build_sources

            self._sources = {s.name: s for s in build_sources(self.settings)}
        return self._sources

    @property
    def publishers(self) -> dict[str, Any]:
        if self._publishers is None:
            from wdvi.publish.base import build_publishers

            self._publishers = dict(build_publishers(self.settings))
        return self._publishers

    @property
    def transcriber(self):
        with self._init_lock:
            if self._transcriber is None:
                from wdvi.media.transcribe import Transcriber

                self._transcriber = Transcriber(self.settings)
            return self._transcriber

    @property
    def detector(self):
        with self._init_lock:
            if self._detector is None:
                from wdvi.media.faces import FaceDetector

                self._detector = FaceDetector(self.settings)
            return self._detector

    def stop(self) -> None:
        """Beim Herunterfahren: laufende Zyklen hören nach dem aktuellen Kandidaten/Post auf."""
        self._stop.set()

    # ------------------------------------------------------------------ JobRun

    @contextmanager
    def _job(self, name: str, *, reraise: bool = True) -> Iterator[_JobCtx]:
        run_id: int | None = None
        try:
            with db.session_scope() as s:
                run = JobRun(job=name, started_at=utcnow())
                s.add(run)
                s.flush()
                run_id = run.id
        except Exception as e:  # DB-Problem darf den Job nicht verhindern
            log.warning("could not record job start %s: %s", name, e)
        ctx = _JobCtx()
        try:
            yield ctx
        except Exception as e:
            expected = isinstance(e, SourceError | ValueError)
            ctx.fail(_short(str(e) if expected else f"{type(e).__name__}: {e}", 1000))
            if expected:
                log.warning("job %s failed: %s", name, e)
            else:
                log.exception("job %s failed", name)
            self._finish_job(run_id, ctx)
            if reraise:
                raise
            return
        self._finish_job(run_id, ctx)

    def _finish_job(self, run_id: int | None, ctx: _JobCtx) -> None:
        if run_id is None:
            return
        try:
            with db.session_scope() as s:
                run = s.get(JobRun, run_id)
                if run is not None:
                    run.finished_at = utcnow()
                    run.ok = ctx.ok
                    run.message = _short(ctx.message, 1000) if ctx.message else None
                    s.add(run)
        except Exception as e:
            log.warning("could not record job end: %s", e)

    # ------------------------------------------------------------------ Creator-Regeln

    def _load_rules(self) -> dict[tuple[str, str], str]:
        with db.session_scope() as s:
            rows = s.exec(select(CreatorRule)).all()
        rules: dict[tuple[str, str], str] = {}
        prio = {"block": 3, "boost": 2, "allow": 1}
        for r in rows:
            k = ((r.platform_label or "").strip().lower(), _norm_handle(r.handle))
            rule = (r.rule or "").strip().lower()
            if rule in prio and prio[rule] > prio.get(rules.get(k, ""), 0):
                rules[k] = rule
        return rules

    @staticmethod
    def _rule_for(c: CandidateIn, rules: dict[tuple[str, str], str]) -> str | None:
        found = {rules[k] for k in creator_keys(c) if k in rules}
        for rule in ("block", "boost", "allow"):
            if rule in found:
                return rule
        return None

    def _prescore(self, c: CandidateIn, now: datetime, rule: str | None) -> float:
        try:
            base = float(scoring.prescore(c, now))
        except Exception as e:
            log.warning("prescore failed for %s: %s", c.key, e)
            base = 0.0
        return round(base + RULE_BONUS.get(rule or "", 0.0), 4)

    # ------------------------------------------------------------------ discover

    def discover(self, source_name: str | None = None, now: datetime | None = None) -> int:
        """Quelle(n) abfragen, neue Kandidaten upserten (key unique; view_count/engagement aktualisieren),
        prescore setzen, CreatorRule block -> status skipped. Gibt Anzahl neuer Kandidaten zurück.
        Schreibt JobRun "discover:<name>"."""
        now = _utc(now) or utcnow()
        srcs = self.sources
        if source_name is not None:
            if source_name not in srcs:
                active = ", ".join(sorted(srcs)) or "keine"
                raise ValueError(
                    f"Quelle '{source_name}' ist unbekannt oder in config.yaml deaktiviert (aktiv: {active})."
                )
            names = [source_name]
        else:
            names = list(srcs)
        total = 0
        for name in names:
            src = srcs[name]
            with self._job(f"discover:{name}", reraise=False) as job:
                if not src.is_configured():
                    job.fail(
                        f"Quelle '{name}' ist nicht konfiguriert (Zugangsdaten in .env bzw. config.yaml "
                        "fehlen) – siehe 'wdvi doctor'."
                    )
                    log.warning("source %s not configured – skipped", name)
                    continue
                found = list(src.discover(now))
                new, updated, skipped = self.upsert_candidates(found, now)
                job.message = (
                    f"{len(found)} gefunden, {new} neu, {updated} aktualisiert, {skipped} übersprungen"
                )
                log.info("discover %s: %s", name, job.message)
                total += new
        return total

    def upsert_candidates(
        self, found: list[CandidateIn], now: datetime | None = None
    ) -> tuple[int, int, int]:
        """Upsert per key. Gibt (neu, aktualisiert, übersprungen) zurück."""
        now = _utc(now) or utcnow()
        rules = self._load_rules()
        new = updated = skipped = 0
        seen: set[str] = set()
        with db.session_scope() as s:
            twitch_recent: list[tuple[Candidate, CandidateIn]] | None = None
            linked_recent: dict[str, Candidate] | None = None
            for c in found:
                key = c.key
                if key in seen:
                    continue
                seen.add(key)
                rule = self._rule_for(c, rules)
                row = s.exec(select(Candidate).where(Candidate.key == key)).first()
                if row is not None:
                    if row.status == "new":
                        if c.view_count is not None:
                            row.view_count = c.view_count
                        if c.engagement:
                            row.engagement = dict(c.engagement)
                        if row.duration_s is None and c.duration_s is not None:
                            row.duration_s = c.duration_s
                        row.prescore = self._prescore(candidate_to_in(row), now, rule)
                        if rule == "block":
                            row.status, row.status_reason = (
                                "skipped",
                                "Creator steht auf der Block-Liste (Opt-out)",
                            )
                        row.updated_at = now
                        s.add(row)
                        updated += 1
                    continue

                status, reason = "new", None
                if rule == "block":
                    status, reason = "skipped", "Creator steht auf der Block-Liste (Opt-out)"
                elif c.source == SourceKind.twitch_clip:
                    if twitch_recent is None:
                        twitch_recent = self._recent_twitch(s, now)
                    dup = self._same_moment(c, twitch_recent, now)
                    if dup is not None:
                        status, reason = (
                            "skipped",
                            f"Gleicher Moment wie {dup} (anderer Clip mit mehr Aufrufen)",
                        )
                linked_key = (
                    linked_clip_key((c.extra or {}).get("linked_url"))
                    if c.source == SourceKind.reddit_post
                    else None
                )
                if status == "new":
                    if linked_recent is None:
                        linked_recent = self._recent_linked(s, now)
                    why = self._cross_source_dup(s, c, linked_key, linked_recent, now)
                    if why:
                        status, reason = "skipped", why
                row = candidate_from_in(
                    c, prescore=self._prescore(c, now, rule), status=status, reason=reason, now=now
                )
                if linked_key:
                    row.extra = {**(row.extra or {}), "linked_key": linked_key}
                s.add(row)
                s.flush()
                if linked_key and linked_recent is not None and status == "new":
                    linked_recent.setdefault(linked_key, row)
                if c.source == SourceKind.twitch_clip and twitch_recent is not None and status == "new":
                    twitch_recent.append((row, c))
                if status == "new":
                    new += 1
                else:
                    skipped += 1
        return new, updated, skipped

    @staticmethod
    def _recent_linked(s, now: datetime) -> dict[str, Candidate]:
        """Reddit-Posts der letzten Tage, die auf einen Clip einer anderen Quelle verlinken: linked_key -> Zeile."""
        since = now - timedelta(days=7)
        rows = s.exec(
            select(Candidate).where(
                Candidate.source == SourceKind.reddit_post.value, Candidate.discovered_at >= since
            )
        ).all()
        out: dict[str, Candidate] = {}
        for r in rows:
            extra = r.extra or {}
            lk = extra.get("linked_key") or linked_clip_key(extra.get("linked_url"))
            if lk and r.status != "skipped":
                out.setdefault(str(lk), r)
        return out

    @staticmethod
    def _cross_source_dup(
        s, c: CandidateIn, linked_key: str | None, linked: dict[str, Candidate], now: datetime
    ) -> str | None:
        """Derselbe Clip aus zwei Quellen (Twitch-Clip + Reddit-Post mit Link darauf) nur einmal verarbeiten.
        Gibt einen Überspring-Grund zurück oder None."""
        if c.source == SourceKind.reddit_post:
            if not linked_key:
                return None
            orig = s.exec(select(Candidate).where(Candidate.key == linked_key)).first()
            if orig is not None:
                return f"Gleicher Clip wie {linked_key} (schon direkt von {orig.platform_label} gefunden)"
            other = linked.get(linked_key)
            if other is not None and other.key != c.key:
                return f"Gleicher Clip wie {other.key} (anderer Reddit-Post)"
            return None
        other = linked.get(c.key)
        if other is None:
            return None
        if other.status == "new":  # noch unbearbeitet -> Original-Quelle bevorzugen (echte Aufrufe/Creator)
            other.status = "skipped"
            other.status_reason = f"Gleicher Clip wie {c.key} (Original-Quelle bevorzugt)"
            other.updated_at = now
            s.add(other)
            linked.pop(c.key, None)
            return None
        return f"Gleicher Clip wie {other.key} (schon über Reddit verarbeitet)"

    @staticmethod
    def _recent_twitch(s, now: datetime) -> list[tuple[Candidate, CandidateIn]]:
        since = now - timedelta(days=3)
        rows = s.exec(
            select(Candidate).where(
                Candidate.source == SourceKind.twitch_clip.value,
                Candidate.discovered_at >= since,
                col(Candidate.status).in_(["new", "queued", "processing", "rendered"]),
            )
        ).all()
        out = []
        for r in rows:
            try:
                out.append((r, candidate_to_in(r)))
            except Exception:
                continue
        return out

    @staticmethod
    def _same_moment(
        c: CandidateIn, recent: list[tuple[Candidate, CandidateIn]], now: datetime
    ) -> str | None:
        """Key des besseren Clips desselben Moments oder None. Ist der neue Clip besser als ein noch
        unbearbeiteter, wird der alte übersprungen."""
        for row, other in list(recent):
            try:
                same = scoring.same_twitch_moment(c, other)
            except Exception:
                same = False
            if not same:
                continue
            if row.status == "new" and (c.view_count or 0) > (other.view_count or 0):
                row.status = "skipped"
                row.status_reason = f"Gleicher Moment wie {c.key} (anderer Clip mit mehr Aufrufen)"
                row.updated_at = now
                recent.remove((row, other))
                continue
            return row.key
        return None

    def open_clips(self, key: str) -> list[tuple[int, str]]:
        """Clips eines Kandidaten, die noch in Arbeit sind (Review, freigegeben, eingeplant): [(id, status)]."""
        with db.session_scope() as s:
            row = s.exec(select(Candidate).where(Candidate.key == key)).first()
            if row is None:
                return []
            clips = s.exec(
                select(Clip)
                .where(Clip.candidate_id == row.id, col(Clip.status).in_(OPEN_CLIP_STATUSES))
                .order_by(col(Clip.id))
            ).all()
            return [(int(c.id), c.status) for c in clips]

    def add_candidate(self, c: CandidateIn, now: datetime | None = None, *, force: bool = False) -> int:
        """Manueller Kandidat (CLI process-url): anlegen oder vorhandenen wieder auf 'new' setzen.
        Hat der Kandidat schon einen Clip in Review/freigegeben/eingeplant, wird abgelehnt (ValueError) –
        außer mit force=True (dann entsteht ein zweiter Clip; der vorhandene bleibt unangetastet)."""
        now = _utc(now) or utcnow()
        existing = self.open_clips(c.key)
        if existing:
            names = ", ".join(f"#{cid} ({CLIP_STATUS_NAMES.get(st, st)})" for cid, st in existing)
            if not force:
                raise ValueError(
                    f"Für {c.key} gibt es schon Clip {names} – nicht erneut verarbeitet. "
                    "Mit --force trotzdem einen neuen Clip rendern."
                )
            log.warning("add_candidate %s: already has clip %s – processing again (force)", c.key, names)
        rule = self._rule_for(c, self._load_rules())
        with db.session_scope() as s:
            row = s.exec(select(Candidate).where(Candidate.key == c.key)).first()
            if row is None:
                row = candidate_from_in(c, prescore=self._prescore(c, now, rule), now=now)
            else:
                row.status, row.status_reason, row.updated_at = "new", "manuell hinzugefügt", now
                if c.duration_s and not row.duration_s:
                    row.duration_s = c.duration_s
                # neuer Anlauf: alte Versuchszähler/Backoffs zurücksetzen (sonst scheitert er sofort endgültig)
                extra = {k: v for k, v in (row.extra or {}).items() if k not in RETRY_BOOKKEEPING}
                if extra != (row.extra or {}):
                    row.extra = extra
            s.add(row)
            s.flush()
            return int(row.id)

    def candidate_state(self, candidate_id: int) -> tuple[str, str | None]:
        with db.session_scope() as s:
            row = s.get(Candidate, candidate_id)
            if row is None:
                raise ValueError(f"Kandidat {candidate_id} nicht gefunden.")
            return row.status, row.status_reason

    def moment_ids(self, parent_key: str) -> list[int]:
        """'new'-Momente eines YouTube-Videos, beste zuerst."""
        with db.session_scope() as s:
            rows = s.exec(
                select(Candidate)
                .where(Candidate.parent_key == parent_key, Candidate.status == "new")
                .order_by(col(Candidate.prescore).desc())
            ).all()
            return [int(r.id) for r in rows]

    # ------------------------------------------------------------------ process

    def _llm_ready(self) -> tuple[bool, str]:
        if self._llm is not None:
            return True, ""
        cfg = self.settings.config.llm
        if cfg.backend == "api":
            if self.settings.secrets.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY"):
                return True, ""
            return False, (
                "Kein ANTHROPIC_API_KEY gesetzt – in .env eintragen oder llm.backend: cli verwenden. "
                "Kandidaten bleiben in der Warteschlange."
            )
        if shutil.which(cfg.cli_path):
            return True, ""
        return False, (
            f"Claude-CLI '{cfg.cli_path}' nicht gefunden – Docker-Image mit WITH_CLAUDE_CLI=1 bauen "
            "oder llm.backend: api verwenden. Kandidaten bleiben in der Warteschlange."
        )

    def _llm_remaining(self) -> int | None:
        """Verbleibende Claude-Aufrufe in dieser Stunde (None = unbekannt/unbegrenzt)."""
        try:
            limiter = getattr(self.llm, "rate_limiter", None)
            remaining = getattr(limiter, "remaining", None)
            return int(remaining()) if callable(remaining) else None
        except Exception:
            return None

    def _llm_paused(self, now: datetime) -> bool:
        return self._llm_pause_until is not None and now < self._llm_pause_until

    def _media_paused(self, now: datetime) -> bool:
        return self._media_pause_until is not None and now < self._media_pause_until

    def reset_interrupted(self) -> int:
        """Beim Start des Dauerbetriebs (`wdvi run`, einziger Worker): was noch 'queued'/'processing' ist,
        stammt aus dem vorigen Prozess -> sofort zurück auf 'new' statt erst nach STALE_PROCESSING."""
        return self._reset_stale(all_rows=True)

    def _reset_stale(self, all_rows: bool = False) -> int:
        """Kandidaten, die bei einem Absturz/Neustart in 'queued'/'processing' hängen geblieben sind
        (Wanduhr-Zeit, weil updated_at immer die echte Zeit ist). 'queued' wurde nie angefasst und zählt
        nicht als Abbruch."""
        now = utcnow()
        cutoff = now - STALE_PROCESSING
        query = select(Candidate).where(col(Candidate.status).in_(["queued", "processing"]))
        if not all_rows:
            query = query.where(Candidate.updated_at < cutoff)
        with db.session_scope() as s:
            rows = s.exec(query).all()
            for r in rows:
                if r.status == "queued":
                    r.status, r.status_reason, r.updated_at = "new", None, now
                    s.add(r)
                    continue
                resets = int((r.extra or {}).get("process_resets", 0)) + 1
                r.extra = {**(r.extra or {}), "process_resets": resets}
                if resets > MAX_PROCESS_RESETS:
                    r.status, r.status_reason = (
                        "failed",
                        "Verarbeitung mehrfach abgebrochen (Absturz/Neustart)",
                    )
                else:
                    r.status, r.status_reason = "new", "Verarbeitung unterbrochen – neuer Versuch"
                r.updated_at = now
                s.add(r)
                log.warning("candidate %s was stuck – reset to %s", r.key, r.status)
            return len(rows)

    @staticmethod
    def _not_before(row: Candidate) -> datetime | None:
        """Frühester nächster Versuch nach einem vorübergehenden Fehler (extra.next_try_at)."""
        raw = (row.extra or {}).get("next_try_at")
        if not raw:
            return None
        try:
            return _utc(datetime.fromisoformat(str(raw)))
        except ValueError:
            return None

    def _cheap_reject(self, row: Candidate, now: datetime, rules: dict[tuple[str, str], str]) -> str | None:
        """Billige Filter vor dem Download. Grund oder None."""
        cfg = self.settings.config
        f = cfg.filters
        created = _utc(row.created_at)
        if created is not None and created < now - timedelta(hours=f.max_candidate_age_hours):
            return f"Zu alt (älter als {f.max_candidate_age_hours} h)"
        try:
            cin = candidate_to_in(row)
        except Exception as e:
            return f"Ungültige Kandidatendaten: {_short(e, 120)}"
        if self._rule_for(cin, rules) == "block":
            return "Creator steht auf der Block-Liste (Opt-out)"
        dur = row.duration_s
        if dur is None and row.start_s is not None and row.end_s is not None:
            dur = row.end_s - row.start_s
        if row.source == SourceKind.youtube_video.value:
            yt = cfg.sources.youtube
            if dur and dur < yt.min_video_minutes * 60:
                return f"Video zu kurz ({dur / 60:.1f} min, vermutlich ein Short)"
            if dur and dur > yt.max_video_minutes * 60:
                return f"Video zu lang ({dur / 60:.0f} min)"
        elif dur is not None:
            if dur < f.min_source_duration_s:
                return f"Zu kurz ({dur:.0f} s < {f.min_source_duration_s:.0f} s)"
            if dur > f.max_source_duration_s:
                return f"Zu lang ({dur:.0f} s > {f.max_source_duration_s:.0f} s)"
        if row.source == SourceKind.twitch_clip.value:
            langs = [x.lower() for x in cfg.sources.twitch.languages if x]
            if langs and row.language and row.language.lower() not in langs:
                return f"Twitch-Sprache '{row.language}' nicht in sources.twitch.languages"
        return None

    def process_cycle(self, now: datetime | None = None) -> int:
        """Bis zu filters.candidates_per_cycle beste 'new'-Kandidaten (jünger als max_candidate_age_hours,
        günstige Filter bestanden, LLM-Rate-Limit beachtet) verarbeiten. youtube_video -> mine_youtube.
        Gibt Anzahl erzeugter Clips zurück."""
        if not self._process_lock.acquire(blocking=False):
            log.info("process cycle already running – skipped")
            return 0
        try:
            with self._job("process") as job:
                return self._process_cycle(_utc(now) or utcnow(), job)
        finally:
            self._process_lock.release()

    def _disk_problem(self) -> str | None:
        """Zu wenig freier Speicher für neue Downloads/Renders -> Meldung, sonst None."""
        min_gb = float(self.settings.config.cleanup.min_free_gb or 0)
        if min_gb <= 0:
            return None
        try:
            free = shutil.disk_usage(self.settings.config.paths.data_dir).free / 1e9
        except OSError:
            return None
        if free >= min_gb:
            return None
        return (
            f"Nur noch {free:.1f} GB frei (< cleanup.min_free_gb = {min_gb:g} GB) – keine neuen Clips. "
            "Alte Videos in data/clips löschen oder cleanup.keep_posted_days verringern."
        )

    def _pause(self, why: str) -> timedelta:
        """Verarbeitung pausieren; jede weitere Pause ohne Erfolg dazwischen dauert doppelt so lang."""
        pause = min(MAX_PAUSE, FAIL_PAUSE * (2**self._pause_level))
        self._pause_level += 1
        self._fail_pause_until = utcnow() + pause
        log.error("process: pausing processing for %s (%s)", pause, why)
        return pause

    def _pause_media(self, why: str) -> timedelta:
        """Wie die Claude-Pause: nach einem vorübergehenden Download-/Whisper-Fehler keine weiteren
        Kandidaten anfassen (Internet weg, Plattform-Limit). Pausen in Folge ohne Erfolg verdoppeln sich."""
        pause = min(MAX_PAUSE, MEDIA_PAUSE * (2**self._pause_level))
        self._pause_level += 1
        self._media_pause_until = utcnow() + pause
        log.warning("process: pausing downloads/transcription for %s (%s)", pause, why)
        return pause

    def _process_cycle(self, now: datetime, job: _JobCtx) -> int:
        self._reset_stale()
        if self._llm_paused(now):
            job.message = f"Claude pausiert bis {self._llm_pause_until:%H:%M} UTC (Limit/Überlast)"
            return 0
        if self._fail_pause_until is not None and utcnow() < self._fail_pause_until:
            job.fail(
                f"Pausiert bis {self._fail_pause_until:%H:%M} UTC nach mehreren Fehlschlägen in Folge – "
                f"letzter Fehler: {self._last_failure or '?'}"
            )
            return 0
        if self._media_paused(utcnow()):
            job.fail(
                f"Pausiert bis {self._media_pause_until:%H:%M} UTC nach einem vorübergehenden Download-/"
                "Whisper-Fehler (Internet weg? Plattform-Limit?) – letzter Fehler: "
                f"{self._last_failure or '?'}"
            )
            return 0
        disk = self._disk_problem()
        if disk:
            job.fail(disk)
            log.error("process: %s", disk)
            return 0
        ready, why = self._llm_ready()
        if not ready:
            job.fail(why)
            return 0
        budget = max(0, self.settings.config.filters.candidates_per_cycle)
        remaining = self._llm_remaining()
        if remaining is not None:
            budget = min(budget, remaining)
        if budget <= 0:
            job.message = "Stundenlimit für Claude-Aufrufe erreicht (llm.max_calls_per_hour) – später weiter"
            return 0

        rules = self._load_rules()
        picked: list[tuple[int, str]] = []
        skipped = waiting = 0
        wall = utcnow()
        with db.session_scope() as s:
            rows = s.exec(
                select(Candidate)
                .where(Candidate.status == "new")
                .order_by(col(Candidate.prescore).desc(), col(Candidate.discovered_at).desc())
            ).all()
            for row in rows:
                reason = self._cheap_reject(row, now, rules)
                if reason:
                    row.status, row.status_reason, row.updated_at = "skipped", reason, now
                    s.add(row)
                    skipped += 1
                    continue
                not_before = self._not_before(row)
                if not_before is not None and not_before > wall:  # Backoff nach vorübergehendem Fehler
                    waiting += 1
                    continue
                if len(picked) < budget:
                    row.status, row.status_reason, row.updated_at = "queued", None, now
                    s.add(row)
                    picked.append((int(row.id), row.source))

        created = mined = 0
        done = 0
        for cid, source in picked:
            if (
                self._stop.is_set()
                or self._llm_paused(utcnow())
                or self._media_paused(utcnow())
                or self._fail_streak >= FAIL_STREAK_MAX
                or len(self._transient_ids) >= TRANSIENT_STREAK_MAX
            ):
                break
            done += 1
            if source == SourceKind.youtube_video.value:
                mined += self.mine_youtube(cid)
            elif self.process_candidate(cid) is not None:
                created += 1
            self._track_outcome(cid)
        leftover = [cid for cid, _ in picked[done:]]
        if leftover:  # nicht mehr drangekommen -> zurück in die Warteschlange
            with db.session_scope() as s:
                for cid in leftover:
                    row = s.get(Candidate, cid)
                    if row is not None and row.status == "queued":
                        row.status, row.updated_at = "new", utcnow()
                        s.add(row)
        job.message = (
            f"{done} Kandidaten bearbeitet, {created} Clips gerendert, {mined} Momente gefunden, "
            f"{skipped} übersprungen"
        )
        if waiting:
            job.message += f", {waiting} warten auf einen neuen Versuch"
        if self._llm_paused(utcnow()):
            job.message += " – Claude pausiert (Limit/Überlast)"
        if self._fail_streak >= FAIL_STREAK_MAX:
            reverted = self._revert_streak()
            pause = self._pause(f"{FAIL_STREAK_MAX} failures in a row: {self._last_failure}")
            self._fail_streak = 0
            job.fail(
                f"{job.message} – {FAIL_STREAK_MAX} Fehlschläge in Folge, Pause für "
                f"{int(pause.total_seconds() // 60)} min"
                + (f" ({reverted} davon kommen danach erneut dran)" if reverted else "")
                + f". Letzter Fehler: {self._last_failure or '?'}"
            )
        elif len(self._transient_ids) >= TRANSIENT_STREAK_MAX:
            pause = self._pause(f"{TRANSIENT_STREAK_MAX} transient errors in a row: {self._last_failure}")
            self._transient_ids.clear()
            job.fail(
                f"{job.message} – {TRANSIENT_STREAK_MAX} vorübergehende Fehler in Folge (Internet weg? "
                f"Platte voll?), Pause für {int(pause.total_seconds() // 60)} min. Die Kandidaten bleiben in "
                f"der Warteschlange. Letzter Fehler: {self._last_failure or '?'}"
            )
        elif self._media_paused(utcnow()):
            job.fail(
                f"{job.message} – vorübergehender Download-/Whisper-Fehler (Internet weg? Plattform-Limit?), "
                f"Verarbeitung pausiert bis {self._media_pause_until:%H:%M} UTC. Die übrigen Kandidaten bleiben "
                f"in der Warteschlange. Letzter Fehler: {self._last_failure or '?'}"
            )
        log.info("process cycle: %s", job.message)
        return created

    def _track_outcome(self, candidate_id: int) -> None:
        """Fehlschläge in Folge zählen (Schutz vor systemischen Fehlern, die sonst die ganze Warteschlange
        als 'failed' markieren würden). Vorübergehende Fehler zählen getrennt."""
        kind = self._outcome.pop(candidate_id, None)
        if kind is None:
            try:
                status, reason = self.candidate_state(candidate_id)
            except Exception:
                return
            kind = "failed" if status == "failed" else None
            if kind:
                self._last_failure = reason
        if kind == "failed":
            self._fail_streak += 1
            self._fail_streak_ids.append(candidate_id)
            self._transient_ids.clear()
        elif kind == "transient":  # derselbe Kandidat zweimal ist kein Ausfall, zwei verschiedene schon
            self._transient_ids.add(candidate_id)
        elif kind in ("deferred", "capacity", "paused"):
            pass  # weder Erfolg noch Fehler des Systems (Livestream läuft noch, Claude-Limit, Netz-Pause)
        else:  # Erfolg oder inhaltliche Entscheidung: System funktioniert
            self._fail_streak = self._pause_level = 0
            self._transient_ids.clear()
            self._fail_streak_ids.clear()

    def _revert_streak(self) -> int:
        """Kandidaten der Fehlerserie einmalig zurück auf 'new' (der Fehler war vermutlich systemisch)."""
        n = 0
        ids, self._fail_streak_ids = list(self._fail_streak_ids), []
        with db.session_scope() as s:
            for cid in ids:
                row = s.get(Candidate, cid)
                if row is None or row.status != "failed" or (row.extra or {}).get("streak_reverts"):
                    continue
                pause_end = utcnow() + min(MAX_PAUSE, FAIL_PAUSE * (2**self._pause_level))
                row.extra = {**(row.extra or {}), "streak_reverts": 1, "next_try_at": pause_end.isoformat()}
                row.status = "new"
                row.status_reason = _short(
                    f"Fehlerserie (vermutlich systemisch) – neuer Versuch nach der Pause: {row.status_reason}",
                    500,
                )
                row.updated_at = utcnow()
                s.add(row)
                n += 1
        return n

    # -- Kandidaten-Status

    def _set_candidate(
        self,
        candidate_id: int,
        status: str,
        reason: str | None = None,
        extra: dict | None = None,
        **fields: Any,
    ) -> None:
        with db.session_scope() as s:
            row = s.get(Candidate, candidate_id)
            if row is None:
                return
            row.status = status
            row.status_reason = _short(reason, 500) if reason else None
            if extra:
                row.extra = {**(row.extra or {}), **extra}
            for k, v in fields.items():
                setattr(row, k, v)
            row.updated_at = utcnow()
            s.add(row)

    def _retry_later(
        self,
        candidate_id: int,
        counter: str,
        limit: int,
        reason: str,
        *,
        base: timedelta = CANDIDATE_RETRY_BASE,
    ) -> bool:
        """Kandidat nach einem vorübergehenden Fehler zurück auf 'new' mit Backoff (extra.next_try_at).
        Nach `limit` Versuchen -> 'failed'. Gibt True zurück, wenn er in der Warteschlange bleibt."""
        with db.session_scope() as s:
            row = s.get(Candidate, candidate_id)
            if row is None:
                return False
            extra = dict(row.extra or {})
            n = int(extra.get(counter, 0) or 0) + 1
            extra[counter] = n
            now = utcnow()
            if n > limit:
                extra.pop("next_try_at", None)
                row.status = "failed"
                row.status_reason = _short(f"{reason} – aufgegeben nach {n - 1} neuen Versuchen", 500)
                keep = False
            else:
                retry_at = now + base * (2 ** (n - 1))
                extra["next_try_at"] = retry_at.isoformat()
                row.status = "new"
                row.status_reason = _short(
                    f"{reason} – neuer Versuch ab {retry_at:%d.%m. %H:%M} UTC ({n}/{limit})", 500
                )
                keep = True
            row.extra = extra
            row.updated_at = now
            s.add(row)
            return keep

    def _handle_failure(self, candidate_id: int, e: BaseException, step: str) -> None:
        """Fehler -> Kandidatenstatus. Claude-Limits pausieren Claude (Kandidat bleibt 'new'). Download-/
        Whisper-Fehler mit retryable=True (Netz/DNS, 429/5xx, Bot-Check, Modell-Download) stellen den Kandidaten
        zurück (extra.media_retries, nach MAX_MEDIA_ATTEMPTS 'failed') und pausieren die Verarbeitung wie bei
        Claude; andere vorübergehende Fehler (z. B. volle Platte beim Rendern) stellen ihn mit Backoff zurück."""
        if isinstance(e, brain_claude.LLMRefusal):
            self._set_candidate(candidate_id, "rejected", f"Claude hat abgelehnt: {_short(e, 200)}")
            log.info("candidate %s: Claude refused (%s)", candidate_id, e)
            return
        if (isinstance(e, brain_claude.LLMError) and getattr(e, "retryable", False)) or isinstance(
            e, _RetryLater
        ):
            reason = f"Claude vorübergehend nicht verfügbar: {_short(e, 200)}"
            if _is_llm_capacity(e):
                # Limit/Überlast betrifft alle Kandidaten: Claude pausieren, dieser Kandidat verliert nichts
                self._llm_pause_until = utcnow() + LLM_PAUSE
                self._set_candidate(candidate_id, "new", f"{reason} – neuer Versuch später")
                self._outcome[candidate_id] = "capacity"
                log.warning(
                    "candidate %s: LLM temporarily unavailable (%s) – pausing Claude for %s",
                    candidate_id,
                    e,
                    LLM_PAUSE,
                )
                return
            # einzelner Aufruf gescheitert (Timeout, Verbindung): nur diesen Kandidaten zurückstellen
            kept = self._retry_later(candidate_id, "llm_retries", MAX_LLM_RETRIES, reason)
            self._outcome[candidate_id] = "transient" if kept else "failed"
            self._last_failure = reason
            log.warning(
                "candidate %s: LLM call failed (%s) – %s",
                candidate_id,
                e,
                "retry later" if kept else "giving up",
            )
            return
        if isinstance(e, MEDIA_ERRORS) and getattr(e, "retryable", False) is True:
            # Netz/DNS/Limit/Modell-Download: trifft jeden weiteren Kandidaten genauso -> zurückstellen und
            # pausieren statt die Warteschlange zu verbrennen
            what = "Download" if isinstance(e, media_download.DownloadError) else "Transkription"
            reason = f"{what} vorübergehend fehlgeschlagen: {_short(e, 250)}"
            kept = self._retry_later(candidate_id, "media_retries", MAX_MEDIA_ATTEMPTS - 1, reason)
            pause = self._pause_media(reason)
            self._outcome[candidate_id] = "paused" if kept else "failed"
            self._last_failure = reason
            log.warning(
                "candidate %s: transient %s error (%s) – %s, processing paused for %s",
                candidate_id,
                what.lower(),
                e,
                "retry later" if kept else f"giving up after {MAX_MEDIA_ATTEMPTS} attempts",
                pause,
            )
            return
        if isinstance(e, brain_claude.LLMError):
            reason = f"Claude-Fehler: {_short(e, 250)}"
        elif isinstance(e, media_download.DownloadError):
            reason = f"Download: {_short(e, 250)}"
        elif isinstance(e, TranscribeError):
            reason = f"Transkription: {_short(e, 250)}"
        elif isinstance(e, renderer.RenderError):
            reason = f"Rendern: {_short(e, 250)}"
        else:
            reason = f"{step}: {type(e).__name__}: {_short(e, 220)}"
        if not isinstance(e, brain_claude.LLMError) and is_transient_error(e):
            kept = self._retry_later(candidate_id, "retries", MAX_CANDIDATE_RETRIES, reason)
            self._outcome[candidate_id] = "transient" if kept else "failed"
            self._last_failure = reason
            log.warning(
                "candidate %s: transient error in %s (%s) – %s",
                candidate_id,
                step,
                e,
                "retry later" if kept else "giving up",
            )
            return
        self._set_candidate(candidate_id, "failed", reason)
        self._outcome[candidate_id] = "failed"
        self._last_failure = reason
        log.warning(
            "candidate %s failed in %s: %s",
            candidate_id,
            step,
            e,
            exc_info=not isinstance(
                e,
                media_download.DownloadError | TranscribeError | renderer.RenderError | brain_claude.LLMError,
            ),
        )

    def _work_dir(self, c: CandidateIn) -> Path:
        work = self.settings.config.paths.work_dir / safe_name(c.key)
        work.mkdir(parents=True, exist_ok=True)
        return work

    @staticmethod
    def _drop_work(work: Path | None) -> None:
        """Arbeitsordner (Download, Frames, Analyse-Cache) löschen, sobald der Kandidat endgültig erledigt ist."""
        if work is None:
            return
        try:
            shutil.rmtree(work, ignore_errors=True)
        except OSError as e:  # pragma: no cover - ignore_errors fängt fast alles
            log.debug("could not remove %s: %s", work, e)

    def _length_reason(self, c: CandidateIn, duration: float | None) -> str | None:
        """Gemessene Dauer nach dem Download prüfen (Reddit-Links u. Ä. haben vorher keine Dauer)."""
        if duration is None or c.source in (SourceKind.youtube_moment, SourceKind.youtube_video):
            return None
        f = self.settings.config.filters
        if duration > f.max_source_duration_s + LENGTH_TOLERANCE_S:
            return (
                f"Zu lang ({duration:.0f} s > {f.max_source_duration_s:.0f} s, filters.max_source_duration_s; "
                "erst nach dem Download messbar)"
            )
        if duration < f.min_source_duration_s - LENGTH_TOLERANCE_S:
            return f"Zu kurz ({duration:.0f} s < {f.min_source_duration_s:.0f} s)"
        return None

    @staticmethod
    def _probe_duration(src: Path) -> float | None:
        try:
            return float(media_probe.probe(src).duration)
        except Exception as e:  # unlesbar -> die Analyse meldet den eigentlichen Fehler
            log.debug("probe of %s failed: %s", src, e)
            return None

    def _analyze(self, src: Path, work: Path) -> MediaAnalysis:
        """Analyse mit Cache (analysis.json im Arbeitsordner) – spart Whisper bei Wiederholungen."""
        cache = work / "analysis.json"
        if cache.is_file():
            try:
                cached = MediaAnalysis.model_validate_json(cache.read_text(encoding="utf-8"))
                if Path(cached.info.path).resolve() == src.resolve():
                    log.info("using cached analysis %s", cache)
                    return cached
            except Exception as e:
                log.info("analysis cache unusable (%s) – analysing again", e)
        analysis = media_analyze.analyze(src, work, self.settings, self.transcriber, self.detector)
        try:
            cache.write_text(analysis.model_dump_json(), encoding="utf-8")
        except OSError as e:
            log.warning("could not write analysis cache: %s", e)
        return analysis

    def _known_phashes(self, candidate_id: int, now: datetime) -> list[list[str]]:
        since = now - timedelta(days=max(0, self.settings.config.filters.dedup_days))
        with db.session_scope() as s:
            rows = s.exec(
                select(Clip).where(Clip.created_at >= since, Clip.candidate_id != candidate_id)
            ).all()
            return [list(r.phashes) for r in rows if r.phashes]

    def _rejected_phashes(self, candidate_id: int, now: datetime) -> list[list[str]]:
        """pHashes kürzlich aussortierter Kandidaten (Claude/Sprache/Musik) – dasselbe Material aus einer
        anderen Quelle soll nicht erneut bei Claude landen."""
        since = now - timedelta(days=max(0, self.settings.config.filters.dedup_days))
        with db.session_scope() as s:
            rows = s.exec(
                select(Candidate).where(
                    Candidate.status == "rejected",
                    Candidate.updated_at >= since,
                    Candidate.id != candidate_id,
                )
            ).all()
            return [list(r.extra["phashes"]) for r in rows if (r.extra or {}).get("phashes")]

    def _media_reject(self, analysis: MediaAnalysis, candidate_id: int, now: datetime) -> str | None:
        f = self.settings.config.filters
        tr = analysis.transcript
        langs = [x.lower() for x in f.languages if x]
        if langs and tr is not None and tr.language and analysis.speech_ratio >= SPEECH_MIN_RATIO:
            lang = tr.language.lower()
            if lang not in langs and (tr.language_prob or 0.0) >= LANGUAGE_MIN_PROB:
                return f"Sprache '{lang}' (erkannt) nicht in filters.languages"
        if (
            f.music_policy == "reject"
            and analysis.music_prob is not None
            and analysis.music_prob >= f.music_threshold
        ):
            return f"Musik erkannt ({analysis.music_prob:.0%} ≥ {f.music_threshold:.0%}) – Copyright-Risiko"
        if analysis.phashes:
            known = self._known_phashes(candidate_id, now)
            if known and media_dedup.is_near_duplicate(analysis.phashes, known, f.phash_max_distance):
                return f"Duplikat eines Clips der letzten {f.dedup_days} Tage"
            seen = self._rejected_phashes(candidate_id, now)
            if seen and media_dedup.is_near_duplicate(analysis.phashes, seen, f.phash_max_distance):
                return f"Duplikat eines bereits aussortierten Kandidaten der letzten {f.dedup_days} Tage"
        return None

    def _plan_reject(self, plan: EditPlan) -> str | None:
        f = self.settings.config.filters
        if not plan.keep:
            return plan.reject_reason or plan.reasoning or "von Claude verworfen"
        if plan.is_unsafe:
            return "Unsicherer Inhalt (NSFW/Hass/reale Gewalt)"
        if plan.is_leak_or_unreleased and f.leak_policy == "reject":
            return "Leak/unveröffentlichtes Material (Takedown-Risiko)"
        if plan.virality_score < f.min_virality_score:
            return f"Viral-Score {plan.virality_score} < {f.min_virality_score}"
        return None

    def enabled_platforms(self) -> list[str]:
        if self._publishers is not None:
            return [p for p in PLATFORMS if p in self._publishers]
        pub = self.settings.config.publish
        return [p for p in PLATFORMS if getattr(pub, p).enabled]

    @staticmethod
    def _analysis_summary(a: MediaAnalysis) -> dict:
        tr = a.transcript
        return {
            "layout": a.layout.kind,
            "layout_confidence": round(float(a.layout.confidence), 3),
            "music_prob": None if a.music_prob is None else round(float(a.music_prob), 3),
            "speech_ratio": round(float(a.speech_ratio), 3),
            "language": tr.language if tr is not None else None,
            "duration": round(float(a.info.duration), 2),
            "source_width": a.info.width,
            "source_height": a.info.height,
            "transcript": _short(tr.text, 1500) if tr is not None and tr.text else "",
        }

    def process_candidate(self, candidate_id: int) -> int | None:
        """download -> Längen-Check -> analyze -> Sprach-/Musik-/Dedup-Filter -> plan_clip -> render_clip -> Clip
        (pending_review; bei require_approval=False direkt approved). Gibt Clip-ID oder None zurück.
        Jeder Ablehnungsgrund landet in Candidate.status_reason."""
        with db.session_scope() as s:
            row = s.get(Candidate, candidate_id)
            if row is None:
                raise ValueError(f"Kandidat {candidate_id} nicht gefunden.")
            source = row.source
        if source == SourceKind.youtube_video.value:
            self.mine_youtube(candidate_id)
            return None

        self._outcome.pop(candidate_id, None)
        step = "Vorbereitung"
        work: Path | None = None
        try:
            with db.session_scope() as s:
                row = s.get(Candidate, candidate_id)
                c = candidate_to_in(row)
            self._set_candidate(candidate_id, "processing", None)
            now = utcnow()
            settings = self.settings
            work = self._work_dir(c)

            step = "Download"
            try:
                src = Path(media_download.download(c, work, settings))
            except media_download.TooLongError as e:  # match_filter: gar nicht erst heruntergeladen
                fields = (
                    {"duration_s": round(e.duration_s, 3)} if e.duration_s and c.duration_s is None else {}
                )
                self._set_candidate(candidate_id, "skipped", str(e), **fields)
                self._drop_work(work)
                log.info("candidate %s skipped before download: %s", c.key, e)
                return None
            step = "Länge"
            measured = self._probe_duration(src)
            if measured is not None and c.duration_s is None:
                self._set_candidate(candidate_id, "processing", None, duration_s=round(measured, 3))
            reason = self._length_reason(c, measured)
            if reason:  # vor Whisper/Claude: lange fremde Videos nicht komplett verarbeiten
                self._set_candidate(candidate_id, "skipped", reason)
                self._drop_work(work)
                log.info("candidate %s skipped after download: %s", c.key, reason)
                return None
            step = "Analyse"
            analysis = self._analyze(src, work)
            step = "Filter"
            reason = self._length_reason(c, analysis.info.duration) if measured is None else None
            reason = reason or self._media_reject(analysis, candidate_id, now)
            if reason:
                extra = (
                    {"phashes": list(analysis.phashes)}
                    if analysis.phashes and "Duplikat" not in reason
                    else None
                )
                self._set_candidate(candidate_id, "rejected", reason, extra=extra)
                self._drop_work(work)
                log.info("candidate %s rejected: %s", c.key, reason)
                return None

            step = "Claude"
            fb = settings.config.feedback
            examples = self.performance_examples(fb.examples_in_prompt) if fb.enabled else []
            plan = editor.plan_clip(self.llm, c, analysis, examples, settings)
            llm_info = {
                "llm": {
                    "score": plan.virality_score,
                    "keep": plan.keep,
                    "reasoning": _short(plan.reasoning, 500),
                }
            }
            reason = self._plan_reject(plan)
            if reason:
                self._set_candidate(
                    candidate_id,
                    "rejected",
                    f"Claude: {_short(reason, 300)} (Score {plan.virality_score})",
                    extra={**llm_info, "phashes": list(analysis.phashes)},
                )
                self._drop_work(work)
                log.info("candidate %s rejected by plan: %s", c.key, reason)
                return None

            step = "Rendern"
            res = renderer.render_clip(
                src, analysis, plan, c, settings, work_dir=work, out_dir=settings.config.paths.clips_dir
            )

            step = "Speichern"
            clip_id = self._create_clip(candidate_id, c, analysis, plan, res)
            self._set_candidate(
                candidate_id, "rendered", f"Clip #{clip_id} (Score {plan.virality_score})", extra=llm_info
            )
            self._drop_work(work)
            log.info("candidate %s -> clip %s (%s)", c.key, clip_id, res.video_path)
            return clip_id
        except Exception as e:
            self._handle_failure(candidate_id, e, step)
            if self._outcome.get(candidate_id) == "failed":
                self._drop_work(work)  # endgültig gescheitert: Download nicht 2 Tage liegen lassen
            return None

    def _create_clip(
        self, candidate_id: int, c: CandidateIn, analysis: MediaAnalysis, plan: EditPlan, res
    ) -> int:
        platforms = self.enabled_platforms()
        titles: dict[str, str] = {}
        descriptions: dict[str, str] = {}
        for p in PLATFORMS:
            title = getattr(plan.titles, p, "") or plan.titles.youtube
            body = getattr(plan.descriptions, p, "") or ""
            titles[p] = text.compose_title(p, title)
            descriptions[p] = text.compose_description(p, body, list(plan.hashtags), c, self.settings)
        auto = not self.settings.config.publish.require_approval
        note = None
        f = self.settings.config.filters
        if f.music_policy == "reject" and analysis.music_prob is None and analysis.info.has_audio:
            # Musik-Erkennung ausgefallen: nie ungeprüft automatisch posten (Copyright-Risiko)
            auto, note = False, MUSIC_UNCHECKED
            log.warning("clip of %s: music check unavailable – needs manual review", c.key)
        clip = Clip(
            candidate_id=candidate_id,
            error=note,
            status="approved" if auto else "pending_review",
            virality_score=int(plan.virality_score),
            plan=plan.model_dump(mode="json"),
            analysis=self._analysis_summary(analysis),
            video_path=str(res.video_path),
            thumb_path=str(res.thumb_path) if res.thumb_path else None,
            duration=float(res.duration),
            phashes=list(analysis.phashes),
            titles={p: titles[p] for p in platforms} or titles,
            descriptions={p: descriptions[p] for p in platforms} or descriptions,
            platforms=platforms,
            created_at=utcnow(),
        )
        with db.session_scope() as s:
            s.add(clip)
            s.flush()
            return int(clip.id)

    def mine_youtube(self, candidate_id: int) -> int:
        """fetch_video_context + mine_moments -> youtube_moment-Kandidaten anlegen; Eltern -> 'mined'.
        Läuft das Video noch live bzw. ist es eine geplante Premiere: später erneut (Kandidat bleibt 'new')."""
        self._outcome.pop(candidate_id, None)
        step = "Video-Infos"
        try:
            with db.session_scope() as s:
                row = s.get(Candidate, candidate_id)
                if row is None:
                    raise ValueError(f"Kandidat {candidate_id} nicht gefunden.")
                c = candidate_to_in(row)
            self._set_candidate(candidate_id, "processing", None)
            now = utcnow()
            yt = self.settings.config.sources.youtube
            try:
                ctx = yt_source.fetch_video_context(c.url, self.settings)
            except SourceError as e:
                if not _is_live_error(e):
                    raise
                retry_at = utcnow() + LIVE_RETRY
                self._set_candidate(
                    candidate_id,
                    "new",
                    f"Livestream/Premiere läuft noch – neuer Versuch ab {retry_at:%d.%m. %H:%M} UTC "
                    "(das fertige Video wird dann zerlegt)",
                    extra={"next_try_at": retry_at.isoformat()},
                )
                self._outcome[candidate_id] = "deferred"
                log.info("youtube video %s is live/upcoming – retry at %s", c.key, retry_at)
                return 0
            duration = ctx.duration or c.duration_s
            if duration and duration < yt.min_video_minutes * 60:
                self._set_candidate(
                    candidate_id,
                    "skipped",
                    f"Video zu kurz ({duration / 60:.1f} min, vermutlich ein Short)",
                    duration_s=duration,
                )
                return 0
            if duration and duration > yt.max_video_minutes * 60:
                self._set_candidate(
                    candidate_id, "skipped", f"Video zu lang ({duration / 60:.0f} min)", duration_s=duration
                )
                return 0
            parent = c.model_copy(
                update={
                    "duration_s": duration or c.duration_s,
                    "view_count": ctx.view_count if ctx.view_count is not None else c.view_count,
                }
            )
            step = "Claude"
            picks = editor.mine_moments(self.llm, parent, ctx, self.settings)
            moments: list[CandidateIn] = []
            for p in picks:
                try:
                    moments.append(yt_source.make_moment_candidate(parent, p.start, p.end, p.score, p.why))
                except ValueError as e:
                    log.info("moment of %s ignored: %s", c.key, e)
            step = "Speichern"
            rule = self._rule_for(parent, self._load_rules())
            n = 0
            with db.session_scope() as s:
                for m in moments:
                    if s.exec(select(Candidate).where(Candidate.key == m.key)).first() is not None:
                        continue
                    s.add(candidate_from_in(m, prescore=self._prescore(m, now, rule), now=now))
                    n += 1
            reason = f"{n} Momente gefunden" if n else "Keine geeigneten Momente gefunden"
            self._set_candidate(
                candidate_id, "mined", reason, duration_s=parent.duration_s, view_count=parent.view_count
            )
            log.info("mined %s: %s", c.key, reason)
            return n
        except Exception as e:
            self._handle_failure(candidate_id, e, step)
            return 0

    # ------------------------------------------------------------------ review (vom Dashboard aufgerufen)

    def _edited_description(
        self, platform: str, edited: str, current: str | None, hashtags: list[str], c: CandidateIn | None
    ) -> str:
        """Im Dashboard bearbeitete Beschreibung: Credit, Fan-Hinweis und #GTA6 hängt immer der Code an.
        Unveränderte Texte bleiben exakt so; sonst wird der Text (ohne alten Anhang) neu zusammengesetzt."""
        d = (edited or "").replace("\r\n", "\n").strip()  # Browser-Formulare schicken CRLF
        if current is not None and d == current.strip():
            return current
        credit = None
        if c is not None:
            try:
                credit = text.credit_line(c)
            except Exception:
                credit = None
        # alten Anhang (Credit-Zeile, Fan-Hinweis, Hashtag-Zeile) abtrennen; Hashtags daraus übernehmen
        markers = [m for m in (credit, text.FAN_DISCLAIMER, "🎥 Clip:") if m]
        cuts = [d.find(m) for m in markers if m in d]
        body, tail_tags = d, None
        if cuts:
            cut = min(cuts)
            body, tail = d[:cut].rstrip(), d[cut:]
            tail_tags = text.hashtags_in(tail)
        if c is None:  # ohne Kandidat kein Credit möglich – Text so übernehmen
            return d
        try:
            return text.compose_description(
                platform, body, tail_tags if tail_tags else hashtags, c, self.settings
            )
        except Exception as e:  # Absicherung: wenigstens den Credit anhängen
            log.warning("compose_description failed for %s: %s", platform, e)
            return f"{d}\n\n{credit}" if credit and credit not in d else d

    def approve_clip(
        self,
        clip_id: int,
        platforms: list[str],
        titles: dict[str, str] | None = None,
        descriptions: dict[str, str] | None = None,
    ) -> None:
        chosen = [p for p in dict.fromkeys(x.strip().lower() for x in platforms or []) if p in PLATFORMS]
        if not chosen:
            raise ValueError("Bitte mindestens eine Plattform auswählen (YouTube, TikTok oder Instagram).")
        with db.session_scope() as s:
            clip = s.get(Clip, clip_id)
            if clip is None:
                raise ValueError(f"Clip {clip_id} nicht gefunden.")
            if clip.status in ("scheduled", "posted"):
                raise ValueError(
                    f"Clip {clip_id} ist bereits {clip.status} und kann nicht erneut freigegeben werden."
                )
            cand = s.get(Candidate, clip.candidate_id)
            cin: CandidateIn | None = None
            if cand is not None:
                try:
                    cin = candidate_to_in(cand)
                except Exception:
                    cin = None
            new_titles = dict(clip.titles or {})
            for p, t in (titles or {}).items():
                if p in PLATFORMS and t is not None and t.strip():
                    new_titles[p] = text.compose_title(p, t)
            new_desc = dict(clip.descriptions or {})
            hashtags = list((clip.plan or {}).get("hashtags") or [])
            for p, d in (descriptions or {}).items():
                if p in PLATFORMS and d is not None:
                    new_desc[p] = self._edited_description(p, d, new_desc.get(p), hashtags, cin)
            clip.titles = new_titles
            clip.descriptions = new_desc
            clip.platforms = chosen
            clip.status = "approved"
            clip.reviewed_at = utcnow()
            clip.error = None
            s.add(clip)
        log.info("clip %s approved for %s", clip_id, ", ".join(chosen))

    def reject_clip(self, clip_id: int, reason: str | None = None) -> None:
        """Clip ablehnen; geplante Posts abbrechen – schon bei YouTube geplante Videos werden dort per
        unschedule() zurückgezogen (sonst Hinweis, sie von Hand zu löschen)."""
        with db.session_scope() as s:
            clip = s.get(Clip, clip_id)
            if clip is None:
                raise ValueError(f"Clip {clip_id} nicht gefunden.")
            remote = [
                (int(p.id), p.platform, str(p.remote_id))
                for p in s.exec(
                    select(Post).where(
                        Post.clip_id == clip_id, Post.status == "scheduled", col(Post.remote_id).is_not(None)
                    )
                ).all()
            ]
        pulled = {pid: self._unschedule_remote(platform, rid) for pid, platform, rid in remote}
        with db.session_scope() as s:
            clip = s.get(Clip, clip_id)
            if clip is None:
                raise ValueError(f"Clip {clip_id} nicht gefunden.")
            clip.status = "rejected"
            clip.reviewed_at = utcnow()
            manual: list[str] = []
            for p in s.exec(select(Post).where(Post.clip_id == clip_id, Post.status == "scheduled")).all():
                msg = "Clip abgelehnt"
                if p.remote_id:
                    ok, info = pulled.get(int(p.id), (False, "nicht versucht"))
                    msg += f" – {info}" if ok else " – " + self._manual_note(p.platform, p.remote_id, info)
                    if not ok:
                        manual.append(f"{p.platform} (ID {p.remote_id})")
                p.status, p.error = "canceled", _short(msg, 500)
                s.add(p)
            note = _short(reason, 500) if reason else None
            if manual:
                extra = "Bereits geplante Videos selbst löschen: " + ", ".join(manual)
                note = f"{note} · {extra}" if note else extra
            clip.error = note
            s.add(clip)
        log.info("clip %s rejected (%s)", clip_id, reason or "-")

    @staticmethod
    def _manual_note(platform: str, remote_id: str | None, why: str | None = None) -> str:
        name = PLATFORM_NAMES.get(platform, platform)
        return _short(
            f"ACHTUNG: Video liegt schon geplant bei {name} (ID {remote_id}) und geht sonst online: dort löschen "
            "oder auf privat stellen!" + (f" ({why})" if why else ""),
            400,
        )

    def cancel_post(self, post_id: int, reason: str = "Im Dashboard abgebrochen") -> tuple[bool, str]:
        """Einen geplanten Post abbrechen (Dashboard). Liegt das Video schon geplant bei der Plattform
        (YouTube native_schedule, remote_id), wird es dort per unschedule() zurückgezogen; klappt das nicht,
        sagt die Meldung, dass es von Hand gelöscht werden muss. Danach den Clip-Status nachziehen.
        Gibt (ok, Meldung) zurück."""
        with db.session_scope() as s:
            post = s.get(Post, post_id)
            if post is None:
                return False, f"Post #{post_id} nicht gefunden."
            name = PLATFORM_NAMES.get(post.platform, post.platform)
            if post.status == "uploading":
                return False, f"{name}-Upload läuft gerade – kann nicht mehr abgebrochen werden."
            if post.status != "scheduled":
                label = POST_STATUS_NAMES.get(post.status, post.status)
                return False, f"Post #{post_id} ist nicht (mehr) geplant (Status: {label})."
            platform, remote_id = post.platform, post.remote_id
        pulled: tuple[bool, str] | None = None
        if remote_id:  # Netzwerk-Aufruf außerhalb der DB-Transaktion
            pulled = self._unschedule_remote(platform, remote_id)
        with db.session_scope() as s:
            post = s.get(Post, post_id)
            if post is None or post.status != "scheduled":
                label = POST_STATUS_NAMES.get(post.status, post.status) if post is not None else "gelöscht"
                return False, f"Post #{post_id} hat sich inzwischen geändert (Status: {label})."
            msg = reason
            if pulled is not None:
                ok, info = pulled
                msg += f" – {info}" if ok else " – " + self._manual_note(platform, remote_id, info)
            post.status, post.error = "canceled", _short(msg, 500)
            s.add(post)
            s.flush()
            clip = s.get(Clip, post.clip_id)
            extra = self._settle_clip_after_cancel(s, clip, platform) if clip is not None else ""
        log.info("post %s (%s) canceled: %s", post_id, platform, msg)
        if pulled is None:
            return True, f"{name}-Post abgebrochen.{extra}"
        ok, info = pulled
        if ok:
            return True, f"{name}-Post abgebrochen. {info}{extra}"
        return True, (
            f"{name}-Post abgebrochen. Achtung: Das Video ist bei {name} schon hochgeladen und dort geplant und "
            f"konnte nicht automatisch zurückgezogen werden ({info}) – bitte zusätzlich im Studio bzw. in der "
            f"App löschen oder auf privat stellen.{extra}"
        )

    @staticmethod
    def _settle_clip_after_cancel(s, clip: Clip, platform: str) -> str:
        """Clip-Status nach einem abgebrochenen Post nachziehen (sonst hängt er für immer in 'scheduled').
        Gibt einen Zusatz für die Meldung zurück."""
        if clip.status not in ("approved", "scheduled", "posted", "failed"):
            return ""  # z. B. abgelehnt: bleibt abgelehnt
        # Plattform abwählen, sonst plant schedule_approved sie bei 'approved'-Clips gleich wieder ein
        clip.platforms = [p for p in (clip.platforms or []) if p != platform]
        posts = [
            p for p in s.exec(select(Post).where(Post.clip_id == clip.id)).all() if p.status != "canceled"
        ]
        if clip.status == "approved" and clip.platforms:
            s.add(clip)
            return ""
        if not posts:
            clip.status = "pending_review"
            clip.error = "Alle Posts im Dashboard abgebrochen – neu freigeben oder ablehnen."
            s.add(clip)
            return f" Clip #{clip.id} ist wieder in der Review."
        if clip.status == "approved":  # nichts mehr einzuplanen, nur noch vorhandene Posts
            clip.status = "scheduled"
        if clip.status == "scheduled":
            pending = [
                p for p in posts if p.status == "uploading" or (p.status == "scheduled" and not p.remote_id)
            ]
            if not pending:
                done = [p for p in posts if p.status in POST_DONE or p.status == "scheduled"]
                clip.status = "posted" if done else "failed"
        s.add(clip)
        return ""

    # ------------------------------------------------------------------ publish

    @staticmethod
    def _taken_slots(s, platform: str, since: datetime, exclude_post: int | None = None) -> list[datetime]:
        rows = s.exec(
            select(Post).where(
                Post.platform == platform,
                col(Post.status).in_(SLOT_TAKEN),
                Post.scheduled_at >= since,
            )
        ).all()
        return [_utc(p.scheduled_at) for p in rows if p.id != exclude_post]

    def _free_slot(self, s, platform: str, earliest: datetime, exclude_post: int | None = None) -> datetime:
        """Nächster freier Slot ab `earliest` (bei Problemen mit der Slot-Konfiguration: `earliest`)."""
        taken = self._taken_slots(s, platform, earliest - timedelta(days=2), exclude_post)
        try:
            return _utc(slots.next_slot(platform, taken, earliest, self.settings))
        except Exception as e:
            log.warning("no free %s slot after %s (%s) – using that time", platform, earliest, e)
            return earliest

    def _unschedule_remote(self, platform: str, remote_id: str) -> tuple[bool, str]:
        """Schon bei der Plattform geplantes Video zurückziehen (Publisher-Methode `unschedule(remote_id)`,
        YouTube: privat ohne publishAt). (True, Meldung) bei Erfolg, sonst (False, Grund) – dann muss es von
        Hand gelöscht oder auf privat gestellt werden."""
        name = PLATFORM_NAMES.get(platform, platform)
        try:
            publisher = self.publishers.get(platform)
        except Exception as e:  # Publisher nicht baubar (Konfiguration kaputt)
            log.warning("publishers unavailable for unschedule: %s", e)
            publisher = None
        fn = getattr(publisher, "unschedule", None)
        if not callable(fn):
            return False, f"{name} ist nicht aktiv bzw. kann geplante Videos nicht automatisch zurückziehen"
        try:
            res = fn(remote_id)
        except Exception as e:  # PublishError mit deutscher Meldung (Rechte, Netz, schon veröffentlicht)
            log.warning("could not unschedule %s video %s: %s", platform, remote_id, e)
            return False, _short(str(e) or type(e).__name__, 300)
        log.info("%s video %s unscheduled", platform, remote_id)
        return True, _short(str(res or f"Bei {name} zurückgezogen."), 300)

    def _apply_blocks(self, now: datetime) -> int:
        """Creator auf der Block-Liste (Opt-out/Takedown): offene Clips verwerfen und geplante Posts abbrechen –
        auch wenn der Clip schon freigegeben oder eingeplant war; bei YouTube schon geplante Videos werden dort
        zurückgezogen (unschedule). Gibt die Zahl betroffener Clips zurück."""
        rules = self._load_rules()
        if "block" not in rules.values():
            return 0
        blocked: list[int] = []
        remote: dict[int, tuple[str, str]] = {}
        with db.session_scope() as s:
            # offene Clips + Clips mit noch geplanten Posts (nur bei YouTube geplant zählt der Clip schon als
            # 'posted', das Video ist aber noch nicht öffentlich)
            pending_ids = select(Post.clip_id).where(Post.status == "scheduled")
            clips = s.exec(
                select(Clip).where(
                    or_(col(Clip.status).in_(OPEN_CLIP_STATUSES), col(Clip.id).in_(pending_ids))
                )
            ).all()
            for clip in clips:
                cand = s.get(Candidate, clip.candidate_id)
                if cand is None:
                    continue
                try:
                    if self._rule_for(candidate_to_in(cand), rules) != "block":
                        continue
                except Exception:
                    continue
                blocked.append(int(clip.id))
                for post in s.exec(
                    select(Post).where(
                        Post.clip_id == clip.id, Post.status == "scheduled", col(Post.remote_id).is_not(None)
                    )
                ).all():
                    remote[int(post.id)] = (post.platform, str(post.remote_id))
        if not blocked:
            return 0
        # Plattform-Aufrufe außerhalb der DB-Transaktion (sonst blockiert ein langsames Netz alle Schreiber)
        pulled = {pid: self._unschedule_remote(platform, rid) for pid, (platform, rid) in remote.items()}
        n = 0
        with db.session_scope() as s:
            for clip_id in blocked:
                clip = s.get(Clip, clip_id)
                if clip is None:
                    continue
                posts = s.exec(select(Post).where(Post.clip_id == clip.id)).all()
                if clip.status not in OPEN_CLIP_STATUSES and not any(p.status == "scheduled" for p in posts):
                    continue
                manual: list[str] = []
                for post in posts:
                    if post.status != "scheduled":
                        continue
                    msg = BLOCKED_NOTE
                    if post.remote_id:  # YouTube: schon hochgeladen und dort geplant
                        ok, info = pulled.get(int(post.id), (False, "nicht versucht"))
                        if ok:
                            msg += f" – geplantes Video bei {post.platform} zurückgezogen"
                        else:
                            manual.append(f"{post.platform} (ID {post.remote_id})")
                            msg += " – " + self._manual_note(post.platform, post.remote_id, info)
                    post.status, post.error = "canceled", _short(msg, 500)
                    s.add(post)
                if any(p.status == "uploading" for p in posts):
                    continue  # läuft gerade – Status klärt sich nach dem Upload
                if any(p.status in POST_DONE for p in posts):
                    clip.status = "posted"
                else:
                    clip.status = "rejected"
                    clip.reviewed_at = now
                clip.error = BLOCKED_NOTE + (
                    " – bereits geplante Videos selbst löschen: " + ", ".join(manual) if manual else ""
                )
                s.add(clip)
                n += 1
                if manual:
                    log.error("clip %s: creator blocked, but already scheduled on %s", clip.id, manual)
                else:
                    log.warning("clip %s: creator blocked – not posted", clip.id)
        return n

    def schedule_approved(self, now: datetime | None = None) -> int:
        """Für approved-Clips je gewählter Plattform einen Post im nächsten Slot anlegen; Clip -> scheduled.
        Clips blockierter Creator werden vorher verworfen."""
        now = _utc(now) or utcnow()
        with self._job("schedule") as job:
            blocked = self._apply_blocks(now)
            enabled = set(self.enabled_platforms())
            created = 0
            problems: list[str] = []  # Clips, die nicht (vollständig) eingeplant werden konnten -> Health
            with db.session_scope() as s:
                clips = s.exec(
                    select(Clip)
                    .where(Clip.status == "approved")
                    .order_by(col(Clip.reviewed_at).asc(), col(Clip.created_at).asc())
                ).all()
                for clip in clips:
                    wanted = [p for p in (clip.platforms or []) if p in enabled]
                    if not wanted:
                        clip.error = "Keine der gewählten Plattformen ist in config.yaml aktiviert."
                        s.add(clip)
                        problems.append(f"Clip #{clip.id}: keine gewählte Plattform aktiviert")
                        continue
                    existing = {
                        p.platform
                        for p in s.exec(
                            select(Post).where(
                                Post.clip_id == clip.id, col(Post.status).not_in(["canceled", "failed"])
                            )
                        ).all()
                    }
                    ok = True
                    for platform in wanted:
                        if platform in existing:
                            continue
                        taken = self._taken_slots(s, platform, now - timedelta(days=2))
                        try:
                            when = _utc(slots.next_slot(platform, taken, now, self.settings))
                        except Exception as e:
                            clip.error = _short(f"Kein freier Slot für {platform}: {e}", 300)
                            ok = False
                            problems.append(
                                f"Clip #{clip.id} ({PLATFORM_NAMES.get(platform, platform)}): {e}"
                            )
                            log.warning("no slot for clip %s on %s: %s", clip.id, platform, e)
                            continue
                        s.add(
                            Post(
                                clip_id=clip.id,
                                platform=platform,
                                status="scheduled",
                                scheduled_at=when,
                                title=(clip.titles or {}).get(platform) or "",
                                description=(clip.descriptions or {}).get(platform) or "",
                                created_at=utcnow(),
                            )
                        )
                        s.flush()
                        created += 1
                        log.info("clip %s scheduled on %s at %s", clip.id, platform, when)
                    if ok:
                        clip.status = "scheduled"
                        clip.error = None
                    s.add(clip)
            job.message = f"{created} Posts eingeplant" + (
                f", {blocked} Clips blockierter Creator verworfen" if blocked else ""
            )
            if problems:  # sonst wartet ein freigegebener Clip unbemerkt tagelang
                job.fail(
                    f"{job.message} – {len(problems)} nicht einplanbar: "
                    + " · ".join(_short(x, 200) for x in problems[:5])
                    + (f" · (+{len(problems) - 5} weitere)" if len(problems) > 5 else "")
                )
            return created

    def requeue_clip(self, clip_id: int, now: datetime | None = None) -> list[tuple[str, datetime]]:
        """Gescheiterte (auch durch einen Neustart unterbrochene) Posts eines Clips neu einplanen: nächster
        freier Slot, Versuche zurückgesetzt. Für Dashboard und `wdvi requeue`. Gibt (Plattform, Termin) zurück."""
        now = _utc(now) or utcnow()
        rules = self._load_rules()
        enabled = set(self.enabled_platforms())
        out: list[tuple[str, datetime]] = []
        with db.session_scope() as s:
            clip = s.get(Clip, clip_id)
            if clip is None:
                raise ValueError(f"Clip {clip_id} nicht gefunden.")
            if not clip.video_path or not Path(clip.video_path).is_file():
                raise ValueError(f"Das Video von Clip {clip_id} existiert nicht mehr ({clip.video_path}).")
            cand = s.get(Candidate, clip.candidate_id)
            if cand is not None and self._rule_for(candidate_to_in(cand), rules) == "block":
                raise ValueError(f"Clip {clip_id}: {BLOCKED_NOTE} – wird nicht neu eingeplant.")
            posts = s.exec(select(Post).where(Post.clip_id == clip_id, Post.status == "failed")).all()
            posts = [p for p in posts if p.platform in enabled]
            if not posts:
                raise ValueError(
                    f"Clip {clip_id} hat keine fehlgeschlagenen Posts auf aktivierten Plattformen."
                )
            for post in posts:
                when = self._free_slot(s, post.platform, now, exclude_post=post.id)
                post.status, post.scheduled_at, post.attempts, post.error = "scheduled", when, 0, None
                post.remote_id = post.remote_url = None
                post.created_at = now  # POST_MAX_AGE zählt ab dem erneuten Einplanen
                self._post_transient.pop(int(post.id), None)
                self._early_retry_after.pop(int(post.id), None)
                self._remote_check_after.pop(int(post.id), None)
                self._remote_check_since.pop(int(post.id), None)
                s.add(post)
                s.flush()
                out.append((post.platform, when))
                log.info("post %s (clip %s, %s) requeued for %s", post.id, clip_id, post.platform, when)
            clip.status, clip.error = "scheduled", None
            s.add(clip)
        return out

    def _recover_uploads(self) -> set[int]:
        """'uploading' aus einem früheren (abgestürzten) Prozess: NICHT automatisch neu hochladen (der Upload
        kann schon angekommen sein -> Doppelpost), sondern als fehlgeschlagen mit Hinweis markieren."""
        if self._uploads_recovered:
            return set()
        self._uploads_recovered = True
        touched: set[int] = set()
        with db.session_scope() as s:
            for p in s.exec(select(Post).where(Post.status == "uploading")).all():
                p.status, p.error = "failed", INTERRUPTED_UPLOAD
                s.add(p)
                touched.add(int(p.clip_id))
                log.warning(
                    "post %s (clip %s) was interrupted while uploading – marked failed", p.id, p.clip_id
                )
        return touched

    def publish_due(self, now: datetime | None = None) -> int:
        """Fällige Posts hochladen (YouTube native_schedule: schon vorab, sobald geplant). Echte Fehler: bis zu
        MAX_POST_ATTEMPTS Versuche mit wachsendem Backoff; vorübergehende Fehler (Netz, Limits, nicht verbunden)
        zählen nicht. Clip -> posted, wenn alle Posts durch sind."""
        if not self._publish_lock.acquire(blocking=False):
            log.info("publish already running – skipped")
            return 0
        try:
            with self._job("publish") as job:
                return self._publish_due(_utc(now) or utcnow(), job)
        finally:
            self._publish_lock.release()

    def _is_due(self, post: Post, now: datetime) -> bool:
        at = _utc(post.scheduled_at)
        if at <= now:
            return True
        yt = self.settings.config.publish.youtube
        # Vorab-Upload nur beim ersten Anlauf und nur, wenn YouTube wirklich plant (publishAt geht nur für
        # "public"); nach einem Fehlschlag gilt der normale Backoff
        if (
            post.platform == "youtube"
            and yt.native_schedule
            and yt.privacy == "public"
            and post.attempts == 0
            and at <= now + YT_EARLY_WINDOW
        ):
            retry = self._early_retry_after.get(int(post.id))
            return retry is None or retry <= now
        return False

    def _publish_due(self, now: datetime, job: _JobCtx) -> int:
        touched = self._recover_uploads()
        self._apply_blocks(now)
        remote_touched, remote_notes = self._settle_remote_scheduled(now)
        touched |= remote_touched
        with db.session_scope() as s:
            candidates = s.exec(
                select(Post)
                .where(Post.status == "scheduled", col(Post.remote_id).is_(None))
                .order_by(col(Post.scheduled_at).asc())
            ).all()
            due_ids = [int(p.id) for p in candidates if self._is_due(p, now)]

        published = failed = 0
        for post_id in due_ids:
            if self._stop.is_set():
                break
            ok, clip_id = self._publish_one(post_id, now)
            touched.add(clip_id)
            if ok:
                published += 1
            elif ok is False:
                failed += 1
        if touched:
            with db.session_scope() as s:
                for clip_id in touched:
                    self._refresh_clip_status(s, clip_id)
        job.message = f"{published} hochgeladen, {failed} fehlgeschlagen"
        if remote_notes:
            job.message += " – " + "; ".join(remote_notes)
        if published or failed or remote_notes:
            log.info("publish: %s", job.message)
        return published

    def _settle_remote_scheduled(self, now: datetime) -> tuple[set[int], list[str]]:
        """Bei der Plattform geplante Videos (YouTube native_schedule, remote_id) nach publishAt abschließen.
        Kann der Publisher die Sichtbarkeit prüfen (`check_status`), wird 10–30 min nach publishAt
        nachgesehen: öffentlich -> 'posted'; noch privat (API-Projekt ohne Audit: Uploads bleiben gesperrt)
        -> 'posted' mit deutlichem Hinweis fürs Dashboard statt still "veröffentlicht"; nicht mehr vorhanden
        -> 'failed'. Ohne check_status zählt der Post ab publishAt als veröffentlicht.
        Gibt (betroffene Clip-IDs, Hinweise für die Job-Meldung) zurück."""
        with db.session_scope() as s:
            rows = [
                (int(p.id), int(p.clip_id), p.platform, str(p.remote_id), _utc(p.scheduled_at))
                for p in s.exec(
                    select(Post).where(
                        Post.status == "scheduled", col(Post.remote_id).is_not(None), Post.scheduled_at <= now
                    )
                ).all()
            ]
        touched: set[int] = set()
        notes: list[str] = []
        for post_id, clip_id, platform, remote_id, at in rows:
            name = PLATFORM_NAMES.get(platform, platform)
            try:
                publisher = self.publishers.get(platform)
            except Exception:
                publisher = None
            check = getattr(publisher, "check_status", None)
            status, note = "posted", None
            if callable(check):
                if now < at + REMOTE_CHECK_AFTER:
                    continue  # YouTube braucht nach publishAt einen Moment
                retry = self._remote_check_after.get(post_id)
                if retry is not None and now < retry:
                    continue
                try:
                    visibility = check(remote_id)
                except Exception as e:  # Publisher sollen nicht werfen
                    log.warning("status check of %s video %s failed: %s", platform, remote_id, e)
                    visibility = None
                if visibility == "private":
                    note = self._locked_private_note(platform)
                    notes.append(f"{name}-Video {remote_id} ist nach dem Termin noch privat")
                    log.error(
                        "post %s: %s video %s still private after publishAt – %s",
                        post_id,
                        platform,
                        remote_id,
                        note,
                    )
                elif visibility == "missing":
                    status, note = "failed", REMOTE_MISSING.format(name=name)
                    notes.append(f"{name}-Video {remote_id} nicht mehr vorhanden")
                    log.warning("post %s: %s video %s is gone", post_id, platform, remote_id)
                elif visibility not in ("public", "unlisted"):  # nicht prüfbar (Netz, Quota, nicht verbunden)
                    # mindestens 20 min ab der ersten Prüfung weiter versuchen – auch wenn der Termin schon länger
                    # vorbei ist (PC/Container war aus, Netz nach dem Start noch nicht da)
                    first = self._remote_check_since.setdefault(post_id, now)
                    if now < max(at + REMOTE_CHECK_UNTIL, first + (REMOTE_CHECK_UNTIL - REMOTE_CHECK_AFTER)):
                        self._remote_check_after[post_id] = now + REMOTE_CHECK_RETRY
                        continue
                    note = REMOTE_UNCHECKED.format(name=name)
                    log.warning(
                        "post %s: visibility of %s video %s could not be checked",
                        post_id,
                        platform,
                        remote_id,
                    )
            self._remote_check_after.pop(post_id, None)
            self._remote_check_since.pop(post_id, None)
            with db.session_scope() as s:
                post = s.get(Post, post_id)
                if post is None or post.status != "scheduled" or post.remote_id != remote_id:
                    continue  # inzwischen abgebrochen/geändert
                post.status = status
                if status == "posted":
                    post.posted_at = at
                post.error = _short(note, 500) if note else None
                s.add(post)
            touched.add(clip_id)
        return touched, notes

    @staticmethod
    def _locked_private_note(platform: str) -> str:
        if platform == "youtube":
            from wdvi.publish.youtube import LOCKED_PRIVATE_HINT

            return LOCKED_PRIVATE_HINT
        name = PLATFORM_NAMES.get(platform, platform)
        return f"{name} hat das Video zum geplanten Termin nicht veröffentlicht – es ist weiterhin privat."

    def _publish_one(self, post_id: int, now: datetime) -> tuple[bool | None, int]:
        """Einen Post hochladen. (True=ok, False=Fehler, None=übersprungen), clip_id."""
        with db.session_scope() as s:
            post = s.get(Post, post_id)
            clip = s.get(Clip, post.clip_id) if post is not None else None
            if post is None:
                return None, -1
            if clip is None or post.status != "scheduled":
                return None, int(post.clip_id)
            publisher = self.publishers.get(post.platform)
            if publisher is None:
                post.status, post.error = (
                    "canceled",
                    f"Plattform {post.platform} ist in config.yaml deaktiviert.",
                )
                s.add(post)
                return None, clip.id
            early = _utc(post.scheduled_at) > now  # nur YouTube native_schedule (siehe _is_due)
            req = PostRequest(
                platform=post.platform,
                video_path=clip.video_path,
                title=post.title,
                description=post.description,
                hashtags=list((clip.plan or {}).get("hashtags") or []),
                publish_at=_utc(post.scheduled_at) if early else None,
                thumb_path=clip.thumb_path,
            )
            post.status = "uploading"
            s.add(post)
            clip_id = int(clip.id)

        connected = True
        try:
            if not publisher.is_connected():
                connected = False
                result = PostResult(
                    ok=False,
                    error=f"{post.platform} ist nicht verbunden – im Dashboard unter "
                    "„Verbindungen“ verbinden.",
                )
            else:
                log.info(
                    "uploading post %s (clip %s) to %s%s",
                    post_id,
                    clip_id,
                    post.platform,
                    " (vorab, geplant)" if early else "",
                )
                result = publisher.publish(req)
        except Exception as e:  # Publisher sollen nicht werfen – falls doch: wie Fehlschlag behandeln
            log.exception("publisher %s crashed", post.platform)
            result = PostResult(ok=False, error=f"{type(e).__name__}: {_short(e, 200)}")

        with db.session_scope() as s:
            post = s.get(Post, post_id)
            if result.ok and result.status != "failed":
                post.remote_id = result.remote_id
                post.remote_url = result.remote_url
                post.error = _short(result.error, 500) if result.error else None
                if result.status == "scheduled":
                    post.status = "scheduled"  # liegt geplant auf der Plattform (remote_id gesetzt)
                    if _utc(post.scheduled_at) <= now and not callable(
                        getattr(publisher, "check_status", None)
                    ):
                        post.status, post.posted_at = "posted", utcnow()
                    # sonst prüft _settle_remote_scheduled nach dem Termin, ob es wirklich öffentlich ist
                elif result.status == "draft_sent":
                    post.status, post.posted_at = "draft_sent", utcnow()
                else:
                    post.status, post.posted_at = "posted", utcnow()
                self._early_retry_after.pop(post_id, None)
                self._post_transient.pop(post_id, None)
                s.add(post)
                return True, clip_id
            err = _short(result.error or "Unbekannter Fehler beim Hochladen", 400)
            kind = post_error_kind(result, connected)
            age = now - (_utc(post.created_at) or now)
            if early:
                # Vorab-Upload (YouTube) ist "best effort": Versuche zählen erst ab Fälligkeit
                post.status, post.error = "scheduled", err
                self._early_retry_after[post_id] = now + EARLY_RETRY
                log.warning("early upload of post %s failed (%s) – retry later", post_id, err)
            elif kind is not None and age < POST_MAX_AGE:
                # vorübergehend (Netz, Limit, nicht verbunden): kein Fehlversuch, wachsender Abstand
                n = self._post_transient.get(post_id, 0) + 1
                self._post_transient[post_id] = n
                when = now + TRANSIENT_POST_DELAYS[min(n, len(TRANSIENT_POST_DELAYS)) - 1]
                retry_after = getattr(result, "retry_after_s", None)
                if retry_after:
                    when = max(when, now + timedelta(seconds=float(retry_after)))
                if kind in ("quota", "auth"):  # nicht alles auf einmal nachholen -> nächster freier Slot
                    when = max(when, self._free_slot(s, post.platform, when, exclude_post=post_id))
                post.status, post.scheduled_at = "scheduled", when
                post.error = _short(
                    f"{err} – zählt nicht als Fehlversuch, neuer Versuch {when:%d.%m. %H:%M} UTC", 500
                )
                if post.platform == "youtube":
                    self._early_retry_after[post_id] = when  # kein Vorab-Upload vor dem neuen Termin
                log.warning("post %s: temporary problem (%s): %s – retry at %s", post_id, kind, err, when)
            elif kind is not None:
                post.status = "failed"
                post.error = _short(
                    f"{err} – seit über {POST_MAX_AGE.days} Tagen nicht hochladbar, aufgegeben", 500
                )
                log.warning("post %s given up after %s of temporary errors: %s", post_id, age, err)
            else:
                post.error = err
                post.attempts += 1
                if post.attempts >= MAX_POST_ATTEMPTS:
                    post.status = "failed"
                    log.warning("post %s failed for good after %d attempts: %s", post_id, post.attempts, err)
                else:
                    post.status = "scheduled"
                    delay = POST_RETRY_DELAYS[min(post.attempts, len(POST_RETRY_DELAYS)) - 1]
                    post.scheduled_at = now + delay
                    log.warning(
                        "post %s failed (attempt %d): %s – retry at %s",
                        post_id,
                        post.attempts,
                        err,
                        post.scheduled_at,
                    )
            s.add(post)
            return False, clip_id

    @staticmethod
    def _refresh_clip_status(s, clip_id: int) -> None:
        clip = s.get(Clip, clip_id)
        if clip is None or clip.status not in ("scheduled", "posted"):
            return
        posts = [
            p for p in s.exec(select(Post).where(Post.clip_id == clip_id)).all() if p.status != "canceled"
        ]
        if not posts:
            return
        pending = [
            p for p in posts if p.status == "uploading" or (p.status == "scheduled" and not p.remote_id)
        ]
        if pending:
            return
        done = [p for p in posts if p.status in POST_DONE or (p.status == "scheduled" and p.remote_id)]
        bad = [p for p in posts if p.status == "failed"]
        clip.status = "posted" if done else "failed"
        clip.error = (
            "Fehlgeschlagen: "
            + ", ".join(f"{p.platform} ({_short(p.error, 120)})" for p in bad)
            + f" · erneut einplanen: Dashboard „Gepostet“ → „Erneut einplanen“ oder 'wdvi requeue {clip_id}'"
            if bad
            else None
        )
        s.add(clip)

    # ------------------------------------------------------------------ feedback

    def fetch_stats(self, now: datetime | None = None) -> int:
        """Metriken nach feedback.fetch_after_hours (z. B. 24 h / 72 h) je veröffentlichtem Post holen.
        TikTok im Inbox-Modus ('draft_sent') zählt mit: der Publisher löst die publish_id auf, sobald du den
        Entwurf gepostet hast (sonst None -> nichts gespeichert)."""
        now = _utc(now) or utcnow()
        with self._job("stats") as job:
            fb = self.settings.config.feedback
            hours = sorted({int(h) for h in fb.fetch_after_hours if h and h > 0})
            if not fb.enabled or not hours:
                job.message = "Feedback deaktiviert"
                return 0
            oldest = now - timedelta(hours=max(hours)) - STATS_GRACE
            todo: list[tuple[int, str, str, list[int], datetime]] = []
            with db.session_scope() as s:
                posts = s.exec(
                    select(Post).where(
                        or_(
                            Post.status == "posted",
                            (Post.status == "draft_sent") & (Post.platform == "tiktok"),
                        ),
                        col(Post.remote_id).is_not(None),
                        col(Post.posted_at).is_not(None),
                        Post.posted_at >= oldest,
                    )
                ).all()
                for p in posts:
                    posted = _utc(p.posted_at)
                    have = set(s.exec(select(Metric.hours_after).where(Metric.post_id == p.id)).all())
                    due = [
                        h
                        for h in hours
                        if h not in have
                        and posted + timedelta(hours=h) <= now < posted + timedelta(hours=h) + STATS_GRACE
                    ]
                    if due:
                        todo.append((int(p.id), p.platform, str(p.remote_id), due, posted))
            stored = errors = 0
            for post_id, platform, remote_id, due, _posted in todo:
                publisher = self.publishers.get(platform)
                if publisher is None:
                    continue
                try:
                    m = publisher.fetch_metrics(remote_id)
                except Exception as e:
                    log.warning("metrics for post %s (%s) failed: %s", post_id, platform, e)
                    errors += 1
                    continue
                if m is None:
                    continue
                with db.session_scope() as s:
                    for h in due:
                        s.add(
                            Metric(
                                post_id=post_id,
                                fetched_at=utcnow(),
                                hours_after=h,
                                views=m.views,
                                likes=m.likes,
                                comments=m.comments,
                                shares=m.shares,
                            )
                        )
                        stored += 1
            job.message = f"{stored} Metriken gespeichert" + (f", {errors} Fehler" if errors else "")
            return stored

    def performance_examples(self, limit: int) -> list:
        """Beste und schlechteste Posts (nach Views) als PerformanceExample für den Prompt."""
        if limit <= 0:
            return []
        with db.session_scope() as s:
            metrics = s.exec(
                select(Metric)
                .where(col(Metric.views).is_not(None))
                .order_by(col(Metric.hours_after).desc(), col(Metric.fetched_at).desc())
            ).all()
            latest: dict[int, Metric] = {}
            for m in metrics:
                latest.setdefault(m.post_id, m)  # pro Post die späteste Messung
            examples: list[PerformanceExample] = []
            for post_id, m in latest.items():
                post = s.get(Post, post_id)
                clip = s.get(Clip, post.clip_id) if post is not None else None
                if post is None or clip is None:
                    continue
                examples.append(
                    PerformanceExample(
                        platform=post.platform,
                        title=post.title or (clip.titles or {}).get(post.platform, ""),
                        hook_text=str((clip.plan or {}).get("hook_text") or ""),
                        views=int(m.views or 0),
                        hours_after=int(m.hours_after),
                    )
                )
        if len(examples) <= limit:
            return sorted(examples, key=lambda e: e.views, reverse=True)
        ranked = sorted(examples, key=lambda e: e.views, reverse=True)
        n_best = (limit + 1) // 2
        return ranked[:n_best] + ranked[len(ranked) - (limit - n_best) :]

    # ------------------------------------------------------------------ housekeeping

    def cleanup(self, now: datetime | None = None) -> None:
        """work_dir-Dateien > 2 Tage löschen; ungeprüfte Clips nach cleanup.review_expire_days verwerfen;
        abgelehnte Clip-Videos > 7 Tage löschen; Videos fertig geposteter/gescheiterter Clips nach
        cleanup.keep_posted_days löschen (Vorschaubild bleibt fürs Dashboard)."""
        now = _utc(now) or utcnow()
        with self._job("cleanup") as job:
            paths = self.settings.config.paths
            files = freed = 0
            cutoff_ts = (now - WORK_MAX_AGE).timestamp()
            work = paths.work_dir
            if work.is_dir():
                entries = sorted(work.rglob("*"), key=lambda x: len(x.parts), reverse=True)  # tiefste zuerst
                old_dirs = set()
                for p in entries:
                    try:
                        if p.is_dir() and not p.is_symlink() and p.stat().st_mtime < cutoff_ts:
                            old_dirs.add(p)
                    except OSError:
                        continue
                emptied: set[Path] = set()
                for p in entries:
                    try:
                        if p.is_file() or p.is_symlink():
                            if p.lstat().st_mtime < cutoff_ts:
                                freed += p.lstat().st_size
                                p.unlink()
                                files += 1
                                emptied.add(p.parent)
                        elif p.is_dir() and (p in old_dirs or p in emptied) and not any(p.iterdir()):
                            p.rmdir()
                            emptied.add(p.parent)
                    except OSError as e:
                        log.debug("cleanup: cannot remove %s: %s", p, e)
            videos = expired = old_videos = 0
            cc = self.settings.config.cleanup
            with db.session_scope() as s:
                # ungeprüfte Clips verfallen (virale Momente sind nach einer Woche ohnehin vorbei)
                if cc.review_expire_days > 0:
                    exp_cut = now - timedelta(days=cc.review_expire_days)
                    for clip in s.exec(select(Clip).where(Clip.status == "pending_review")).all():
                        created = _utc(clip.created_at)
                        if created is not None and created < exp_cut:
                            clip.status, clip.reviewed_at = "rejected", now
                            clip.error = (
                                f"Automatisch verworfen: {cc.review_expire_days} Tage nicht geprüft "
                                "(cleanup.review_expire_days)"
                            )
                            s.add(clip)
                            expired += 1
                # wer welche Videodatei benutzt: eine Datei nur löschen, wenn kein anderer nicht abgelehnter
                # Clip sie noch braucht (z. B. erneut verarbeiteter Kandidat, Clip aus alter Version)
                users: dict[str, list[Clip]] = {}
                for clip in s.exec(select(Clip).where(col(Clip.video_path).is_not(None))).all():
                    users.setdefault(self._path_key(clip.video_path), []).append(clip)

                def shared(clip: Clip, deletable: set[int] | frozenset[int] = frozenset()) -> bool:
                    others = [
                        o
                        for o in users.get(self._path_key(clip.video_path), [])
                        if o.id != clip.id and o.status != "rejected" and o.id not in deletable
                    ]
                    if others:
                        log.info(
                            "cleanup: %s still used by clip(s) %s – kept",
                            clip.video_path,
                            ", ".join(f"#{o.id} ({o.status})" for o in others),
                        )
                    return bool(others)

                cutoff = now - REJECTED_VIDEO_MAX_AGE
                for clip in s.exec(select(Clip).where(Clip.status == "rejected")).all():
                    when = _utc(clip.reviewed_at) or _utc(clip.created_at)
                    if when is None or when > cutoff or shared(clip):
                        continue
                    size = self._remove_files(clip.video_path, clip.thumb_path)
                    if size is not None:
                        freed += size
                        videos += 1
                        self._note(clip, "Video gelöscht (abgelehnt, Aufräumen)")
                        s.add(clip)
                # fertig gepostete/gescheiterte Clips: Video nach keep_posted_days löschen, Vorschaubild bleibt
                if cc.keep_posted_days > 0:
                    keep_cut = now - timedelta(days=cc.keep_posted_days)
                    due: list[Clip] = []
                    for clip in s.exec(select(Clip).where(col(Clip.status).in_(["posted", "failed"]))).all():
                        if not clip.video_path or not Path(clip.video_path).is_file():
                            continue
                        posts = s.exec(select(Post).where(Post.clip_id == clip.id)).all()
                        if any(
                            p.status == "uploading" or (p.status == "scheduled" and not p.remote_id)
                            for p in posts
                        ):
                            continue  # wird noch gebraucht
                        times = [
                            _utc(t)
                            for p in posts
                            for t in (p.posted_at, p.scheduled_at, p.created_at)
                            if t is not None
                        ] + [_utc(t) for t in (clip.reviewed_at, clip.created_at) if t is not None]
                        if times and max(times) > keep_cut:
                            continue
                        due.append(clip)
                    due_ids = {int(c.id) for c in due}
                    for clip in due:
                        if shared(clip, due_ids):  # andere, ebenfalls fällige Clips blockieren nicht
                            continue
                        size = self._remove_files(clip.video_path)
                        if size is not None:
                            freed += size
                            old_videos += 1
                            self._note(clip, f"Video gelöscht (nach {cc.keep_posted_days} Tagen aufgeräumt)")
                            s.add(clip)
                old_runs = s.exec(select(JobRun).where(JobRun.started_at < now - JOBRUN_MAX_AGE)).all()
                for r in old_runs:
                    s.delete(r)
            job.message = (
                f"{files} Arbeitsdateien, {videos} abgelehnte und {old_videos} alte gepostete Videos gelöscht "
                f"({freed / 1e6:.0f} MB), {expired} ungeprüfte Clips verfallen, {len(old_runs)} alte "
                "Job-Einträge entfernt"
            )
            log.info("cleanup: %s", job.message)

    @staticmethod
    def _path_key(path: str | None) -> str:
        """Vergleichbarer Dateipfad (relativ/absolut, Groß-/Kleinschreibung unter Windows)."""
        if not path:
            return ""
        try:
            return os.path.normcase(str(Path(path).resolve()))
        except OSError:
            return os.path.normcase(str(path))

    @staticmethod
    def _remove_files(*files: str | None) -> int | None:
        """Dateien löschen; gibt die freigegebenen Bytes zurück (None = nichts gelöscht)."""
        freed, removed = 0, False
        for f in files:
            if f and Path(f).is_file():
                try:
                    size = Path(f).stat().st_size
                    Path(f).unlink()
                    freed += size
                    removed = True
                except OSError as e:
                    log.warning("cleanup: cannot remove %s: %s", f, e)
        return freed if removed else None

    @staticmethod
    def _note(clip: Clip, note: str) -> None:
        clip.error = f"{clip.error} · {note}" if clip.error and note not in clip.error else note
