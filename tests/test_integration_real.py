"""Echte Integration pipeline <-> media <-> brain <-> render <-> publish.text.

Echt: media.analyze (probe, Frames, Haar-Gesichter, Layout, Lautstärke, pHash), media.download (nur
yt-dlp selbst ist gefälscht und "lädt" synthetische Videos), brain.editor (Prompt + sanitize_plan,
nur das LLM ist gefälscht), render.renderer (ffmpeg/libx264, Captions, Emojis, SFX), publish.text +
publish.slots, YouTubePublisher._body/publish (mit Fake-API-Service).
Gefälscht: Quellen, Whisper (fester Text, 0-basierte Zeiten der übergebenen Datei), LLM, Netzwerk.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from sqlmodel import select

from wdvi import db as dbmod
from wdvi.dashboard import _data as dash_data
from wdvi.media import download as media_download
from wdvi.media.faces import FaceDetector
from wdvi.media.probe import probe
from wdvi.models import Candidate, Clip, Post
from wdvi.pipeline import Pipeline
from wdvi.publish import text
from wdvi.publish.youtube import YouTubePublisher
from wdvi.render import encoder, overlays
from wdvi.schemas import (
    CandidateIn,
    EditPlan,
    Emphasis,
    MomentPick,
    MomentPicks,
    PlatformText,
    PostRequest,
    PostResult,
    Segment,
    SourceKind,
    Transcript,
    Word,
    Zoom,
)
from wdvi.sources import youtube as yt_source
from wdvi.sources.youtube import HeatPoint, VideoContext

pytestmark = pytest.mark.slow

FACE_JPG = Path(__file__).parent / "media_fixtures" / "face.jpg"
SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 36 36">'
    b'<circle cx="18" cy="18" r="16" fill="#FFCC4D"/></svg>'
)

TWITCH_SLUG = "BraveTinyOtterKappa"
TWITCH_URL = f"https://clips.twitch.tv/{TWITCH_SLUG}"
REDDIT_LINKED = "https://www.twitch.tv/shroud/clip/FunnyBigLlamaPogChamp"
REDDIT_ID = "1gx9abc"
YT_ID = "dQw4w9WgXcQ"
LONG_YT_URL = "https://www.youtube.com/watch?v=LongVid1234"
MOMENT = (200.0, 225.0)  # Ausschnitt im langen YouTube-Video (Elternzeit)

WORDS = (
    "no way he just jumped the whole bridge bro this is insane look at that crash we are so back chat what"
).split()


# --------------------------------------------------------------------------- Testvideos


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], check=True)


def _audio_args(seconds: float) -> list[str]:
    return [
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=330:sample_rate=48000:duration={seconds},"
        "volume='if(between(mod(t,5),3,3.5),1.0,0.2)':eval=frame",
    ]


def make_facecam_stream(out: Path, seconds: float = 12.0) -> Path:
    """Stream-Look: Gameplay (testsrc2) + Webcam-Gesicht unten links + Ton."""
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size=1280x720:rate=30:duration={seconds}",
        "-loop",
        "1",
        "-i",
        str(FACE_JPG),
        *_audio_args(seconds),
        "-filter_complex",
        "[1:v]scale=320:-2[cam];[0:v][cam]overlay=0:H-h:shortest=1[v]",
        "-map",
        "[v]",
        "-map",
        "2:a",
        "-t",
        str(seconds),
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
        str(out),
    )
    return out


def make_plain(out: Path, src: str, seconds: float) -> Path:
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        f"{src}:duration={seconds}",
        *_audio_args(seconds),
        "-t",
        str(seconds),
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
        str(out),
    )
    return out


@pytest.fixture(scope="module")
def videos(tmp_path_factory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("integration_media")
    return {
        "twitch": make_facecam_stream(d / "twitch.mp4", 12.0),
        "reddit": make_plain(d / "reddit.mp4", "testsrc=size=1280x720:rate=30", 10.0),
    }


# --------------------------------------------------------------------------- Fakes


class FakeYoutubeDL:
    """Ersetzt yt_dlp.YoutubeDL in media.download: "lädt" je nach URL ein synthetisches Video.

    youtube_moment: wie echtes yt-dlp mit download_ranges -> nur der Abschnitt, Zeitbasis beginnt bei 0.
    """

    calls: list[dict] = []
    videos: dict[str, Path] = {}
    fail_with: Exception | None = None

    def __init__(self, opts: dict) -> None:
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None

    def extract_info(self, url: str, download: bool = True) -> dict:
        out = Path(self.opts["outtmpl"].replace("%(ext)s", "mp4"))
        ranges = None
        if self.opts.get("download_ranges") is not None:
            ranges = list(self.opts["download_ranges"]({"duration": 600.0, "id": YT_ID}, self))
        FakeYoutubeDL.calls.append({"url": url, "ranges": ranges, "out": out})
        if ranges:
            (sec,) = ranges
            seconds = float(sec["end_time"]) - float(sec["start_time"])
            make_plain(out, "gradients=size=1280x720:rate=30:speed=0.02", seconds)
        elif FakeYoutubeDL.fail_with is not None:
            raise FakeYoutubeDL.fail_with
        elif url == LONG_YT_URL:  # langes Video ohne Abschnitt (Reddit-Link auf ein YouTube-Video)
            make_plain(out, "gradients=size=640x360:rate=30:speed=0.02", 30.0)
        elif "reddit" in url or url == REDDIT_LINKED:
            out.write_bytes(self.videos["reddit"].read_bytes())
        else:
            out.write_bytes(self.videos["twitch"].read_bytes())
        return {"id": "x", "ext": "mp4", "requested_downloads": [{"filepath": str(out)}]}

    def to_screen(self, *a, **k) -> None:  # von download_range_func benutzt
        pass


class FakeTranscriber:
    """Whisper-Ersatz: Wörter gleichmäßig über die tatsächlich übergebene Datei (0-basiert)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def transcribe(self, path, language: str | None = None) -> Transcript:
        self.calls.append((str(path), language))
        dur = probe(path).duration
        words, t, i = [], 0.6, 0
        while t + 0.35 < dur - 0.8:
            words.append(
                Word(text=WORDS[i % len(WORDS)], start=round(t, 2), end=round(t + 0.32, 2), prob=0.93)
            )
            t += 0.42
            i += 1
        segs = [
            Segment(
                text=" ".join(w.text for w in words), start=words[0].start, end=words[-1].end, words=words
            )
        ]
        return Transcript(language="en", language_prob=0.97, segments=segs)


_WORD_LINE = re.compile(r"^\[(\d+\.\d+)-(\d+\.\d+)\] (\S+?)\??$", re.M)


class FakeLLM:
    """Gibt einen realistischen EditPlan (aus den Prompt-Daten) bzw. MomentPicks zurück."""

    def __init__(self) -> None:
        self.prompts: list[tuple[str, str, list[dict]]] = []
        self.images_present: list[
            bool
        ] = []  # Standbilder existierten beim Aufruf (Arbeitsordner wird danach aufgeräumt)

    def complete_json(self, system, content, schema_model, max_tokens=16000):
        prompt = "\n".join(b["text"] for b in content if b.get("type") == "text")
        self.prompts.append((schema_model.__name__, prompt, content))
        self.images_present += [Path(b["path"]).is_file() for b in content if b.get("type") == "image"]
        if schema_model is MomentPicks:
            return MomentPicks(
                moments=[
                    MomentPick(start=MOMENT[0], end=MOMENT[1], score=91, why="huge crash, chat explodes")
                ]
            )
        assert schema_model is EditPlan
        dur = float(re.search(r"Source clip duration: ([\d.]+) s", prompt).group(1))
        words = [(float(a), float(b), w) for a, b, w in _WORD_LINE.findall(prompt)]
        assert words, "prompt contains no word-level transcript"
        by_text = {}
        for s, _e, w in words:
            by_text.setdefault(w.lower(), s)
        cut_a = round(dur * 0.7, 2)
        return EditPlan(
            keep=True,
            virality_score=91,
            reasoning="Massive bridge jump with an instant crash and a loud reaction.",
            content_language="en",
            trim_start=0.3,
            trim_end=round(dur - 0.4, 2),
            remove_ranges=[(cut_a, cut_a + 0.6)],
            layout=None,
            hook_text="HE JUMPED THE WHOLE BRIDGE 💀",
            highlight_words=["insane", "crash", "bridge"],
            zooms=[
                Zoom(start=1.5, end=3.0, scale=1.25, focus="face"),
                Zoom(start=round(dur * 0.45, 2), end=round(dur * 0.45 + 1.5, 2), scale=1.3, focus="gameplay"),
            ],
            emphasis=[
                Emphasis(t=by_text["bridge"], emoji="💀", sfx="boom"),
                Emphasis(t=by_text["insane"], emoji="😳", sfx="whoosh"),
                Emphasis(t=by_text["crash"], sfx="ding"),
            ],
            titles=PlatformText(
                youtube="He jumped the WHOLE bridge 💀 #shorts",
                tiktok="bridge jump 💀",
                instagram="bridge jump 💀",
            ),
            descriptions=PlatformText(
                youtube="No way he made that jump.\n\n#gta6 #gaming",
                tiktok="insane jump 💀 #gta6 #fyp",
                instagram="insane jump 💀",
            ),
            hashtags=["GTA6", "gaming", "GTAVI"],
        )


class RecordingPublisher:
    """Fake-Publisher für TikTok/Instagram: rechnet die Caption wie die echten Publisher."""

    def __init__(self, platform: str, status: str = "posted") -> None:
        self.platform = platform
        self.status = status
        self.requests: list[PostRequest] = []
        self.captions: list[str] = []

    def is_configured(self) -> bool:
        return True

    def is_connected(self) -> bool:
        return True

    def publish(self, req: PostRequest) -> PostResult:
        self.requests.append(req)
        self.captions.append(text.build_caption(self.platform, req.title, req.description))
        n = len(self.requests)
        return PostResult(ok=True, remote_id=f"{self.platform}-{n}", status=self.status)

    def fetch_metrics(self, remote_id):
        return None


class _FakeInsert:
    def __init__(self, body, media_body, sink):
        self.body, self.media_body, self.sink = body, media_body, sink

    def next_chunk(self):
        self.sink.append(self.body)
        st = self.body["status"]
        return None, {
            "id": f"yt{len(self.sink)}",
            "status": {"uploadStatus": "uploaded", "privacyStatus": st["privacyStatus"]},
        }


class _FakeList:
    def __init__(self, service, video_id):
        self.service, self.video_id = service, video_id

    def execute(self):
        self.service.status_checks.append(self.video_id)
        return {"items": [{"id": self.video_id, "status": {"privacyStatus": self.service.visibility}}]}


class _FakeVideos:
    def __init__(self, service):
        self.service = service
        self.sink = service.bodies

    def insert(self, part, body, media_body):
        assert Path(media_body._filename).is_file()
        return _FakeInsert(body, media_body, self.sink)

    def list(self, part, id):  # noqa: A002 - Name wie in der YouTube-API
        return _FakeList(self.service, id)


class FakeYTService:
    def __init__(self) -> None:
        self.bodies: list[dict] = []
        self.visibility = "public"  # was videos.list nach publishAt meldet (check_status)
        self.status_checks: list[str] = []

    def videos(self):
        return _FakeVideos(self)


class ConnectedYouTube(YouTubePublisher):
    """Echter YouTubePublisher (Body-Aufbau, Upload-Schleife) mit Fake-API-Service."""

    def is_connected(self) -> bool:
        return True


# --------------------------------------------------------------------------- Fixtures


@pytest.fixture(autouse=True)
def _no_network():
    overlays._failed.clear()
    with respx.mock(assert_all_called=False) as mock:  # alles andere: Fehler statt echtes Netz
        mock.get(url__regex=r"https://raw\.githubusercontent\.com/.*").mock(
            return_value=httpx.Response(200, content=SVG)
        )
        yield
    encoder.reset_cache()


@pytest.fixture
def env(settings, db, videos, monkeypatch, tmp_path):
    # Arbeitsverzeichnis wie im Docker (relative data_dir) ist hier absolut über tmp_path
    settings.config.publish.tiktok.enabled = True
    settings.config.publish.instagram.enabled = True
    settings.config.llm.max_frames = 4
    FakeYoutubeDL.calls = []
    FakeYoutubeDL.videos = videos
    FakeYoutubeDL.fail_with = None
    monkeypatch.setattr(media_download.yt_dlp, "YoutubeDL", FakeYoutubeDL)

    yt_service = FakeYTService()
    yt = ConnectedYouTube(settings, http=yt_service)
    tt = RecordingPublisher("tiktok", status="draft_sent")
    ig = RecordingPublisher("instagram")
    transcriber = FakeTranscriber()
    llm = FakeLLM()
    p = Pipeline(
        settings,
        llm=llm,
        sources=[],
        publishers={"youtube": yt, "tiktok": tt, "instagram": ig},
        transcriber=transcriber,
        detector=FaceDetector(settings, allow_download=False),
    )
    return {"p": p, "llm": llm, "tr": transcriber, "yt": yt_service, "tt": tt, "ig": ig}


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def twitch_candidate(now: datetime) -> CandidateIn:
    return CandidateIn(
        source=SourceKind.twitch_clip,
        source_id=TWITCH_SLUG,
        url=TWITCH_URL,
        title="no way bro",
        creator_name="xQc",
        creator_handle="xqc",
        creator_url="https://twitch.tv/xqc",
        platform_label="Twitch",
        created_at=now - timedelta(hours=3),
        duration_s=12.0,
        view_count=25000,
        language="en",
        extra={"broadcaster_id": "71092938", "video_id": "", "vod_offset": None, "is_featured": True},
    )


def reddit_linked_candidate(now: datetime) -> CandidateIn:
    """So, wie RedditSource._to_candidate einen Link-Post auf einen Twitch-Clip abbildet: Credit an den
    ORIGINAL-Creator (Kanal aus der Clip-URL), der Reddit-Poster steht in extra.via."""
    return CandidateIn(
        source=SourceKind.reddit_post,
        source_id=REDDIT_ID,
        url=f"https://www.reddit.com/r/GTA6/comments/{REDDIT_ID}/insane_bridge_jump/",
        title="This bridge jump is insane",
        creator_name="shroud",
        creator_handle="shroud",
        creator_url="https://twitch.tv/shroud",
        platform_label="Twitch",
        created_at=now - timedelta(hours=4),
        duration_s=None,
        engagement={"score": 2400.0, "num_comments": 310.0, "upvote_ratio": 0.97},
        extra={
            "subreddit": "GTA6",
            "permalink": "https://www.reddit.com/r/GTA6/comments/x/",
            "domain": "twitch.tv",
            "author": "clipposter",
            "linked_url": REDDIT_LINKED,
            "linked_platform": "Twitch",
            "via": "u/clipposter",
            "via_url": "https://reddit.com/u/clipposter",
        },
    )


def test_reddit_fixture_matches_reddit_source_mapping():
    """Die Fixture oben muss genau dem entsprechen, was sources/reddit.py aus dem Link-Post macht."""
    from wdvi.sources.reddit import credit_linked_creator

    now = _now()
    fixture = reddit_linked_candidate(now)
    raw = fixture.model_copy(
        update={
            "creator_name": "u/clipposter",
            "creator_handle": "clipposter",
            "creator_url": "https://reddit.com/u/clipposter",
            "platform_label": "Reddit",
            "extra": {k: v for k, v in fixture.extra.items() if k not in ("via", "via_url")},
        }
    )
    assert credit_linked_creator(raw) == fixture


def youtube_video_candidate(now: datetime) -> CandidateIn:
    return CandidateIn(
        source=SourceKind.youtube_video,
        source_id=YT_ID,
        url=f"https://www.youtube.com/watch?v={YT_ID}",
        title="GTA 6 FIRST 4 HOURS",
        creator_name="GTA Guy",
        creator_handle="gtaguy",
        creator_url="https://www.youtube.com/@gtaguy",
        platform_label="YouTube",
        created_at=now - timedelta(hours=5),
        duration_s=600.0,
        view_count=90000,
        language="en",
        extra={"channel_id": "UCabcdefghijklmnop"},
    )


def _video_context(url: str, settings) -> VideoContext:
    segs = []
    t = 0.0
    while t < 600:
        segs.append(Segment(text=f"part at {int(t)} seconds what is going on", start=t, end=t + 5.0))
        t += 5.0
    return VideoContext(
        video_id=YT_ID,
        title="GTA 6 FIRST 4 HOURS",
        channel="GTA Guy",
        duration=600.0,
        heatmap=[HeatPoint(start=195.0, end=230.0, value=1.0)],
        transcript=Transcript(language="en", segments=segs),
        view_count=90000,
    )


def _probe_json(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
        capture_output=True,
        check=True,
    ).stdout
    return json.loads(out)


def _clip_for(key: str) -> tuple[Candidate, Clip | None]:
    with dbmod.session_scope() as s:
        cand = s.exec(select(Candidate).where(Candidate.key == key)).one()
        clip = s.exec(select(Clip).where(Clip.candidate_id == cand.id)).first()
        return cand, clip


def _assert_rendered(clip: Clip, settings, expect_dur: float) -> None:
    clips_dir = settings.config.paths.clips_dir.resolve()
    video, thumb = Path(clip.video_path), Path(clip.thumb_path)
    assert video.is_file() and thumb.is_file()
    assert clips_dir in video.resolve().parents, f"{video} not inside {clips_dir}"
    assert clips_dir in thumb.resolve().parents
    info = _probe_json(video)
    vs = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (vs["width"], vs["height"]) == (1080, 1920)
    assert any(s["codec_type"] == "audio" for s in info["streams"])
    assert abs(float(info["format"]["duration"]) - expect_dur) < 0.6, (info["format"]["duration"], expect_dur)
    assert abs(clip.duration - expect_dur) < 0.6
    # das Dashboard liefert nur Dateien aus clips_dir aus
    assert dash_data.media_url(clip.video_path, settings.config.paths.clips_dir).startswith("/media/clips/")
    assert dash_data.media_url(clip.thumb_path, settings.config.paths.clips_dir).startswith("/media/clips/")


def _count(haystack: str, needle: str) -> int:
    return haystack.lower().count(needle.lower())


def _assert_texts_once(titles: dict, descriptions: dict, credit: str) -> None:
    assert set(titles) == {"youtube", "tiktok", "instagram"}
    assert _count(titles["youtube"], "#shorts") == 1, titles["youtube"]
    assert titles["youtube"].startswith("He jumped the WHOLE bridge")
    for p in ("tiktok", "instagram"):
        assert titles[p] == "bridge jump 💀"
    for p, d in descriptions.items():
        assert d.count(credit) == 1, (p, d)
        assert d.count(text.FAN_DISCLAIMER) == 1, (p, d)
        assert _count(d, "#gta6") == 1, (p, d)
        assert "#shorts" not in d.lower()
    assert descriptions["youtube"].startswith("No way he made that jump.")
    assert _count(descriptions["youtube"], "#gaming") == 1
    assert _count(descriptions["tiktok"], "#fyp") == 1


# --------------------------------------------------------------------------- Tests


def test_twitch_clip_end_to_end_real_media_render_text(env, settings):
    p, llm, tr = env["p"], env["llm"], env["tr"]
    now = _now()
    cand = twitch_candidate(now)
    assert p.upsert_candidates([cand], now) == (1, 0, 0)

    assert p.process_cycle(now=now) == 1
    row, clip = _clip_for(cand.key)
    assert row.status == "rendered", row.status_reason
    assert clip is not None and clip.status == "pending_review"

    # Download: Clip-URL, kein Abschnitt; Datei liegt im Arbeitsordner
    (call,) = FakeYoutubeDL.calls
    assert call["url"] == TWITCH_URL and call["ranges"] is None
    assert settings.config.paths.work_dir.resolve() in call["out"].resolve().parents
    # Whisper-Sprache wird nicht erzwungen (sonst wäre der Sprachfilter wirkungslos)
    assert [lang for _, lang in tr.calls] == [None]

    # echte Analyse: Facecam unten links erkannt, Frames für Claude existieren
    assert clip.analysis["layout"] == "facecam_split"
    assert clip.analysis["language"] == "en" and clip.analysis["duration"] == pytest.approx(12.0, abs=0.1)
    (kind, prompt, blocks) = llm.prompts[0]
    assert kind == "EditPlan"
    imgs = [b for b in blocks if b.get("type") == "image"]
    assert 1 <= len(imgs) <= 4 and llm.images_present == [True] * len(imgs)
    # Arbeitsordner (Download, Frames) ist nach dem Rendern aufgeräumt, das fertige Video bleibt
    assert not call["out"].exists()
    assert "facecam_split" in prompt and "Source clip duration: 12.00 s" in prompt

    # Plan: Zeiten in Quellzeit, sanitize lässt den realistischen Plan intakt
    plan = EditPlan.model_validate(clip.plan)
    assert plan.trim_start == 0.3 and plan.trim_end == pytest.approx(11.6)
    assert plan.remove_ranges == [(8.4, 9.0)]
    assert len(plan.zooms) == 2 and plan.zooms[0].focus == "face"
    assert [e.emoji for e in plan.emphasis if e.emoji] == ["💀", "😳"]
    assert [e.sfx for e in plan.emphasis] == ["boom", "whoosh", "ding"]
    assert plan.hashtags[0].lower() == "gta6" and len({h.lower() for h in plan.hashtags}) == len(
        plan.hashtags
    )

    # echtes Render: 1080x1920 in clips_dir, Dauer = trim minus Schnitt
    _assert_rendered(clip, settings, expect_dur=(11.6 - 0.3) - 0.6)

    # Texte genau einmal komponiert
    credit = text.credit_line(cand)
    assert credit == "🎥 Clip: xQc on Twitch – twitch.tv/xqc"
    _assert_texts_once(clip.titles, clip.descriptions, credit)
    assert clip.platforms == ["youtube", "tiktok", "instagram"]

    # Review: Dashboard schickt die (unveränderten) vorbefüllten Texte mit
    items, total = dash_data.review_items(
        ["youtube", "tiktok", "instagram"], settings.config.paths.clips_dir, 0, 10
    )
    assert total == 1 and items[0]["video_url"]
    form_titles = {t["platform"]: t["title"] for t in items[0]["texts"]}
    form_descs = {t["platform"]: t["description"] for t in items[0]["texts"]}
    p.approve_clip(clip.id, ["youtube", "tiktok", "instagram"], form_titles, form_descs)
    with dbmod.session_scope() as s:
        c = s.get(Clip, clip.id)
        assert c.status == "approved"
        _assert_texts_once(c.titles, c.descriptions, credit)

    # Slots + Posten
    assert p.schedule_approved(now=now) == 3
    with dbmod.session_scope() as s:
        posts = s.exec(select(Post).where(Post.clip_id == clip.id)).all()
        assert {x.platform for x in posts} == {"youtube", "tiktok", "instagram"}
        assert all(x.status == "scheduled" and _aware(x.scheduled_at) > now for x in posts)
        last_slot = max(_aware(x.scheduled_at) for x in posts)
    # YouTube native_schedule: Vorab-Upload als private + publishAt
    assert p.publish_due(now=now) >= 0
    assert p.publish_due(now=last_slot + timedelta(minutes=1)) >= 1
    # YouTube veröffentlicht selbst zum Termin; 10–30 min danach prüft die Pipeline, ob es wirklich öffentlich ist
    p.publish_due(now=last_slot + timedelta(minutes=11))
    assert env["yt"].status_checks == ["yt1"]
    with dbmod.session_scope() as s:
        posts = {x.platform: x for x in s.exec(select(Post).where(Post.clip_id == clip.id)).all()}
        assert posts["youtube"].status == "posted" and posts["youtube"].remote_id == "yt1"
        assert posts["tiktok"].status == "draft_sent"
        assert posts["instagram"].status == "posted"
        assert s.get(Clip, clip.id).status == "posted"

    # was die (echten) Publisher daraus machen: nichts doppelt
    (body,) = env["yt"].bodies
    assert body["snippet"]["title"] == clip.titles["youtube"]
    assert body["snippet"]["description"] == clip.descriptions["youtube"]
    assert body["snippet"]["tags"][0] == "GTA6"
    for pub in (env["tt"], env["ig"]):
        (req,) = pub.requests
        assert Path(req.video_path) == Path(clip.video_path) and req.publish_at is None
        (cap,) = pub.captions
        assert cap.count("bridge jump 💀") == 1 and cap.count(credit) == 1 and _count(cap, "#gta6") == 1
        assert cap.count(text.FAN_DISCLAIMER) == 1


def test_youtube_moment_time_base(env, settings, monkeypatch):
    """Langes Video -> Moment 200–225 s -> Download nur des Abschnitts (beginnt bei 0) -> Analyse,
    Transkript, Plan und Render arbeiten alle in der 0-basierten Zeit des Abschnitts."""
    p, llm, tr = env["p"], env["llm"], env["tr"]
    monkeypatch.setattr(yt_source, "fetch_video_context", _video_context)
    now = _now()
    parent = youtube_video_candidate(now)
    assert p.upsert_candidates([parent], now) == (1, 0, 0)

    assert p.process_cycle(now=now) == 0  # Eltern-Video wird nur in Momente zerlegt
    with dbmod.session_scope() as s:
        prow = s.exec(select(Candidate).where(Candidate.key == parent.key)).one()
        assert prow.status == "mined", prow.status_reason
        moment = s.exec(select(Candidate).where(Candidate.parent_key == parent.key)).one()
        assert (moment.source, moment.start_s, moment.end_s) == ("youtube_moment", *MOMENT)
        assert moment.duration_s == pytest.approx(25.0)
        mkey = moment.key

    assert p.process_cycle(now=now) == 1
    row, clip = _clip_for(mkey)
    assert row.status == "rendered", row.status_reason

    (call,) = FakeYoutubeDL.calls
    assert call["url"] == parent.url
    assert call["ranges"] == [{"start_time": MOMENT[0], "end_time": MOMENT[1]}]
    assert [lang for _, lang in tr.calls] == [None]

    prompt = next(pr for kind, pr, _ in llm.prompts if kind == "EditPlan")
    assert "Source: youtube_moment" in prompt and "relative to this clip" in prompt
    assert "Source clip duration: 25.00 s" in prompt and "within 0.00-25.00" in prompt
    assert clip.analysis["duration"] == pytest.approx(25.0, abs=0.1)
    plan = EditPlan.model_validate(clip.plan)
    assert plan.trim_start == 0.3 and plan.trim_end == pytest.approx(24.6)
    assert plan.remove_ranges == [(17.5, 18.1)]
    _assert_rendered(clip, settings, expect_dur=(24.6 - 0.3) - 0.6)
    credit = text.credit_line(_cand_in(row))
    assert credit.startswith("🎥 Clip: GTA Guy on YouTube")
    for d in clip.descriptions.values():
        assert d.count(credit) == 1


def _cand_in(row: Candidate) -> CandidateIn:
    from wdvi.pipeline import candidate_to_in

    return candidate_to_in(row)


def test_reddit_post_linking_twitch_clip(env, settings):
    """Reddit-Link-Post auf einen Twitch-Clip: Download vom verlinkten Clip, Credit an den Original-Creator."""
    p = env["p"]
    now = _now()
    cand = reddit_linked_candidate(now)
    p.upsert_candidates([cand], now)
    assert p.process_cycle(now=now) == 1
    row, clip = _clip_for(cand.key)
    assert row.status == "rendered", row.status_reason
    (call,) = FakeYoutubeDL.calls
    assert call["url"] == REDDIT_LINKED  # nicht die Reddit-Seite
    assert clip.analysis["layout"] == "gameplay_fit"
    _assert_rendered(clip, settings, expect_dur=(9.6 - 0.3) - 0.6)
    # Credit: der Clip stammt von Twitch (shroud) – nur "u/clipposter on Reddit" wäre falsch gutgeschrieben;
    # der Reddit-Poster wird als Quelle zusätzlich genannt
    expected = "🎥 Clip: shroud on Twitch – twitch.tv/shroud (via u/clipposter on Reddit)"
    assert text.credit_line(_cand_in(row)) == expected
    for p_, d in clip.descriptions.items():
        assert d.count(expected) == 1, (p_, d)


def test_reprocessing_a_candidate_does_not_overwrite_existing_clip(env, settings):
    """process-url auf einen schon gerenderten Clip (add_candidate setzt ihn auf 'new') darf das MP4 eines
    bestehenden (evtl. schon eingeplanten) Clips nicht überschreiben."""
    p = env["p"]
    now = _now()
    cand = twitch_candidate(now)
    p.upsert_candidates([cand], now)
    assert p.process_cycle(now=now) == 1
    _, first = _clip_for(cand.key)
    p.approve_clip(first.id, ["tiktok"])
    assert p.schedule_approved(now=now) == 1
    first_mtime = Path(first.video_path).stat().st_mtime_ns

    # wie `wdvi process-url --force <clip>` (ohne --force verweigert, weil der Clip schon eingeplant ist)
    with pytest.raises(ValueError, match="--force"):
        p.add_candidate(cand)
    cid = p.add_candidate(cand, force=True)
    assert p.process_candidate(cid) is not None
    with dbmod.session_scope() as s:
        clips = s.exec(select(Clip).where(Clip.candidate_id == cid).order_by(Clip.id)).all()
    assert len(clips) == 2
    assert clips[0].video_path != clips[1].video_path, "second render reuses the scheduled clip's file"
    assert Path(first.video_path).stat().st_mtime_ns == first_mtime


def test_reddit_link_to_long_video_is_length_checked_after_download(env, settings):
    """Reddit-Link-Posts haben vor dem Download keine Dauer (duration_s=None) -> der Längenfilter
    (filters.max_source_duration_s) muss nach dem Download greifen, statt ein beliebig langes fremdes
    Video komplett zu laden, zu transkribieren und daraus einen Clip zu schneiden."""
    p = env["p"]
    settings.config.filters.max_source_duration_s = 20.0
    now = _now()
    cand = reddit_linked_candidate(now).model_copy(
        update={"extra": {"author": "clipposter", "linked_url": LONG_YT_URL, "linked_platform": "YouTube"}}
    )
    p.upsert_candidates([cand], now)
    p.process_cycle(now=now)
    row, clip = _clip_for(cand.key)
    (call,) = FakeYoutubeDL.calls
    assert call["url"] == LONG_YT_URL
    assert clip is None, f"30 s source > max_source_duration_s=20 was rendered ({clip.video_path})"
    assert row.status in ("skipped", "rejected"), (row.status, row.status_reason)


def test_transient_download_error_keeps_candidate_for_retry(env, settings):
    """Netz weg / DNS-Fehler beim Download ist kein Fehler des Clips: Kandidat darf nicht dauerhaft
    'failed' werden (sonst verbrennt ein Internet-Ausfall die ganze Warteschlange, 3 pro Pause)."""
    import yt_dlp.utils

    p = env["p"]
    FakeYoutubeDL.fail_with = yt_dlp.utils.DownloadError(
        "ERROR: [twitch:clips] BraveTinyOtterKappa: Unable to download JSON metadata: <urlopen error "
        "[Errno -3] Temporary failure in name resolution>"
    )
    now = _now()
    cand = twitch_candidate(now)
    p.upsert_candidates([cand], now)
    p.process_cycle(now=now)
    row, clip = _clip_for(cand.key)
    assert clip is None
    assert row.status == "new", (row.status, row.status_reason)
