"""Fakes für die Pipeline-Tests (keine Netzwerk-/GPU-/Whisper-Aufrufe). Enthält selbst keine Tests."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from wdvi.schemas import (
    CandidateIn,
    EditPlan,
    LayoutHint,
    MediaAnalysis,
    MediaInfo,
    PlatformText,
    PostMetrics,
    PostRequest,
    PostResult,
    Segment,
    SourceKind,
    Transcript,
    Word,
)

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def make_cand(
    source_id: str = "ClipA",
    *,
    source: SourceKind = SourceKind.twitch_clip,
    views: int = 5000,
    creator: str = "xQc",
    handle: str | None = "xqc",
    platform: str = "Twitch",
    age_h: float = 2.0,
    duration: float | None = 30.0,
    language: str | None = "en",
    extra: dict | None = None,
    **kw,
) -> CandidateIn:
    return CandidateIn(
        source=source,
        source_id=source_id,
        url=f"https://clips.twitch.tv/{source_id}",
        title=f"clip {source_id}",
        creator_name=creator,
        creator_handle=handle,
        creator_url=f"https://twitch.tv/{handle}" if handle else None,
        platform_label=platform,
        created_at=NOW - timedelta(hours=age_h),
        duration_s=duration,
        view_count=views,
        language=language,
        extra=extra if extra is not None else {"broadcaster_id": "1", "video_id": "", "vod_offset": None},
        **kw,
    )


def make_pipeline(settings, **kw):
    """Pipeline mit Fake-LLM/-Transcriber/-Detector (lädt keine Modelle)."""
    from wdvi.pipeline import Pipeline

    kw.setdefault("llm", FakeLLM())
    kw.setdefault("transcriber", object())
    kw.setdefault("detector", object())
    kw.setdefault("publishers", {})
    return Pipeline(settings, **kw)


class FakeSource:
    def __init__(
        self,
        name: str,
        candidates: list[CandidateIn],
        configured: bool = True,
        error: Exception | None = None,
    ):
        self.name = name
        self.candidates = candidates
        self.configured = configured
        self.error = error
        self.calls = 0

    def is_configured(self) -> bool:
        return self.configured

    def discover(self, now: datetime) -> list[CandidateIn]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return list(self.candidates)


class FakeLLM:
    """Wird nie wirklich aufgerufen (editor ist gepatcht) – nur damit die Pipeline 'bereit' ist."""

    def complete_json(self, *a, **kw):  # pragma: no cover
        raise AssertionError("LLM should not be called directly in tests")


class FakeRateLimitedLLM(FakeLLM):
    def __init__(self, remaining: int) -> None:
        class _RL:
            def __init__(self, n: int) -> None:
                self.n = n

            def remaining(self) -> int:
                return self.n

        self.rate_limiter = _RL(remaining)


@dataclass
class FakePublisher:
    platform: str
    results: list[PostResult] = field(default_factory=list)  # der Reihe nach; danach immer ok/posted
    connected: bool = True
    metrics: PostMetrics | None = None
    requests: list[PostRequest] = field(default_factory=list)

    def is_configured(self) -> bool:
        return True

    def is_connected(self) -> bool:
        return self.connected

    def auth_url(self, state: str) -> str:
        return f"https://auth.example/{self.platform}?state={state}"

    def handle_callback(self, params: dict[str, str]) -> None:
        pass

    def publish(self, req: PostRequest) -> PostResult:
        self.requests.append(req)
        if self.results:
            return self.results.pop(0)
        n = len(self.requests)
        return PostResult(
            ok=True,
            remote_id=f"{self.platform}-{n}",
            remote_url=f"https://x/{self.platform}/{n}",
            status="posted",
        )

    def fetch_metrics(self, remote_id: str) -> PostMetrics | None:
        return self.metrics


def make_plan(**kw) -> EditPlan:
    pt = PlatformText(
        youtube="He jumped the whole bridge", tiktok="bridge jump 💀", instagram="bridge jump 💀"
    )
    base = dict(
        keep=True,
        virality_score=88,
        reasoning="insane jump",
        trim_start=0.0,
        trim_end=20.0,
        hook_text="HE JUMPED THE BRIDGE",
        titles=pt,
        descriptions=PlatformText(youtube="What a jump", tiktok="jump", instagram="jump"),
        hashtags=["GTA6", "gaming"],
    )
    base.update(kw)
    return EditPlan(**base)


def make_analysis(
    path: str | Path,
    *,
    language: str = "en",
    speech_ratio: float = 0.5,
    music_prob: float | None = 0.05,
    phashes: list[str] | None = None,
    language_prob: float = 0.95,
) -> MediaAnalysis:
    words = [Word(text="no", start=0.5, end=0.8), Word(text="way", start=0.9, end=1.2)]
    tr = Transcript(
        language=language,
        language_prob=language_prob,
        segments=[Segment(text="no way", start=0.5, end=1.2, words=words)],
    )
    return MediaAnalysis(
        info=MediaInfo(path=str(path), duration=30.0, width=1920, height=1080, fps=30, has_audio=True),
        transcript=tr,
        music_prob=music_prob,
        speech_ratio=speech_ratio,
        layout=LayoutHint(kind="gameplay_fit", confidence=0.7),
        phashes=phashes if phashes is not None else ["ffd8c0a0b0e0f000"],
    )


class MediaFakes:
    """Patcht download/analyze/plan_clip/render_clip/next_slot im Pipeline-Modul-Namensraum."""

    def __init__(self, monkeypatch) -> None:
        from wdvi.brain import editor
        from wdvi.media import analyze as media_analyze
        from wdvi.media import download as media_download
        from wdvi.publish import slots
        from wdvi.render import renderer
        from wdvi.schemas import RenderResult

        self.downloads: list[str] = []
        self.analyses: list[str] = []
        self.plans: list[str] = []
        self.renders: list[str] = []
        self.analysis_by_key: dict[str, MediaAnalysis] = {}
        self.plan_by_key: dict[str, object] = {}  # EditPlan oder Exception
        self.download_errors: dict[str, Exception] = {}
        self.examples_seen: list[list] = []

        def download(c, dest_dir, settings):
            self.downloads.append(c.key)
            if c.key in self.download_errors:
                raise self.download_errors[c.key]
            p = Path(dest_dir) / "source.mp4"
            p.write_bytes(b"fake video")
            return p

        def analyze(path, work_dir, settings, transcriber, detector, language=None):
            key = Path(work_dir).name
            self.analyses.append(key)
            for k, a in self.analysis_by_key.items():
                if Path(work_dir).name == _safe(k):
                    return a.model_copy(update={"info": a.info.model_copy(update={"path": str(path)})})
            return make_analysis(path, phashes=[hashlib.md5(key.encode()).hexdigest()[:16]])

        def plan_clip(llm, c, analysis, examples, settings):
            self.plans.append(c.key)
            self.examples_seen.append(list(examples))
            got = self.plan_by_key.get(c.key)
            if isinstance(got, Exception):
                raise got
            return got if got is not None else make_plan()

        def render_clip(source, analysis, plan, c, settings, work_dir, out_dir):
            self.renders.append(c.key)
            out_dir = Path(out_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            video = out_dir / f"{_safe(c.key)}.mp4"
            thumb = out_dir / f"{_safe(c.key)}.jpg"
            video.write_bytes(b"rendered")
            thumb.write_bytes(b"jpg")
            return RenderResult(
                video_path=str(video),
                thumb_path=str(thumb),
                duration=20.0,
                width=1080,
                height=1920,
                encoder="libx264",
            )

        def next_slot(platform, taken, now, settings):
            # deterministisch: volle Stunde nach now + 1 h, dann je belegtem Slot +2 h
            base = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            t = base
            while any(abs((t - x).total_seconds()) < 3600 for x in taken):
                t += timedelta(hours=2)
            return t

        monkeypatch.setattr(media_download, "download", download)
        monkeypatch.setattr(media_analyze, "analyze", analyze)
        monkeypatch.setattr(editor, "plan_clip", plan_clip)
        monkeypatch.setattr(renderer, "render_clip", render_clip)
        monkeypatch.setattr(slots, "next_slot", next_slot)


def _safe(key: str) -> str:
    from wdvi.pipeline import safe_name

    return safe_name(key)
