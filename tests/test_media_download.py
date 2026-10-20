from __future__ import annotations

import http.server
import re
import threading
from pathlib import Path

import pytest
import yt_dlp

from tests.conftest import make_video
from wdvi.media import download as dl
from wdvi.media.download import DownloadError, download, safe_key
from wdvi.media.probe import probe
from wdvi.schemas import CandidateIn, SourceKind


class FakeYDL:
    """Ersetzt yt_dlp.YoutubeDL: merkt sich Optionen/URLs und schreibt eine Datei wie yt-dlp."""

    instances: list[FakeYDL] = []
    error: Exception | None = None
    report_path = True
    ext = "mp4"
    duration: float | None = None  # Metadaten-Dauer (für match_filter wie im echten yt-dlp)

    def __init__(self, opts: dict):
        self.opts = opts
        self.urls: list[str] = []
        FakeYDL.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url: str, download: bool = True):
        assert download is True
        self.urls.append(url)
        if FakeYDL.error:
            raise FakeYDL.error
        match = self.opts.get("match_filter")
        if match is not None and match({"id": "abc", "duration": FakeYDL.duration}, incomplete=False):
            return {"id": "abc", "duration": FakeYDL.duration}  # wie yt-dlp: übersprungen, nichts geladen
        out = Path(self.opts["outtmpl"].replace("%(ext)s", FakeYDL.ext))
        out.write_bytes(b"\x00" * 1024)
        Path(str(out.with_suffix("")) + ".f137.mp4.part").write_bytes(b"x")  # Reste ignorieren
        info = {"id": "abc", "ext": FakeYDL.ext}
        if FakeYDL.report_path:
            info["requested_downloads"] = [{"filepath": str(out)}]
        return info


@pytest.fixture(autouse=True)
def fake_ydl(monkeypatch):
    FakeYDL.instances, FakeYDL.error, FakeYDL.report_path, FakeYDL.ext = [], None, True, "mp4"
    FakeYDL.duration = None
    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)
    monkeypatch.delenv("WDVI_YTDLP_COOKIES", raising=False)
    monkeypatch.delenv("WDVI_YTDLP_COOKIES_FILE", raising=False)
    return FakeYDL


def test_download_twitch_clip(candidate: CandidateIn, tmp_path: Path, settings):
    path = download(candidate, tmp_path / "dl", settings)
    assert path == tmp_path / "dl" / "twitch_clip_AwkwardHelplessSalamanderSwiftRage.mp4"
    assert path.is_file()
    ydl = FakeYDL.instances[0]
    assert ydl.urls == [candidate.url]
    o = ydl.opts
    assert o["format"] == "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080][ext=mp4]/bv*[height<=1080]+ba/b"
    assert o["merge_output_format"] == "mp4"
    assert o["outtmpl"].endswith("twitch_clip_AwkwardHelplessSalamanderSwiftRage.%(ext)s")
    assert o["quiet"] and o["noprogress"]
    assert o["retries"] == 3 and o["fragment_retries"] == 3
    assert "download_ranges" not in o
    assert "cookiefile" not in o


def test_existing_file_is_not_downloaded_again(candidate: CandidateIn, tmp_path: Path, settings):
    first = download(candidate, tmp_path, settings)
    second = download(candidate, tmp_path, settings)
    assert first == second
    assert len(FakeYDL.instances) == 1


def test_reddit_linked_url_is_used(tmp_path: Path, settings, candidate: CandidateIn):
    c = candidate.model_copy(update={
        "source": SourceKind.reddit_post, "source_id": "1abcde", "url": "https://www.reddit.com/r/GTA6/comments/1abcde/x/",
        "extra": {"linked_url": "https://clips.twitch.tv/SomeClip", "linked_platform": "Twitch"}})
    download(c, tmp_path, settings)
    assert FakeYDL.instances[0].urls == ["https://clips.twitch.tv/SomeClip"]


def test_youtube_moment_downloads_only_range(tmp_path: Path, settings, candidate: CandidateIn):
    c = candidate.model_copy(update={"source": SourceKind.youtube_moment, "source_id": "dQw4w9WgXcQ",
                                     "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                                     "start_s": 120.0, "end_s": 165.5})
    path = download(c, tmp_path, settings)
    assert path.name == safe_key(c.key) + ".mp4"
    assert "@" not in path.name and ":" not in path.name
    o = FakeYDL.instances[0].opts
    assert o["force_keyframes_at_cuts"] is True
    ranges = list(o["download_ranges"]({}, None))
    assert ranges == [{"start_time": 120.0, "end_time": 165.5}]


def test_youtube_moment_without_range_fails(tmp_path: Path, settings, candidate: CandidateIn):
    c = candidate.model_copy(update={"source": SourceKind.youtube_moment, "start_s": None, "end_s": None})
    with pytest.raises(DownloadError, match="Abschnitt"):
        download(c, tmp_path, settings)


def test_output_found_by_glob(tmp_path: Path, settings, candidate: CandidateIn):
    FakeYDL.report_path, FakeYDL.ext = False, "mkv"
    path = download(candidate, tmp_path, settings)
    assert path.suffix == ".mkv" and path.is_file()


def test_yt_dlp_error_becomes_german_download_error(tmp_path: Path, settings, candidate: CandidateIn):
    FakeYDL.error = yt_dlp.utils.DownloadError(
        "\x1b[0;31mERROR:\x1b[0m [twitch:clips] Awkward: This clip is no longer available")
    with pytest.raises(DownloadError) as ei:
        download(candidate, tmp_path, settings)
    msg = str(ei.value)
    assert "Download fehlgeschlagen" in msg and "nicht (mehr) verfügbar" in msg
    assert "\x1b" not in msg


def test_rate_limit_and_bot_check_messages():
    assert "429" in dl._explain("HTTP Error 429: Too Many Requests")
    assert "Bot-Schutz" in dl._explain("Sign in to confirm you're not a bot")
    assert "ffmpeg fehlt" in dl._explain("ffmpeg not found. Please install")
    assert dl._explain("something weird") == "Unbekannter Fehler."


def test_cookies_env(tmp_path: Path, settings, candidate: CandidateIn, monkeypatch):
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n")
    monkeypatch.setenv("WDVI_YTDLP_COOKIES", str(cookies))
    download(candidate, tmp_path / "out", settings)
    assert FakeYDL.instances[0].opts["cookiefile"] == str(cookies)


def test_cookies_from_secrets_preferred(tmp_path: Path, settings, candidate: CandidateIn, monkeypatch):
    new, old = tmp_path / "new.txt", tmp_path / "old.txt"
    new.write_text("# Netscape HTTP Cookie File\n")
    old.write_text("# Netscape HTTP Cookie File\n")
    monkeypatch.setenv("WDVI_YTDLP_COOKIES", str(old))
    settings.secrets.ytdlp_cookies_file = str(new)
    assert dl.cookie_file(settings) == str(new)
    download(candidate, tmp_path / "out", settings)
    assert FakeYDL.instances[0].opts["cookiefile"] == str(new)
    # neuer Name in der Umgebung, Datei fehlt -> ohne Cookies (Warnung statt Absturz)
    settings.secrets.ytdlp_cookies_file = None
    monkeypatch.delenv("WDVI_YTDLP_COOKIES")
    monkeypatch.setenv("WDVI_YTDLP_COOKIES_FILE", str(tmp_path / "missing.txt"))
    assert dl.cookie_file(settings) is None


def test_secrets_read_both_cookie_env_names(monkeypatch):
    from wdvi.config import Secrets

    monkeypatch.setenv("WDVI_YTDLP_COOKIES", "data/old.txt")
    assert Secrets(_env_file=None).ytdlp_cookies_file == "data/old.txt"
    monkeypatch.setenv("WDVI_YTDLP_COOKIES_FILE", "data/new.txt")
    assert Secrets(_env_file=None).ytdlp_cookies_file == "data/new.txt"  # neuer Name gewinnt
    monkeypatch.setenv("WDVI_YTDLP_COOKIES_FILE", "")  # leer wie in .env.example -> alter Name gilt
    assert Secrets(_env_file=None).ytdlp_cookies_file == "data/old.txt"


@pytest.mark.parametrize("raw, retryable, needle", [
    ("[twitch:clips] X: Unable to download JSON metadata: <urlopen error [Errno -3] Temporary failure in "
     "name resolution>", True, "Netzwerkfehler"),
    ("Unable to download webpage: <urlopen error [Errno -2] Name or service not known>", True, "Netzwerkfehler"),
    ("Unable to download webpage: The read operation timed out", True, "Netzwerkfehler"),
    ("Unable to download JSON metadata: HTTP Error 429: Too Many Requests", True, "429"),
    ("Unable to download webpage: HTTP Error 503: Service Unavailable", True, "Serverfehler"),
    ("Unable to download webpage: HTTP Error 502: Bad Gateway", True, "Serverfehler"),
    ("[youtube] abc: Sign in to confirm you're not a bot", True, "Bot-Schutz"),
    ("[Errno 28] No space left on device", True, "Festplatte voll"),
    ("[twitch:clips] X: This clip is no longer available", False, "nicht (mehr) verfügbar"),
    ("Unable to download webpage: HTTP Error 404: Not Found", False, "nicht (mehr) verfügbar"),
    ("[youtube] abc: Private video. Sign in if you've been granted access", False, "privat"),
    ("Unsupported URL: https://example.com/x", False, "nicht unterstützt"),
    # Stichwörter in der URL (Reddit-Titel im Pfad) sind kein Netzwerkfehler
    ("Unsupported URL: https://www.reddit.com/r/GTA6/comments/abc/insane_timeout_glitch/", False,
     "nicht unterstützt"),
    ("[Reddit] abc: Requested format is not available. https://v.redd.it/network_connection_resolve",
     False, "Kein passendes Videoformat"),
])
def test_yt_dlp_errors_are_classified(tmp_path: Path, settings, candidate: CandidateIn, raw, retryable, needle):
    FakeYDL.error = yt_dlp.utils.DownloadError("ERROR: " + raw)
    with pytest.raises(DownloadError) as ei:
        download(candidate, tmp_path / "out", settings)
    assert ei.value.retryable is retryable, raw
    assert needle in str(ei.value)


def test_retryable_from_wrapped_cause(tmp_path: Path, settings, candidate: CandidateIn):
    err = yt_dlp.utils.DownloadError("ERROR: something odd")
    err.__cause__ = TimeoutError("read timed out")
    FakeYDL.error = err
    with pytest.raises(DownloadError) as ei:
        download(candidate, tmp_path / "out", settings)
    assert ei.value.retryable is True


def test_download_error_retryable_defaults():
    assert DownloadError("Zu viele Anfragen (HTTP 429)").retryable is True
    assert DownloadError("Clip/Video ist nicht (mehr) verfügbar.").retryable is False
    assert DownloadError("Clip/Video ist nicht (mehr) verfügbar.", retryable=True).retryable is True
    assert DownloadError("Netzwerkfehler", retryable=False).retryable is False
    # die URL in der Meldung zählt nicht ("timeout" im Reddit-Titel)
    assert DownloadError("Download von https://reddit.com/r/GTA6/comments/x/timeout_glitch/ lieferte keine "
                         "Datei").retryable is False


def test_long_source_refused_before_download(tmp_path: Path, settings, candidate: CandidateIn):
    settings.config.filters.max_source_duration_s = 180.0
    c = candidate.model_copy(update={"duration_s": None})
    FakeYDL.duration = 900.0
    with pytest.raises(dl.TooLongError) as ei:
        download(c, tmp_path / "out", settings)
    assert ei.value.retryable is False and ei.value.duration_s == 900.0
    assert "Zu lang" in str(ei.value) and "max_source_duration_s" in str(ei.value)
    assert not list((tmp_path / "out").glob("*.mp4"))  # nichts heruntergeladen
    # knapp drüber (Container-Rundung) und unbekannte Dauer: wird geladen
    FakeYDL.duration = 181.5
    assert download(c, tmp_path / "out2", settings).is_file()
    FakeYDL.duration = None
    assert download(c, tmp_path / "out3", settings).is_file()


def test_moments_have_no_duration_filter(tmp_path: Path, settings, candidate: CandidateIn):
    c = candidate.model_copy(update={"source": SourceKind.youtube_moment, "source_id": "v",
                                     "start_s": 10.0, "end_s": 40.0})
    FakeYDL.duration = 3600.0  # das ganze Video ist lang, geladen wird nur der Abschnitt
    assert download(c, tmp_path / "out", settings).is_file()
    assert "match_filter" not in FakeYDL.instances[-1].opts


def test_safe_key():
    assert safe_key("youtube_moment:abc@12.0-30.5") == "youtube_moment_abc_12_0-30_5"


# --------------------------------------------------------------------------- echtes yt-dlp gegen lokalen Server

REAL_YDL = yt_dlp.YoutubeDL


class _RangeHandler(http.server.BaseHTTPRequestHandler):
    """Minimaler HTTP-Server mit Range-Support (ffmpeg braucht ihn für Abschnitts-Downloads)."""

    root = "."

    def log_message(self, *a):
        pass

    def do_HEAD(self):  # http.server-API
        self._serve(head=True)

    def do_GET(self):  # http.server-API
        self._serve(head=False)

    def _serve(self, head: bool):
        path = Path(self.root) / self.path.lstrip("/").split("?")[0]
        if not path.is_file():
            self.send_error(404)
            return
        size = path.stat().st_size
        start, end = 0, size - 1
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if m:
            start = int(m.group(1)) if m.group(1) else 0
            end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        if head:
            return
        with path.open("rb") as fh:
            fh.seek(start)
            remaining = end - start + 1
            try:
                while remaining > 0:
                    chunk = fh.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass


@pytest.fixture
def local_server(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(yt_dlp, "YoutubeDL", REAL_YDL)
    for var in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(var, "127.0.0.1,localhost")
    root = tmp_path / "srv"
    root.mkdir()
    handler = type("H", (_RangeHandler,), {"root": str(root)})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield root, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


@pytest.mark.slow
def test_real_yt_dlp_full_download(local_server, tmp_path: Path, settings, candidate: CandidateIn):
    root, base = local_server
    make_video(root / "clip.mp4", seconds=3, size="640x360")
    c = candidate.model_copy(update={"source": SourceKind.reddit_post, "source_id": "r1",
                                     "url": f"{base}/clip.mp4"})
    path = download(c, tmp_path / "out", settings)
    assert path == tmp_path / "out" / "reddit_post_r1.mp4"
    info = probe(path)
    assert info.duration == pytest.approx(3.0, abs=0.2) and info.has_audio


@pytest.mark.slow
def test_real_yt_dlp_section_download(local_server, tmp_path: Path, settings, candidate: CandidateIn):
    root, base = local_server
    make_video(root / "long.mp4", seconds=20, size="640x360")
    c = candidate.model_copy(update={"source": SourceKind.youtube_moment, "source_id": "vid1",
                                     "url": f"{base}/long.mp4", "start_s": 5.0, "end_s": 11.0})
    path = download(c, tmp_path / "out", settings)
    assert path.name == "youtube_moment_vid1_5_0-11_0.mp4"
    assert probe(path).duration == pytest.approx(6.0, abs=0.3)
    assert not list((tmp_path / "out").glob("*.part"))


@pytest.mark.slow
def test_real_yt_dlp_404_is_download_error(local_server, tmp_path: Path, settings, candidate: CandidateIn):
    _, base = local_server
    c = candidate.model_copy(update={"url": f"{base}/missing.mp4"})
    with pytest.raises(DownloadError, match="nicht \\(mehr\\) verfügbar"):
        download(c, tmp_path / "out", settings)
