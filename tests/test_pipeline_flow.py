"""Pipeline End-to-End mit Fakes: discover -> process -> Review -> Slots -> Posten -> Stats."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlmodel import select

from tests.test_pipeline_support import (
    NOW,
    FakePublisher,
    FakeSource,
    MediaFakes,
    make_cand,
    make_pipeline,
)
from wdvi import db as dbmod
from wdvi.models import Candidate, Clip, JobRun, Metric, Post
from wdvi.pipeline import candidate_from_in, candidate_to_in
from wdvi.schemas import MomentPick, PostMetrics, PostResult, SourceKind


def _all(model):
    with dbmod.session_scope() as s:
        return s.exec(select(model)).all()


def _cand(key: str) -> Candidate:
    with dbmod.session_scope() as s:
        return s.exec(select(Candidate).where(Candidate.key == key)).one()


def test_candidate_roundtrip(db, candidate):
    row = candidate_from_in(candidate, prescore=1.5, now=NOW)
    back = candidate_to_in(row)
    assert back.key == candidate.key and back.extra == candidate.extra
    assert row.prescore == 1.5 and row.status == "new" and row.source == "twitch_clip"


def test_end_to_end(settings, db, monkeypatch):
    settings.config.publish.tiktok.enabled = True
    fakes = MediaFakes(monkeypatch)
    src = FakeSource(
        "twitch",
        [
            make_cand("ClipA", views=20000),
            make_cand("ClipB", views=800, creator="Other", handle="other", extra={"broadcaster_id": "2"}),
        ],
    )
    yt = FakePublisher(
        "youtube",
        results=[
            PostResult(
                ok=True, remote_id="yt1", remote_url="https://youtube.com/shorts/yt1", status="scheduled"
            )
        ],
        metrics=PostMetrics(views=1234, likes=99),
    )
    tt = FakePublisher("tiktok", results=[PostResult(ok=True, remote_id="pub1", status="draft_sent")])
    p = make_pipeline(settings, sources=[src], publishers={"youtube": yt, "tiktok": tt})

    # discover
    assert p.discover(now=NOW) == 2
    a = _cand("twitch_clip:ClipA")
    assert a.status == "new" and a.prescore > _cand("twitch_clip:ClipB").prescore

    # process
    assert p.process_cycle(now=NOW) == 2
    assert fakes.plans[0] == "twitch_clip:ClipA"  # bester Prescore zuerst
    clips = _all(Clip)
    assert len(clips) == 2
    clip = next(c for c in clips if c.candidate_id == a.id)
    assert clip.status == "pending_review"
    assert clip.virality_score == 88 and clip.plan["hook_text"] == "HE JUMPED THE BRIDGE"
    assert clip.platforms == ["youtube", "tiktok"] and set(clip.titles) == {"youtube", "tiktok"}
    assert "xQc" in clip.descriptions["youtube"]  # Credit hängt der Code an
    assert clip.analysis["layout"] == "gameplay_fit" and clip.analysis["language"] == "en"
    assert len(clip.phashes) == 1 and len(clip.phashes[0]) == 16
    assert _cand("twitch_clip:ClipA").status == "rendered"

    # review
    with pytest.raises(ValueError):
        p.approve_clip(clip.id, [])
    p.approve_clip(
        clip.id,
        ["youtube", "tiktok"],
        titles={"youtube": "Edited title"},
        descriptions={"tiktok": "my own text"},
    )
    with dbmod.session_scope() as s:
        c = s.get(Clip, clip.id)
        assert c.status == "approved" and c.titles["youtube"].startswith("Edited title")
        assert c.descriptions["tiktok"].startswith("my own text") and "xQc" in c.descriptions["tiktok"]
    other = next(c for c in clips if c.id != clip.id)
    p.reject_clip(other.id, "langweilig")

    # slots
    assert p.schedule_approved(now=NOW) == 2
    posts = _all(Post)
    assert {x.platform for x in posts} == {"youtube", "tiktok"}
    assert all(x.scheduled_at == NOW + timedelta(hours=1) for x in posts)
    assert p.schedule_approved(now=NOW) == 0  # idempotent

    # YouTube native_schedule: sofort vorab hochladen (publishAt), TikTok erst wenn fällig
    assert p.publish_due(now=NOW) == 1
    assert len(yt.requests) == 1 and yt.requests[0].publish_at == NOW + timedelta(hours=1)
    assert tt.requests == []
    with dbmod.session_scope() as s:
        ypost = s.exec(select(Post).where(Post.platform == "youtube")).one()
        assert ypost.status == "scheduled" and ypost.remote_id == "yt1"
        assert s.get(Clip, clip.id).status == "scheduled"

    due = NOW + timedelta(hours=1, minutes=1)
    assert p.publish_due(now=due) == 1
    assert tt.requests[0].publish_at is None
    with dbmod.session_scope() as s:
        by = {x.platform: x for x in s.exec(select(Post)).all()}
        assert by["tiktok"].status == "draft_sent"
        assert by["youtube"].status == "posted" and by["youtube"].posted_at == NOW + timedelta(hours=1)
        assert s.get(Clip, clip.id).status == "posted"
        assert s.get(Clip, other.id).status == "rejected"

    # stats + feedback
    assert p.fetch_stats(now=NOW + timedelta(hours=26)) == 1
    assert p.fetch_stats(now=NOW + timedelta(hours=27)) == 0  # nicht doppelt
    m = _all(Metric)
    assert len(m) == 1 and m[0].views == 1234 and m[0].hours_after == 24
    ex = p.performance_examples(6)
    assert len(ex) == 1 and ex[0].views == 1234 and ex[0].hook_text == "HE JUMPED THE BRIDGE"

    jobs = {r.job for r in _all(JobRun)}
    assert {"discover:twitch", "process", "schedule", "publish", "stats"} <= jobs
    assert all(r.finished_at is not None for r in _all(JobRun))


def test_rediscover_updates_new_rows(settings, db):
    src = FakeSource("twitch", [make_cand("ClipA", views=1000)])
    p = make_pipeline(settings, sources=[src])
    assert p.discover(now=NOW) == 1
    before = _cand("twitch_clip:ClipA").prescore
    src.candidates = [make_cand("ClipA", views=50000), make_cand("ClipA", views=50000)]
    assert p.discover(now=NOW) == 0
    row = _cand("twitch_clip:ClipA")
    assert row.view_count == 50000 and row.prescore > before
    assert len(_all(Candidate)) == 1


def test_discover_errors_are_recorded_not_raised(settings, db):
    from wdvi.sources.base import SourceError

    bad = FakeSource("reddit", [], error=SourceError("Reddit nicht erreichbar"))
    off = FakeSource("kick", [], configured=False)
    ok = FakeSource("twitch", [make_cand("ClipA")])
    p = make_pipeline(settings, sources=[bad, off, ok])
    assert p.discover(now=NOW) == 1
    runs = {r.job: r for r in _all(JobRun)}
    assert runs["discover:reddit"].ok is False and "nicht erreichbar" in runs["discover:reddit"].message
    assert runs["discover:kick"].ok is False and "nicht konfiguriert" in runs["discover:kick"].message
    assert runs["discover:twitch"].ok is True
    with pytest.raises(ValueError, match="unbekannt"):
        p.discover("nope")


def test_same_twitch_moment_keeps_best_clip(settings, db):
    extra = {"broadcaster_id": "71092938", "video_id": "555", "vod_offset": 1000}
    src = FakeSource("twitch", [make_cand("Low", views=300, extra=dict(extra))])
    p = make_pipeline(settings, sources=[src])
    assert p.discover(now=NOW) == 1
    src.candidates = [
        make_cand("High", views=9000, extra={**extra, "vod_offset": 1008}),
        make_cand("Dup", views=100, extra={**extra, "vod_offset": 995}),
    ]
    assert p.discover(now=NOW) == 1
    assert _cand("twitch_clip:Low").status == "skipped"
    assert _cand("twitch_clip:High").status == "new"
    dup = _cand("twitch_clip:Dup")
    assert dup.status == "skipped" and "High" in dup.status_reason


def test_require_approval_false_auto_approves(settings, db, monkeypatch):
    settings.config.publish.require_approval = False
    MediaFakes(monkeypatch)
    p = make_pipeline(
        settings,
        sources=[FakeSource("twitch", [make_cand("ClipA")])],
        publishers={"youtube": FakePublisher("youtube")},
    )
    p.discover(now=NOW)
    assert p.process_cycle(now=NOW) == 1
    clip = _all(Clip)[0]
    assert clip.status == "approved" and clip.platforms == ["youtube"]
    assert p.schedule_approved(now=NOW) == 1


def test_youtube_video_is_mined_into_moments(settings, db, monkeypatch):
    from wdvi.brain import editor
    from wdvi.sources import youtube as yt_source

    fakes = MediaFakes(monkeypatch)
    video = make_cand(
        "vid123",
        source=SourceKind.youtube_video,
        platform="YouTube",
        creator="GTA Guy",
        handle=None,
        duration=None,
        views=100000,
        extra={"channel_id": "UCabc"},
    )
    calls = {}

    def fetch_video_context(url, settings):
        calls["url"] = url
        return yt_source.VideoContext(
            video_id="vid123", title="t", channel="GTA Guy", duration=1800.0, view_count=150000
        )

    def mine_moments(llm, v, ctx, settings):
        calls["duration"] = v.duration_s
        return [
            MomentPick(start=100.0, end=140.0, score=90, why="crazy"),
            MomentPick(start=600.0, end=630.0, score=70, why="funny"),
        ]

    monkeypatch.setattr(yt_source, "fetch_video_context", fetch_video_context)
    monkeypatch.setattr(editor, "mine_moments", mine_moments)
    p = make_pipeline(settings, sources=[FakeSource("youtube", [video])])
    p.discover(now=NOW)
    assert p.process_cycle(now=NOW) == 0  # Mining erzeugt noch keine Clips
    assert calls["duration"] == 1800.0
    parent = _cand(video.key)
    assert parent.status == "mined" and "2 Momente" in parent.status_reason
    with dbmod.session_scope() as s:
        moments = s.exec(select(Candidate).where(Candidate.parent_key == video.key)).all()
    assert len(moments) == 2 and all(m.source == "youtube_moment" and m.status == "new" for m in moments)
    best = max(moments, key=lambda m: m.prescore)
    assert best.start_s == 100.0
    assert p.moment_ids(video.key)[0] == best.id
    # nächster Zyklus verarbeitet die Momente wie normale Clips
    assert p.process_cycle(now=NOW) == 2
    assert fakes.plans[0] == best.key


def test_process_lock_prevents_parallel_cycles(settings, db, monkeypatch):
    import threading

    MediaFakes(monkeypatch)
    from wdvi.render import renderer

    started, release = threading.Event(), threading.Event()
    real = renderer.render_clip

    def slow_render(*a, **kw):
        started.set()
        release.wait(5)
        return real(*a, **kw)

    monkeypatch.setattr(renderer, "render_clip", slow_render)
    p = make_pipeline(settings, sources=[FakeSource("twitch", [make_cand("ClipA")])])
    p.discover(now=NOW)
    results: list[int] = []
    t = threading.Thread(target=lambda: results.append(p.process_cycle(now=NOW)))
    t.start()
    assert started.wait(5)
    assert p.process_cycle(now=NOW) == 0  # läuft schon -> sofort zurück
    release.set()
    t.join(5)
    assert results == [1]
