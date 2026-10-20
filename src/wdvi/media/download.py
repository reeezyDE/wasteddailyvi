"""Downloads via yt-dlp (Python-API).

Fehler kommen als DownloadError mit deutscher Meldung und `retryable` (vorübergehend: Netz/DNS/Timeout,
HTTP 429/5xx, Bot-Check, volle Platte -> später erneut versuchen; sonst ist der Clip selbst das Problem).
Clips/Posts, die länger als filters.max_source_duration_s sind, lehnt ein yt-dlp-match_filter schon VOR dem
Download ab (TooLongError, nicht wiederholbar) – YouTube-Momente laden ohnehin nur ihren Abschnitt.
"""

from __future__ import annotations

import errno
import logging
import os
import re
from pathlib import Path

import yt_dlp
import yt_dlp.utils

from wdvi.config import Settings
from wdvi.schemas import CandidateIn, SourceKind

log = logging.getLogger(__name__)

# Beste Qualität bis 1080p: format_sort wählt die höchste Auflösung <= 1080p, bei Gleichstand H.264/AAC
# (direkt MP4-tauglich). Kein [ext=mp4]-Zwang mehr – der hatte oft das 360p-Kombiformat statt 1080p geholt.
FORMAT = "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b"
FORMAT_SORT = ["res:1080", "vcodec:h264", "acodec:aac"]
MIN_GOOD_HEIGHT = 720  # darunter wird das 1080x1920-Ergebnis sichtbar unscharf
MAX_FILESIZE = 1_500_000_000  # Schutz gegen versehentliche Komplett-Downloads langer Videos
_PARTIAL_SUFFIXES = (".part", ".ytdl", ".temp", ".tmp")
DURATION_TOLERANCE_S = 2.0  # wie pipeline.LENGTH_TOLERANCE_S: Container-Rundung nicht als "zu lang" werten
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

# vorübergehende Fehler: ein späterer neuer Versuch ist sinnvoll
_RETRYABLE_HINTS = (
    "temporary failure",
    "name resolution",
    "name or service not known",
    "nodename nor servname",
    "getaddrinfo",
    "could not resolve",
    "failed to resolve",
    "unable to resolve",
    "timed out",
    "timeout",
    "time out",
    "connection reset",
    "connection refused",
    "connection aborted",
    "connection error",
    "remote end closed",
    "network is unreachable",
    "no route to host",
    "netzwerkfehler",
    "too many requests",
    "zu viele anfragen",
    "service unavailable",
    "bad gateway",
    "gateway time",
    "internal server error",
    "sign in to confirm",
    "not a bot",
    "no space left",
    "errno 28",
)
_RETRYABLE_HTTP = re.compile(r"\bhttp(?:\s*error)?:?\s*(?:429|5\d\d)\b", re.IGNORECASE)
_URL = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE)
_warned_cookies: set[str] = set()


def _without_urls(msg: str) -> str:
    """URLs entfernen: ein Reddit-Titel wie ".../insane_timeout_glitch/" ist kein Netzwerkfehler."""
    return _URL.sub(" ", msg or "")


def is_retryable_message(msg: str) -> bool:
    """Meldung eines vorübergehenden Problems (Netz/DNS/Timeout, HTTP 429/5xx, Bot-Check, Platte voll)?"""
    low = _without_urls(msg).lower()
    return any(h in low for h in _RETRYABLE_HINTS) or bool(_RETRYABLE_HTTP.search(low))


class DownloadError(RuntimeError):
    """Download fehlgeschlagen (deutsche Meldung). `retryable`: vorübergehendes Problem – Kandidat später
    erneut versuchen statt ihn aufzugeben. Ohne Angabe wird es aus der Meldung abgeleitet."""

    def __init__(self, message: str, *, retryable: bool | None = None) -> None:
        super().__init__(message)
        self.retryable: bool = is_retryable_message(message) if retryable is None else bool(retryable)


class TooLongError(DownloadError):
    """Quelle länger als filters.max_source_duration_s – vor dem Download abgelehnt (nie wiederholbar)."""

    def __init__(self, message: str, duration_s: float | None = None) -> None:
        super().__init__(message, retryable=False)
        self.duration_s = duration_s


class _YdlLogger:
    """yt-dlp-Ausgaben ins logging statt auf stdout/stderr."""

    def debug(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def info(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def warning(self, msg: str) -> None:
        log.info("yt-dlp warning: %s", _ANSI.sub("", msg))

    def error(self, msg: str) -> None:
        log.debug("yt-dlp error: %s", _ANSI.sub("", msg))


def safe_key(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", key)


def _explain(msg: str) -> str:
    """Bekannte yt-dlp-Fehler in verständliches Deutsch übersetzen."""
    low = _without_urls(msg).lower()
    if "429" in low or "too many requests" in low:
        return "Zu viele Anfragen (HTTP 429) – die Plattform bremst gerade, später erneut versuchen."
    if "sign in to confirm" in low or "not a bot" in low:
        return ("YouTube verlangt eine Anmeldung (Bot-Schutz). Später erneut versuchen oder eine "
                "cookies.txt über WDVI_YTDLP_COOKIES_FILE hinterlegen.")
    if "ffmpeg" in low and ("not found" in low or "not installed" in low):
        return "ffmpeg fehlt – wird zum Zusammenführen von Video und Audio gebraucht."
    if "no space left" in low or "errno 28" in low:
        return "Festplatte voll – Platz in data/ schaffen, dann wird es erneut versucht."
    if _RETRYABLE_HTTP.search(low) or any(s in low for s in ("service unavailable", "bad gateway",
                                                              "gateway time", "internal server error")):
        return "Serverfehler bei der Plattform (HTTP 5xx) – später erneut versuchen."
    # Netzwerk/DNS vor "nicht verfügbar": "Temporary failure in name resolution" ist kein gelöschter Clip
    if any(s in low for s in ("timed out", "timeout", "connection", "network", "resolve", "name resolution",
                              "temporary failure", "name or service not known", "getaddrinfo",
                              "no route to host")):
        return "Netzwerkfehler – Internetverbindung prüfen, später erneut versuchen."
    if "private video" in low or "is private" in low:
        return "Das Video ist privat."
    if "requested format is not available" in low:
        return "Kein passendes Videoformat gefunden."
    if "unsupported url" in low:
        return "Diese URL wird von yt-dlp nicht unterstützt."
    if "max-filesize" in low or "larger than max-filesize" in low or "file is larger" in low:
        return "Datei zu groß (Schutzgrenze 1,5 GB)."
    if any(s in low for s in ("no longer available", "unavailable", "removed", "404", "does not exist",
                              "not found", "deleted")):
        return "Clip/Video ist nicht (mehr) verfügbar."
    return "Unbekannter Fehler."


def _is_retryable(raw: str, e: BaseException) -> bool:
    """Vorübergehender Fehler? Meldungstext plus die eigentliche Ursache (yt-dlp verpackt sie)."""
    if is_retryable_message(raw):
        return True
    exc_info = getattr(e, "exc_info", None)
    inner = exc_info[1] if isinstance(exc_info, tuple) and len(exc_info) > 1 else None
    for err in (e, e.__cause__, e.__context__, inner):
        if isinstance(err, TimeoutError | ConnectionError):
            return True
        if isinstance(err, OSError) and err.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)):
            return True
    return False


def cookie_file(settings: Settings) -> str | None:
    """cookies.txt für yt-dlp: WDVI_YTDLP_COOKIES_FILE (bzw. alter Name WDVI_YTDLP_COOKIES) aus den Secrets,
    sonst direkt aus der Umgebung. None, wenn nicht gesetzt oder die Datei fehlt (mit einmaliger Warnung)."""
    value = getattr(getattr(settings, "secrets", None), "ytdlp_cookies_file", None) or os.environ.get(
        "WDVI_YTDLP_COOKIES_FILE") or os.environ.get("WDVI_YTDLP_COOKIES")
    value = str(value or "").strip()
    if not value:
        return None
    path = Path(value).expanduser()
    if path.is_file():
        return str(path)
    if value not in _warned_cookies:
        _warned_cookies.add(value)
        log.warning("yt-dlp cookies file %s not found – downloading without cookies", value)
    return None


def _find_output(info: dict | None, dest_dir: Path, stem: str) -> Path | None:
    """Tatsächlich geschriebene (gemergte) Datei finden."""
    candidates: list[Path] = []
    if info:
        for rd in info.get("requested_downloads") or []:
            for k in ("filepath", "filename", "_filename"):
                if rd.get(k):
                    candidates.append(Path(rd[k]))
        for k in ("filepath", "_filename"):
            if info.get(k):
                candidates.append(Path(info[k]))
    candidates.append(dest_dir / f"{stem}.mp4")
    for p in candidates:
        if p.is_file() and p.stat().st_size > 0:
            return p
    globbed = [p for p in dest_dir.glob(f"{glob_escape(stem)}.*")
               if p.is_file() and p.suffix not in _PARTIAL_SUFFIXES and not re.search(r"\.f\d+\.", p.name)
               and p.stat().st_size > 0]
    return max(globbed, key=lambda p: p.stat().st_mtime) if globbed else None


def glob_escape(s: str) -> str:
    return re.sub(r"([\[\]*?])", r"[\1]", s)


def _duration_filter(max_s: float, rejected: list[float]):
    """yt-dlp-match_filter: zu lange Quellen gar nicht erst laden (Dauer steht in den Metadaten).
    Unbekannte Dauer -> laden; die Pipeline misst nach dem Download nach."""

    def match(info: dict, *, incomplete: bool = False) -> str | None:
        try:
            duration = float(info.get("duration")) if info.get("duration") is not None else None
        except (TypeError, ValueError):
            duration = None
        if duration is not None and duration > max_s + DURATION_TOLERANCE_S:
            rejected.append(duration)
            return f"wdvi: {duration:.0f} s > {max_s:.0f} s (filters.max_source_duration_s)"
        return None

    return match


def build_options(c: CandidateIn, dest_dir: Path, settings: Settings | None = None,
                  rejected: list[float] | None = None) -> dict:
    stem = safe_key(c.key)
    opts: dict = {
        "format": FORMAT,
        "format_sort": list(FORMAT_SORT),
        "merge_output_format": "mp4",
        "outtmpl": str(dest_dir / f"{stem}.%(ext)s"),
        "quiet": True,
        "no_warnings": False,
        "noprogress": True,
        "noplaylist": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "max_filesize": MAX_FILESIZE,
        "logger": _YdlLogger(),
        # Nicht-MP4-Container (z. B. Kick-TS, WebM) nach MP4 umpacken; MP4 bleibt unangetastet
        "postprocessors": [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}],
    }
    if c.source == SourceKind.youtube_moment:
        if c.start_s is None or c.end_s is None or c.end_s <= c.start_s:
            raise DownloadError(f"YouTube-Moment {c.key} hat keinen gültigen Abschnitt (start_s/end_s).")
        opts["download_ranges"] = yt_dlp.utils.download_range_func(None, [(float(c.start_s), float(c.end_s))])
        opts["force_keyframes_at_cuts"] = True
    elif settings is not None and c.source != SourceKind.youtube_video:
        max_s = float(settings.config.filters.max_source_duration_s)
        if max_s > 0:
            opts["match_filter"] = _duration_filter(max_s, rejected if rejected is not None else [])
    cookies = cookie_file(settings) if settings is not None else None
    if cookies:
        opts["cookiefile"] = cookies
    return opts


def _int(v: object) -> int | None:
    try:
        n = int(float(v))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def source_resolution(info: dict | None) -> tuple[int | None, int | None]:
    """(Breite, Höhe) des geladenen Videos aus den yt-dlp-Infos (gemergte Formate: größte Videospur)."""
    if not isinstance(info, dict):
        return None, None
    best: tuple[int | None, int | None] = (_int(info.get("width")), _int(info.get("height")))
    for key in ("requested_formats", "requested_downloads"):
        for f in info.get(key) or []:
            if not isinstance(f, dict) or f.get("vcodec") == "none":
                continue
            w, h = _int(f.get("width")), _int(f.get("height"))
            if h is not None and (best[1] is None or h > best[1]):
                best = (w, h)
    if best[1] is None:
        m = re.search(r"(\d{3,4})p", str(info.get("format_note") or info.get("resolution") or ""))
        if m:
            best = (best[0], int(m.group(1)))
        else:
            m = re.search(r"(\d{3,4})x(\d{3,4})", str(info.get("resolution") or ""))
            if m:
                best = (int(m.group(1)), int(m.group(2)))
    return best


def quality_note(info: dict | None) -> str | None:
    """Deutscher Hinweis, wenn die Quelle unter 720p liegt ("Quelle nur 360p – Ergebnis wird unscharf"),
    sonst None (auch bei unbekannter Auflösung). Maßgeblich ist die kurze Kante (Hochkant-Quellen!)."""
    w, h = source_resolution(info)
    if h is None:
        return None
    p = min(w, h) if w is not None else h
    if p >= MIN_GOOD_HEIGHT:
        return None
    return f"Quelle nur {p}p – Ergebnis wird unscharf"


def download(c: CandidateIn, dest_dir: Path, settings: Settings) -> Path:
    """Lädt den Kandidaten als MP4 (beste Qualität bis 1080p, H.264/AAC bevorzugt) nach dest_dir/<sicherer-key>.mp4.

    - youtube_moment: nur den Abschnitt [start_s, end_s] (yt-dlp download_ranges, force_keyframes_at_cuts)
    - andere Quellen: länger als filters.max_source_duration_s (laut Metadaten) -> TooLongError, ohne Download
    - existiert die Datei schon: nicht erneut laden
    - wirft DownloadError mit verständlicher Meldung und `retryable` (vorübergehend: später erneut versuchen)
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stem = safe_key(c.key)
    target = dest_dir / f"{stem}.mp4"
    if target.is_file() and target.stat().st_size > 0:
        log.debug("Already downloaded: %s", target.name)
        return target

    url = (c.extra or {}).get("linked_url") or c.url  # Reddit-Post, der auf einen Twitch-Clip verweist
    too_long: list[float] = []
    opts = build_options(c, dest_dir, settings, too_long)
    log.info("Downloading %s (%s)", c.key, url)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except DownloadError:
        raise
    except Exception as e:  # yt_dlp.utils.DownloadError, ExtractorError, PostProcessingError, OSError, ...
        raw = _ANSI.sub("", str(e)).replace("ERROR: ", "").strip()
        raise DownloadError(f"Download fehlgeschlagen ({c.platform_label}, {url}): {_explain(raw)} "
                            f"[{raw[:300]}]", retryable=_is_retryable(raw, e)) from e
    if too_long:
        max_s = settings.config.filters.max_source_duration_s
        raise TooLongError(f"Zu lang ({too_long[0]:.0f} s > {max_s:.0f} s, filters.max_source_duration_s) – "
                           "nicht heruntergeladen", duration_s=too_long[0])

    if isinstance(info, dict) and info.get("_type") == "playlist":  # sollte mit noplaylist nicht passieren
        entries = [e for e in info.get("entries") or [] if e]
        info = entries[0] if entries else info
    path = _find_output(info if isinstance(info, dict) else None, dest_dir, stem)
    if path is None:
        raise DownloadError(f"Download von {url} lieferte keine Datei (in {dest_dir}).")
    w, h = source_resolution(info if isinstance(info, dict) else None)
    log.info("Downloaded %s -> %s (%.1f MB, %s)", c.key, path.name, path.stat().st_size / 1e6,
             f"{w or '?'}x{h}" if h else "Auflösung unbekannt")
    note = quality_note(info if isinstance(info, dict) else None)
    if note:
        log.warning("%s: %s", c.key, note)
    return path
