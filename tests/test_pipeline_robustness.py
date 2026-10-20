"""Pipeline im Dauerbetrieb: vorübergehende Fehler, Längen-Check nach dem Download, Livestreams,
Neustarts, quellübergreifende Duplikate, Creator-Block nach der Freigabe, Musik ungeprüft,
bearbeitete Beschreibungen, Aufräumen und Speicherplatz."""

from __future__ import annotations

import shutil
from collections import namedtuple
from datetime import timedelta
from pathlib import Path

from sqlmodel import select

from tests.test_pipeline_support import (
    NOW,
    FakePublisher,
    FakeSource,
    MediaFakes,
    make_analysis,
    make_cand,
    make_pipeline,
    make_plan,
)
from wdvi import db as dbmod
from wdvi.brain import claude as brain_claude
from wdvi.media.download import DownloadError
from wdvi.models import Candidate, Clip, CreatorRule, JobRun, Metric, Post
from wdvi.pipeline import (
    MAX_LLM_RETRIES,
    MAX_MEDIA_ATTEMPTS,
    MEDIA_PAUSE,
    MUSIC_UNCHECKED,
    candidate_from_in,
    is_transient_error,
    linked_clip_key,
    post_error_kind,
    utcnow,
)
from wdvi.publish import text
from wdvi.schemas import PostMetrics, PostResult, SourceKind

NET_ERR = (
    "Download fehlgeschlagen (Twitch, https://clips.twitch.tv/X): Unbekannter Fehler. [Unable to download "
    "JSON metadata: <urlopen error [Errno -3] Temporary failure in name resolution>]"
)


def _cand(key: str) -> Candidate:
    with dbmod.session_scope() as s:
        return s.exec(select(Candidate).where(Candidate.key == key)).one()


def _setup(settings, monkeypatch, cands, **kw):
    fakes = MediaFakes(monkeypatch)
    p = make_pipeline(settings, sources=[FakeSource("twitch", cands)], **kw)
    p.discover(now=NOW)
    return p, fakes


def _expire_backoff() -> None:
    with dbmod.session_scope() as s:
        for row in s.exec(select(Candidate)).all():
            row.extra = {**(row.extra or {}), "next_try_at": None}


def _last_run(job: str) -> JobRun:
    with dbmod.session_scope() as s:
        return s.exec(select(JobRun).where(JobRun.job == job).order_by(JobRun.id)).all()[-1]


# --------------------------------------------------------------------------- Fehler-Einordnung


def test_transient_error_classification():
    assert is_transient_error(DownloadError(NET_ERR))
    assert is_transient_error(DownloadError("Zu viele Anfragen (HTTP 429) – die Plattform bremst gerade"))
    assert is_transient_error(DownloadError("[...] HTTP Error 503: Service Unavailable"))
    assert is_transient_error(
        DownloadError("YouTube verlangt eine Anmeldung (Bot-Schutz) [Sign in to confirm]")
    )
    assert is_transient_error(OSError(28, "No space left on device"))
    assert is_transient_error(RuntimeError("Whisper-Modell 'large-v3-turbo' konnte nicht geladen werden"))
    flagged = DownloadError("irgendwas")
    flagged.retryable = True
    assert is_transient_error(flagged)
    assert not is_transient_error(DownloadError("Clip/Video ist nicht (mehr) verfügbar. [HTTP Error 404]"))
    assert not is_transient_error(DownloadError("Das Video ist privat."))
    assert not is_transient_error(KeyError("boom"))


def test_post_error_classification():
    def kind(msg: str, **kw) -> str | None:
        return post_error_kind(PostResult(ok=False, error=msg, **kw))

    assert post_error_kind(PostResult(ok=False, error="x"), connected=False) == "auth"
    assert kind("YouTube-Anmeldung ungültig oder abgelaufen – im Dashboard neu verbinden.") == "auth"
    assert kind("YouTube-API-Kontingent für heute aufgebraucht (Standard 10.000 Einheiten") == "quota"
    assert kind("TikTok: Zu viele offene Entwürfe in der Inbox (max. 5 in 24 h)") == "quota"
    assert kind("Instagram-Rate-Limit erreicht – später erneut versuchen.") == "quota"
    assert kind("Netzwerkfehler bei Instagram (ConnectError): ...") == "transient"
    assert kind("YouTube-API-Fehler (HTTP 503, backendError): unbekannt") == "transient"
    assert kind("anything", retryable=True) == "transient"
    assert kind("Netzwerkfehler", retryable=False) is None  # Publisher weiß es besser
    assert kind("YouTube lehnt den Titel ab (leer, zu lang oder ungültige Zeichen).") is None
    assert kind("TikTok: Dieses Konto darf derzeit nicht posten.") is None


def test_linked_clip_key():
    assert (
        linked_clip_key("https://clips.twitch.tv/FunnyBigLlamaPogChamp")
        == "twitch_clip:FunnyBigLlamaPogChamp"
    )
    assert linked_clip_key("https://www.twitch.tv/shroud/clip/Slug-AbC_1?x=1") == "twitch_clip:Slug-AbC_1"
    assert linked_clip_key("https://kick.com/streamer/clips/clip_01HABC") == "kick_clip:clip_01HABC"
    assert linked_clip_key("https://kick.com/streamer?clip=clip_01HABC") == "kick_clip:clip_01HABC"
    assert linked_clip_key("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=5") == "youtube_video:dQw4w9WgXcQ"
    assert linked_clip_key("https://youtu.be/dQw4w9WgXcQ") == "youtube_video:dQw4w9WgXcQ"
    assert linked_clip_key("https://streamable.com/abc") is None
    assert linked_clip_key(None) is None


# --------------------------------------------------------------------------- vorübergehende Fehler


def test_transient_download_error_pauses_and_keeps_candidate(settings, db, monkeypatch):
    """Netz/DNS-Fehler beim Download: Kandidat zurück auf 'new' (extra.media_retries) und die Verarbeitung
    pausiert sofort – der nächste Kandidat würde genauso scheitern."""
    cands = [make_cand("Top", views=90000), make_cand("Next", views=1000)]
    p, fakes = _setup(settings, monkeypatch, cands)
    err = DownloadError(NET_ERR)
    assert err.retryable  # "Temporary failure in name resolution"
    fakes.download_errors["twitch_clip:Top"] = err
    assert p.process_cycle(now=NOW) == 0
    assert fakes.downloads == ["twitch_clip:Top"]  # "Next" wird nicht verbrannt
    top = _cand("twitch_clip:Top")
    assert top.status == "new" and top.extra["media_retries"] == 1 and "neuer Versuch" in top.status_reason
    assert p._not_before(top) > utcnow() + timedelta(minutes=10)
    assert _cand("twitch_clip:Next").status == "new"
    run = _last_run("process")
    assert run.ok is False and "pausiert" in run.message and "Download" in run.message
    # während der Pause passiert nichts (auch kein Download)
    assert p.process_cycle(now=NOW) == 0 and len(fakes.downloads) == 1
    assert _last_run("process").message.startswith("Pausiert bis")
    # nach der Pause: Top wartet noch auf seinen Backoff, Next läuft durch
    p._media_pause_until = None
    assert p.process_cycle(now=NOW) == 1
    assert "warten auf einen neuen Versuch" in _last_run("process").message
    # nach MAX_MEDIA_ATTEMPTS Versuchen insgesamt endgültig 'failed'
    for _ in range(MAX_MEDIA_ATTEMPTS - 1):
        _expire_backoff()
        p._media_pause_until = None
        p.process_cycle(now=NOW)
    top = _cand("twitch_clip:Top")
    assert top.status == "failed" and "aufgegeben" in top.status_reason
    assert fakes.downloads.count("twitch_clip:Top") == MAX_MEDIA_ATTEMPTS
    # Erfolg nach dem Netzausfall: Kandidat wird ganz normal fertig
    cid = p.add_candidate(make_cand("Top", views=90000))
    fakes.download_errors.clear()
    assert p.process_candidate(cid) is not None


def test_manual_readd_resets_retry_counters(settings, db, monkeypatch):
    """process-url auf einen Kandidaten, der nach MAX_MEDIA_ATTEMPTS aufgegeben wurde: neuer Anlauf mit vollen
    Versuchen – ein weiterer Netzfehler darf ihn nicht sofort wieder endgültig scheitern lassen."""
    p, fakes = _setup(settings, monkeypatch, [make_cand("Top", views=90000)])
    fakes.download_errors["twitch_clip:Top"] = DownloadError(NET_ERR)
    for _ in range(MAX_MEDIA_ATTEMPTS):
        _expire_backoff()
        p._media_pause_until = None
        p.process_cycle(now=NOW)
    top = _cand("twitch_clip:Top")
    assert top.status == "failed" and top.extra["media_retries"] == MAX_MEDIA_ATTEMPTS
    with dbmod.session_scope() as s:  # weitere Zähler aus früheren Fehlern
        row = s.get(Candidate, top.id)
        row.extra = {**row.extra, "llm_retries": 3, "retries": 5, "process_resets": 2, "streak_reverts": 1,
                     "next_try_at": (utcnow() + timedelta(hours=5)).isoformat(), "subreddit": "GTA6"}
    cid = p.add_candidate(make_cand("Top", views=90000))
    extra = _cand("twitch_clip:Top").extra
    assert not {"media_retries", "llm_retries", "retries", "process_resets", "streak_reverts",
                "next_try_at"} & set(extra)
    assert extra["subreddit"] == "GTA6"  # andere Einträge bleiben
    assert p.process_candidate(cid) is None
    top = _cand("twitch_clip:Top")
    assert top.status == "new" and top.extra["media_retries"] == 1


def test_media_pause_grows_and_resets_after_success(settings, db, monkeypatch):
    cands = [make_cand(f"C{i}", views=1000 * (10 - i)) for i in range(3)]
    p, fakes = _setup(settings, monkeypatch, cands)
    fakes.download_errors["twitch_clip:C0"] = DownloadError("x", retryable=True)
    p.process_cycle(now=NOW)
    first = p._media_pause_until - utcnow()
    assert timedelta(minutes=14) < first <= MEDIA_PAUSE
    _expire_backoff()
    p._media_pause_until = None
    p.process_cycle(now=NOW)  # wieder C0, wieder Fehler -> doppelt so lange Pause
    assert p._media_pause_until - utcnow() > first + timedelta(minutes=10)
    fakes.download_errors.clear()
    _expire_backoff()
    p._media_pause_until = None
    assert p.process_cycle(now=NOW) == 3 and p._pause_level == 0  # Erfolg setzt den Backoff zurück


def test_non_retryable_download_error_fails_without_pause(settings, db, monkeypatch):
    p, fakes = _setup(settings, monkeypatch, [make_cand("Gone", views=9000), make_cand("Ok", views=10)])
    gone = DownloadError("Download fehlgeschlagen: Clip/Video ist nicht (mehr) verfügbar. [HTTP Error 404]")
    assert gone.retryable is False
    fakes.download_errors["twitch_clip:Gone"] = gone
    assert p.process_cycle(now=NOW) == 1  # "Ok" läuft im selben Zyklus weiter
    assert _cand("twitch_clip:Gone").status == "failed" and p._media_pause_until is None
    # ausdrücklich nicht wiederholbar schlägt Text-Hinweise ("timeout" im Text)
    assert not is_transient_error(DownloadError("timeout im Titel", retryable=False))


def test_transient_whisper_error_pauses_too(settings, db, monkeypatch):
    from wdvi.media import analyze as media_analyze
    from wdvi.media.transcribe import TranscribeError

    p, fakes = _setup(settings, monkeypatch, [make_cand("A", views=900), make_cand("B", views=10)])
    real = media_analyze.analyze
    calls = []

    def flaky(*a, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise TranscribeError("Whisper-Modell 'large-v3-turbo' konnte nicht geladen werden", retryable=True)
        return real(*a, **kw)

    monkeypatch.setattr(media_analyze, "analyze", flaky)
    assert p.process_cycle(now=NOW) == 0 and len(calls) == 1
    a = _cand("twitch_clip:A")
    assert a.status == "new" and a.extra["media_retries"] == 1 and "Transkription" in a.status_reason
    assert _cand("twitch_clip:B").status == "new"
    # nicht wiederholbarer Whisper-Fehler (kaputte Audiospur) -> failed, keine Pause
    p._media_pause_until = None
    calls.clear()

    def broken(*a, **kw):
        raise TranscribeError("Audio von x.mp4 konnte nicht dekodiert werden")

    monkeypatch.setattr(media_analyze, "analyze", broken)
    p.process_cycle(now=NOW)
    assert _cand("twitch_clip:B").status == "failed" and p._media_pause_until is None


def test_too_long_source_is_skipped_before_download(settings, db, monkeypatch):
    from wdvi.media.download import TooLongError

    p, fakes = _setup(settings, monkeypatch, [make_cand("Long", duration=None), make_cand("Ok", views=1)])
    fakes.download_errors["twitch_clip:Long"] = TooLongError(
        "Zu lang (600 s > 180 s, filters.max_source_duration_s) – nicht heruntergeladen", duration_s=600.0
    )
    assert p.process_cycle(now=NOW) == 1
    row = _cand("twitch_clip:Long")
    assert row.status == "skipped" and "Zu lang" in row.status_reason and row.duration_s == 600.0
    assert p._media_pause_until is None and p._fail_streak == 0


def test_outage_pauses_processing_instead_of_burning_queue(settings, db, monkeypatch):
    """Internet weg: schon nach dem ersten vorübergehenden Download-Fehler Pause, die Warteschlange bleibt 'new'."""
    cands = [make_cand(f"C{i}", views=1000 * (10 - i)) for i in range(6)]
    p, fakes = _setup(settings, monkeypatch, cands)
    for c in cands:
        fakes.download_errors[c.key] = DownloadError(NET_ERR)
    assert p.process_cycle(now=NOW) == 0
    assert len(fakes.downloads) == 1
    assert all(_cand(c.key).status == "new" for c in cands)
    run = _last_run("process")
    assert run.ok is False and "vorübergehender" in run.message and "pausiert" in run.message
    assert p.process_cycle(now=NOW) == 0 and len(fakes.downloads) == 1  # während der Pause nichts


def test_transient_streak_still_pauses_for_other_errors(settings, db, monkeypatch):
    """Vorübergehende Fehler außerhalb von Download/Whisper (z. B. Platte voll beim Rendern) zählen weiter
    über die Serie: zwei verschiedene Kandidaten in Folge -> Pause."""
    from wdvi.render import renderer

    cands = [make_cand(f"C{i}", views=1000 * (10 - i)) for i in range(4)]
    p, fakes = _setup(settings, monkeypatch, cands)

    def full(*a, **kw):
        raise renderer.RenderError("ffmpeg: No space left on device")

    monkeypatch.setattr(renderer, "render_clip", full)
    assert p.process_cycle(now=NOW) == 0
    assert len(fakes.downloads) == 2
    run = _last_run("process")
    assert run.ok is False and "vorübergehende Fehler" in run.message


def test_disk_full_is_transient(settings, db, monkeypatch):
    from wdvi.render import renderer

    p, fakes = _setup(settings, monkeypatch, [make_cand("ClipA")])

    def full(*a, **kw):
        raise renderer.RenderError("ffmpeg: No space left on device")

    monkeypatch.setattr(renderer, "render_clip", full)
    p.process_cycle(now=NOW)
    row = _cand("twitch_clip:ClipA")
    assert row.status == "new" and row.extra["retries"] == 1


def test_poison_llm_candidate_does_not_block_queue(settings, db, monkeypatch):
    """Ein Kandidat, dessen Claude-Aufruf immer in einen Timeout läuft, blockiert nicht alles andere und
    gibt nach MAX_LLM_RETRIES auf."""
    cands = [
        make_cand("Poison", views=90000),
        make_cand("Other1", views=2000),
        make_cand("Other2", views=1000),
    ]
    p, fakes = _setup(settings, monkeypatch, cands)
    timeout = brain_claude.LLMError(
        "Claude-CLI hat nach 600 s nicht geantwortet (llm.cli_timeout_s).", retryable=True
    )
    fakes.plan_by_key["twitch_clip:Poison"] = timeout
    assert p.process_cycle(now=NOW) == 2  # die anderen laufen im selben Zyklus weiter
    assert p._llm_pause_until is None  # kein Kapazitätsproblem -> Claude nicht global pausiert
    assert _cand("twitch_clip:Poison").extra["llm_retries"] == 1
    for _ in range(MAX_LLM_RETRIES):
        _expire_backoff()
        p.process_cycle(now=NOW)
    poison = _cand("twitch_clip:Poison")
    assert poison.status == "failed" and "Claude" in poison.status_reason


def test_llm_rate_limit_does_not_count_against_candidate(settings, db, monkeypatch):
    p, fakes = _setup(settings, monkeypatch, [make_cand("ClipA")])
    limited = brain_claude.LLMError("Stundenlimit für Claude-Aufrufe erreicht (20/h)", retryable=True)
    fakes.plan_by_key["twitch_clip:ClipA"] = limited
    for _ in range(MAX_LLM_RETRIES + 2):
        p._llm_pause_until = None
        p.process_cycle(now=NOW)
    row = _cand("twitch_clip:ClipA")
    assert row.status == "new" and "llm_retries" not in row.extra and p._llm_pause_until is not None


def test_live_youtube_video_is_retried_later(settings, db, monkeypatch):
    from wdvi.brain import editor
    from wdvi.sources import youtube as yt_source
    from wdvi.sources.base import SourceError

    MediaFakes(monkeypatch)
    state = {"live": True}

    def fetch(url, s):
        if state["live"]:
            raise SourceError("YouTube-Video läuft noch live bzw. ist geplant – später erneut versuchen.")
        return yt_source.VideoContext(video_id="v", title="t", channel="c", duration=3600.0)

    monkeypatch.setattr(yt_source, "fetch_video_context", fetch)
    monkeypatch.setattr(editor, "mine_moments", lambda *a, **kw: [])
    videos = [
        make_cand(f"v{i}", source=SourceKind.youtube_video, platform="YouTube", duration=None)
        for i in range(4)
    ]
    p = make_pipeline(settings, sources=[FakeSource("youtube", videos)])
    p.discover(now=NOW)
    p.process_cycle(now=NOW)
    for v in videos:
        row = _cand(v.key)
        assert row.status == "new" and "Livestream" in row.status_reason and row.extra["next_try_at"]
    assert _last_run("process").ok is True  # kein Fehler, keine Pause
    assert p._fail_pause_until is None
    state["live"] = False
    _expire_backoff()
    p.process_cycle(now=NOW)
    assert all(_cand(v.key).status == "mined" for v in videos)


def test_reset_interrupted_on_start(settings, db, monkeypatch):
    p, fakes = _setup(settings, monkeypatch, [make_cand("Busy"), make_cand("Waiting")])
    with dbmod.session_scope() as s:
        for row in s.exec(select(Candidate)).all():
            row.status = "processing" if row.key.endswith("Busy") else "queued"
            row.updated_at = utcnow()  # frisch – würde sonst 3 h warten
    assert p.reset_interrupted() == 2
    busy, waiting = _cand("twitch_clip:Busy"), _cand("twitch_clip:Waiting")
    assert busy.status == "new" and busy.extra["process_resets"] == 1
    assert waiting.status == "new" and "process_resets" not in waiting.extra
    assert p.process_cycle(now=NOW) == 2


# --------------------------------------------------------------------------- Länge nach dem Download


def test_unknown_duration_is_checked_after_download(settings, db, monkeypatch, sample_video):
    """Reddit-Link ohne Dauer: echtes 8-s-Video, Grenze 5 s -> nach dem Download übersprungen, ohne
    Whisper/Claude; die gemessene Dauer wird gespeichert und der Download gelöscht."""
    settings.config.filters.max_source_duration_s = 5.0
    settings.config.filters.min_source_duration_s = 1.0
    reddit = make_cand(
        "r1",
        source=SourceKind.reddit_post,
        platform="Reddit",
        creator="u/x",
        handle="x",
        duration=None,
        extra={"linked_url": "https://www.youtube.com/watch?v=LongVid1234", "linked_platform": "YouTube"},
    )
    p, fakes = _setup(settings, monkeypatch, [reddit])
    from wdvi.media import download as media_download

    def real_download(c, dest_dir, s):
        fakes.downloads.append(c.key)
        out = Path(dest_dir) / "source.mp4"
        shutil.copy(sample_video, out)
        return out

    monkeypatch.setattr(media_download, "download", real_download)
    assert p.process_cycle(now=NOW) == 0
    row = _cand(reddit.key)
    assert row.status == "skipped" and "Zu lang" in row.status_reason, row.status_reason
    assert row.duration_s is not None and 7.5 < row.duration_s < 8.5
    assert fakes.analyses == [] and fakes.plans == []
    assert not (settings.config.paths.work_dir / "reddit_post_r1").exists()


def test_length_checked_from_analysis_when_probe_fails(settings, db, monkeypatch):
    settings.config.filters.max_source_duration_s = 20.0  # Fake-Analyse meldet 30 s
    reddit = make_cand("r2", source=SourceKind.reddit_post, platform="Reddit", handle="x", duration=None)
    p, fakes = _setup(settings, monkeypatch, [reddit])
    assert p.process_cycle(now=NOW) == 0
    row = _cand(reddit.key)
    assert row.status == "rejected" and "Zu lang" in row.status_reason and fakes.plans == []


def test_youtube_moment_is_not_length_checked(settings, db, monkeypatch):
    settings.config.filters.max_source_duration_s = 20.0
    moment = make_cand(
        "vid", source=SourceKind.youtube_moment, platform="YouTube", duration=30.0, start_s=10.0, end_s=40.0
    )
    p, fakes = _setup(settings, monkeypatch, [])
    cid = p.add_candidate(moment)
    assert p.process_candidate(cid) is not None


# --------------------------------------------------------------------------- Duplikate über Quellen hinweg


def _reddit_link(pid: str, url: str):
    return make_cand(
        pid,
        source=SourceKind.reddit_post,
        platform="Reddit",
        creator="u/poster",
        handle="poster",
        duration=None,
        extra={"linked_url": url, "linked_platform": "Twitch"},
    )


def test_reddit_link_to_known_twitch_clip_is_skipped(settings, db, monkeypatch):
    twitch = make_cand("SlugA")
    p, fakes = _setup(settings, monkeypatch, [twitch])
    reddit = _reddit_link("r1abc", "https://www.twitch.tv/xqc/clip/SlugA")
    assert p.upsert_candidates([reddit], NOW) == (0, 0, 1)
    row = _cand(reddit.key)
    assert row.status == "skipped" and "twitch_clip:SlugA" in row.status_reason
    assert row.extra["linked_key"] == "twitch_clip:SlugA"


def test_twitch_clip_found_after_reddit_post(settings, db, monkeypatch):
    reddit = _reddit_link("r1abc", "https://clips.twitch.tv/SlugA")
    p, fakes = _setup(settings, monkeypatch, [reddit])
    # Reddit-Post noch unbearbeitet -> Original-Quelle bevorzugen
    assert p.upsert_candidates([make_cand("SlugA")], NOW) == (1, 0, 0)
    assert _cand(reddit.key).status == "skipped"
    # schon verarbeitet -> der Twitch-Clip wird übersprungen
    reddit2 = _reddit_link("r2abc", "https://clips.twitch.tv/SlugB")
    p.upsert_candidates([reddit2], NOW)
    p.process_cycle(now=NOW)
    assert _cand(reddit2.key).status == "rendered"
    assert p.upsert_candidates([make_cand("SlugB")], NOW) == (0, 0, 1)
    assert "r2abc" in _cand("twitch_clip:SlugB").status_reason
    # zwei Reddit-Posts (Crosspost) auf denselben Clip: nur einer
    reddit3 = _reddit_link("r3abc", "https://clips.twitch.tv/SlugC")
    reddit4 = _reddit_link("r4abc", "https://clips.twitch.tv/SlugC")
    assert p.upsert_candidates([reddit3, reddit4], NOW) == (1, 0, 1)


def test_footage_rejected_by_claude_is_not_sent_again(settings, db, monkeypatch):
    first = make_cand("SlugA", views=9000)
    repost = make_cand("r1abc", source=SourceKind.reddit_post, platform="Reddit", handle="x", views=None)
    p, fakes = _setup(settings, monkeypatch, [first, repost])
    fakes.analysis_by_key[first.key] = make_analysis("x", phashes=["ffd8c0a0b0e0f000", "0f0f0f0f0f0f0f0f"])
    fakes.analysis_by_key[repost.key] = make_analysis("x", phashes=["ffd8c0a0b0e0f001", "0f0f0f0f0f0f0f0e"])
    fakes.plan_by_key[first.key] = make_plan(keep=False, reject_reason="nothing happens")
    p.process_cycle(now=NOW)
    assert fakes.plans == [first.key]
    assert _cand(first.key).extra["phashes"] == ["ffd8c0a0b0e0f000", "0f0f0f0f0f0f0f0f"]
    rp = _cand(repost.key)
    assert rp.status == "rejected" and "aussortierten" in rp.status_reason and fakes.renders == []


# --------------------------------------------------------------------------- Creator-Block nach Freigabe


def _approved_clip(p, fakes, settings, platforms=("youtube", "tiktok")) -> int:
    p.process_cycle(now=NOW)
    with dbmod.session_scope() as s:
        clip = s.exec(select(Clip)).one()
    p.approve_clip(clip.id, list(platforms))
    return clip.id


def test_block_rule_cancels_scheduled_posts(settings, db, monkeypatch):
    settings.config.publish.tiktok.enabled = True
    yt = FakePublisher("youtube", results=[PostResult(ok=True, remote_id="v1", status="scheduled")])
    tt = FakePublisher("tiktok")
    p, fakes = _setup(settings, monkeypatch, [make_cand("ClipA")], publishers={"youtube": yt, "tiktok": tt})
    clip_id = _approved_clip(p, fakes, settings)
    assert p.schedule_approved(now=NOW) == 2
    assert p.publish_due(now=NOW) == 1  # YouTube vorab hochgeladen und dort geplant
    with dbmod.session_scope() as s:
        s.add(CreatorRule(platform_label="Twitch", handle="xqc", rule="block", note="Opt-out"))
    assert p.publish_due(now=NOW + timedelta(hours=5)) == 0
    assert tt.requests == []
    with dbmod.session_scope() as s:
        posts = {x.platform: x for x in s.exec(select(Post)).all()}
        clip = s.get(Clip, clip_id)
    assert posts["tiktok"].status == "canceled" and "blockiert" in posts["tiktok"].error
    assert posts["youtube"].status == "canceled" and "v1" in posts["youtube"].error  # von Hand löschen!
    assert clip.status == "rejected" and "v1" in clip.error


def test_block_rule_rejects_approved_and_pending_clips(settings, db, monkeypatch):
    p, fakes = _setup(
        settings,
        monkeypatch,
        [make_cand("ClipA"), make_cand("ClipB", views=10)],
        publishers={"youtube": FakePublisher("youtube")},
    )
    p.process_cycle(now=NOW)
    with dbmod.session_scope() as s:
        a, b = sorted(s.exec(select(Clip)).all(), key=lambda c: c.id)
    p.approve_clip(a.id, ["youtube"])
    with dbmod.session_scope() as s:
        s.add(CreatorRule(platform_label="Twitch", handle="xqc", rule="block"))
    assert p.schedule_approved(now=NOW) == 0
    with dbmod.session_scope() as s:
        assert {c.status for c in s.exec(select(Clip)).all()} == {"rejected"}
        assert s.exec(select(Post)).all() == []
    import pytest

    with pytest.raises(ValueError, match="blockiert"):
        p.requeue_clip(a.id)


# --------------------------------------------------------------------------- Review


def test_music_unchecked_is_never_auto_approved(settings, db, monkeypatch):
    settings.config.publish.require_approval = False
    p, fakes = _setup(
        settings, monkeypatch, [make_cand("ClipA")], publishers={"youtube": FakePublisher("youtube")}
    )
    fakes.analysis_by_key["twitch_clip:ClipA"] = make_analysis("x", music_prob=None)
    assert p.process_cycle(now=NOW) == 1
    with dbmod.session_scope() as s:
        clip = s.exec(select(Clip)).one()
    assert clip.status == "pending_review" and clip.error == MUSIC_UNCHECKED
    settings.config.filters.music_policy = "allow"
    cid = p.add_candidate(make_cand("ClipA"), force=True)  # erster Clip liegt noch im Review
    clip_id = p.process_candidate(cid)
    with dbmod.session_scope() as s:
        assert s.get(Clip, clip_id).status == "approved"


def test_edited_description_keeps_credit_disclaimer_and_hashtags(settings, db, monkeypatch):
    p, fakes = _setup(
        settings, monkeypatch, [make_cand("ClipA")], publishers={"youtube": FakePublisher("youtube")}
    )
    p.process_cycle(now=NOW)
    with dbmod.session_scope() as s:
        clip = s.exec(select(Clip)).one()
        cand = s.get(Candidate, clip.candidate_id)
    from wdvi.pipeline import candidate_to_in

    credit = text.credit_line(candidate_to_in(cand))
    original = clip.descriptions["youtube"]
    assert credit in original and text.FAN_DISCLAIMER in original

    # 1) nur der Text oben geändert, Anhang (mit eigenem Hashtag) bleibt im Formular stehen
    edited = original.replace("What a jump", "my rewritten caption").replace("#gaming", "#gaming #wow")
    p.approve_clip(clip.id, ["youtube"], descriptions={"youtube": edited.replace("\n", "\r\n")})
    with dbmod.session_scope() as s:
        d = s.get(Clip, clip.id).descriptions["youtube"]
    assert d.startswith("my rewritten caption") and d.count(credit) == 1
    assert d.count(text.FAN_DISCLAIMER) == 1 and "#GTA6" in d and "#wow" in d and "\r" not in d

    # 2) alles gelöscht bis auf eigenen Text -> Credit, Hinweis und Hashtags kommen wieder dazu
    with dbmod.session_scope() as s:
        s.get(Clip, clip.id).status = "pending_review"
    p.approve_clip(clip.id, ["youtube"], descriptions={"youtube": "my rewritten caption"})
    with dbmod.session_scope() as s:
        d = s.get(Clip, clip.id).descriptions["youtube"]
    assert d.startswith("my rewritten caption\n\n") and d.count(credit) == 1
    assert d.count(text.FAN_DISCLAIMER) == 1 and "#GTA6" in d

    # 3) unverändert zurückgeschickt -> exakt gleich
    with dbmod.session_scope() as s:
        s.get(Clip, clip.id).status = "pending_review"
    p.approve_clip(clip.id, ["youtube"], descriptions={"youtube": d})
    with dbmod.session_scope() as s:
        assert s.get(Clip, clip.id).descriptions["youtube"] == d


# --------------------------------------------------------------------------- Statistik


def test_tiktok_inbox_drafts_get_metrics(settings, db, monkeypatch):
    MediaFakes(monkeypatch)
    tt = FakePublisher("tiktok", metrics=PostMetrics(views=321))
    p = make_pipeline(settings, publishers={"tiktok": tt})
    with dbmod.session_scope() as s:
        cand = candidate_from_in(make_cand("ClipA"), now=NOW, status="rendered")
        s.add(cand)
        s.flush()
        clip = Clip(candidate_id=cand.id, status="posted", video_path="x.mp4")
        s.add(clip)
        s.flush()
        s.add(
            Post(
                clip_id=clip.id,
                platform="tiktok",
                status="draft_sent",
                scheduled_at=NOW,
                title="t",
                description="d",
                remote_id="v_inbox_file~v2.123",
                posted_at=NOW,
            )
        )
    assert p.fetch_stats(now=NOW + timedelta(hours=25)) == 1
    with dbmod.session_scope() as s:
        assert [m.views for m in s.exec(select(Metric)).all()] == [321]


# --------------------------------------------------------------------------- Aufräumen / Speicherplatz


def _clip_with_files(settings, key: str, status: str, created, posts=()) -> int:
    clips = settings.config.paths.clips_dir
    clips.mkdir(parents=True, exist_ok=True)
    video, thumb = clips / f"{key}.mp4", clips / f"{key}.jpg"
    video.write_bytes(b"v" * 100)
    thumb.write_bytes(b"j")
    with dbmod.session_scope() as s:
        cand = candidate_from_in(make_cand(key), now=NOW, status="rendered")
        s.add(cand)
        s.flush()
        clip = Clip(
            candidate_id=cand.id,
            status=status,
            video_path=str(video),
            thumb_path=str(thumb),
            created_at=created,
            reviewed_at=created,
        )
        s.add(clip)
        s.flush()
        for platform, pstatus, when in posts:
            s.add(
                Post(
                    clip_id=clip.id,
                    platform=platform,
                    status=pstatus,
                    scheduled_at=when,
                    title="t",
                    description="d",
                    posted_at=when if pstatus in ("posted", "draft_sent") else None,
                    created_at=created,
                )
            )
        return int(clip.id)


def test_cleanup_deletes_old_posted_videos_and_expires_reviews(settings, db):
    p = make_pipeline(settings)
    old = NOW - timedelta(days=30)
    posted_old = _clip_with_files(settings, "PostedOld", "posted", old, [("youtube", "posted", old)])
    posted_new = _clip_with_files(
        settings, "PostedNew", "posted", old, [("youtube", "posted", NOW - timedelta(days=2))]
    )
    failed_old = _clip_with_files(settings, "FailedOld", "failed", old, [("tiktok", "failed", old)])
    waiting = _clip_with_files(
        settings, "Waiting", "scheduled", old, [("youtube", "posted", old), ("tiktok", "scheduled", NOW)]
    )
    pending_old = _clip_with_files(settings, "PendingOld", "pending_review", NOW - timedelta(days=8))
    pending_new = _clip_with_files(settings, "PendingNew", "pending_review", NOW - timedelta(days=1))
    p.cleanup(now=NOW)
    with dbmod.session_scope() as s:
        rows = {c.id: c for c in s.exec(select(Clip)).all()}
    for cid in (posted_old, failed_old):
        assert not Path(rows[cid].video_path).exists() and Path(rows[cid].thumb_path).exists()
        assert "Video gelöscht" in rows[cid].error
    for cid in (posted_new, waiting, pending_new):
        assert Path(rows[cid].video_path).exists()
    assert rows[pending_old].status == "rejected" and "nicht geprüft" in rows[pending_old].error
    assert rows[pending_new].status == "pending_review"
    assert Path(rows[pending_old].video_path).exists()  # wird erst nach weiteren 7 Tagen gelöscht
    p.cleanup(now=NOW + timedelta(days=8))
    with dbmod.session_scope() as s:
        assert not Path(s.get(Clip, pending_old).video_path).exists()


def test_cleanup_retention_can_be_disabled(settings, db):
    settings.config.cleanup.keep_posted_days = 0
    settings.config.cleanup.review_expire_days = 0
    p = make_pipeline(settings)
    old = NOW - timedelta(days=90)
    cid = _clip_with_files(settings, "PostedOld", "posted", old, [("youtube", "posted", old)])
    pid = _clip_with_files(settings, "PendingOld", "pending_review", old)
    p.cleanup(now=NOW)
    with dbmod.session_scope() as s:
        assert Path(s.get(Clip, cid).video_path).exists()
        assert s.get(Clip, pid).status == "pending_review"


def test_low_disk_space_stops_processing(settings, db, monkeypatch):
    p, fakes = _setup(settings, monkeypatch, [make_cand("ClipA")])
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda path: usage(100e9, 98e9, 2e9))
    assert p.process_cycle(now=NOW) == 0 and fakes.downloads == []
    run = _last_run("process")
    assert run.ok is False and "GB frei" in run.message
    assert _cand("twitch_clip:ClipA").status == "new"
    settings.config.cleanup.min_free_gb = 0
    assert p.process_cycle(now=NOW) == 1


def test_work_dir_removed_after_final_state(settings, db, monkeypatch):
    p, fakes = _setup(settings, monkeypatch, [make_cand("Done"), make_cand("Meh", views=10)])
    fakes.plan_by_key["twitch_clip:Meh"] = make_plan(keep=False, reject_reason="boring")
    p.process_cycle(now=NOW)
    work = settings.config.paths.work_dir
    assert not (work / "twitch_clip_Done").exists() and not (work / "twitch_clip_Meh").exists()
