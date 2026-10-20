"""Slots, Upload, Retry/Backoff, YouTube-Vorab-Upload, Stats, Feedback-Beispiele, Aufräumen."""

from __future__ import annotations

import os
import time
from datetime import timedelta
from pathlib import Path

from sqlmodel import select

from tests.test_pipeline_support import NOW, FakePublisher, MediaFakes, make_cand, make_pipeline
from wdvi import db as dbmod
from wdvi.models import Candidate, Clip, JobRun, Metric, Post
from wdvi.pipeline import (
    INTERRUPTED_UPLOAD,
    MAX_POST_ATTEMPTS,
    POST_MAX_AGE,
    POST_RETRY_DELAYS,
    RETRY_STEP,
    TRANSIENT_POST_DELAYS,
    candidate_from_in,
)
from wdvi.schemas import PostMetrics, PostResult


def _clip(settings, platforms=("tiktok",), status="approved", key="ClipA", tmp: Path | None = None) -> int:
    video = (tmp or settings.config.paths.clips_dir) / f"{key}.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"x")
    with dbmod.session_scope() as s:
        cand = candidate_from_in(make_cand(key), now=NOW, status="rendered")
        s.add(cand)
        s.flush()
        clip = Clip(
            candidate_id=cand.id,
            status=status,
            virality_score=80,
            video_path=str(video),
            thumb_path=None,
            plan={"hook_text": f"hook {key}", "hashtags": ["GTA6"]},
            titles={p: f"title {key} {p}" for p in platforms},
            descriptions={p: f"desc {key}" for p in platforms},
            platforms=list(platforms),
            reviewed_at=NOW,
        )
        s.add(clip)
        s.flush()
        return int(clip.id)


def _post(platform: str) -> Post:
    with dbmod.session_scope() as s:
        return s.exec(select(Post).where(Post.platform == platform)).one()


def _clip_row(clip_id: int) -> Clip:
    with dbmod.session_scope() as s:
        return s.get(Clip, clip_id)


def test_retry_with_backoff_then_success(settings, db, monkeypatch):
    MediaFakes(monkeypatch)  # deterministische Slots
    settings.config.publish.tiktok.enabled = True
    tt = FakePublisher(
        "tiktok", results=[PostResult(ok=False, error="TikTok 500"), PostResult(ok=False, error="TikTok 503")]
    )
    p = make_pipeline(settings, publishers={"tiktok": tt})
    clip_id = _clip(settings)
    assert p.schedule_approved(now=NOW) == 1
    slot = _post("tiktok").scheduled_at
    assert _clip_row(clip_id).status == "scheduled"

    assert p.publish_due(now=slot - timedelta(minutes=5)) == 0 and tt.requests == []  # noch nicht fällig
    t1 = slot + timedelta(minutes=1)
    assert p.publish_due(now=t1) == 0
    post = _post("tiktok")
    assert post.status == "scheduled" and post.attempts == 1 and post.error == "TikTok 500"
    assert post.scheduled_at == t1 + RETRY_STEP
    t2 = post.scheduled_at
    assert p.publish_due(now=t2) == 0
    post = _post("tiktok")
    assert post.attempts == 2 and post.scheduled_at == t2 + POST_RETRY_DELAYS[1]  # echter Backoff: 2 h
    assert p.publish_due(now=post.scheduled_at) == 1
    post = _post("tiktok")
    assert post.status == "posted" and post.remote_id == "tiktok-3" and post.error is None
    assert _clip_row(clip_id).status == "posted"
    assert len(tt.requests) == 3 and tt.requests[0].hashtags == ["GTA6"]


def test_gives_up_after_max_attempts(settings, db, monkeypatch):
    MediaFakes(monkeypatch)
    settings.config.publish.tiktok.enabled = True
    tt = FakePublisher("tiktok", results=[PostResult(ok=False, error=f"kaputt {i}") for i in range(5)])
    p = make_pipeline(settings, publishers={"tiktok": tt})
    clip_id = _clip(settings)
    p.schedule_approved(now=NOW)
    t = _post("tiktok").scheduled_at
    for _ in range(MAX_POST_ATTEMPTS):
        p.publish_due(now=t)
        t = _post("tiktok").scheduled_at + timedelta(seconds=1)
    post = _post("tiktok")
    assert post.status == "failed" and post.attempts == MAX_POST_ATTEMPTS
    clip = _clip_row(clip_id)
    assert clip.status == "failed" and "tiktok" in clip.error
    assert p.publish_due(now=t + timedelta(hours=5)) == 0 and len(tt.requests) == MAX_POST_ATTEMPTS


def test_not_connected_keeps_post_queued(settings, db, monkeypatch):
    """Nicht (mehr) verbunden ist kein Fehler des Videos: kein Fehlversuch, Post wartet auf einen freien Slot."""
    MediaFakes(monkeypatch)
    settings.config.publish.instagram.enabled = True
    ig = FakePublisher("instagram", connected=False)
    p = make_pipeline(settings, publishers={"instagram": ig})
    clip_id = _clip(settings, platforms=("instagram",))
    p.schedule_approved(now=NOW)
    t = NOW + timedelta(hours=2)
    for _ in range(6):  # stundenlang nicht verbunden
        p.publish_due(now=t)
        post = _post("instagram")
        assert post.status == "scheduled" and post.attempts == 0 and "nicht verbunden" in post.error
        assert post.scheduled_at >= t + TRANSIENT_POST_DELAYS[0]
        t = post.scheduled_at
    assert ig.requests == [] and _clip_row(clip_id).status == "scheduled"
    ig.connected = True
    assert p.publish_due(now=t) == 1
    assert _post("instagram").status == "posted" and _clip_row(clip_id).status == "posted"


def test_transient_errors_do_not_use_up_attempts(settings, db, monkeypatch):
    """Internet weg / Serverfehler: wachsender Abstand (15 min, 1 h, 4 h, 12 h), keine Versuche verbraucht."""
    MediaFakes(monkeypatch)
    settings.config.publish.tiktok.enabled = True
    err = PostResult(ok=False, error="Netzwerkfehler bei TikTok (ConnectError): [Errno -3] Temporary failure")
    tt = FakePublisher("tiktok", results=[err.model_copy() for _ in range(5)])
    p = make_pipeline(settings, publishers={"tiktok": tt})
    clip_id = _clip(settings)
    p.schedule_approved(now=NOW)
    t = _post("tiktok").scheduled_at
    for delay in (*TRANSIENT_POST_DELAYS, TRANSIENT_POST_DELAYS[-1]):
        p.publish_due(now=t)
        post = _post("tiktok")
        assert post.status == "scheduled" and post.attempts == 0, post.error
        assert post.scheduled_at == t + delay and "kein Fehlversuch" in post.error.replace(
            "zählt nicht als Fehlversuch", "kein Fehlversuch"
        )
        t = post.scheduled_at
    assert p.publish_due(now=t) == 1 and _clip_row(clip_id).status == "posted"


def test_transient_errors_give_up_after_max_age(settings, db, monkeypatch):
    MediaFakes(monkeypatch)
    settings.config.publish.tiktok.enabled = True
    tt = FakePublisher("tiktok", results=[PostResult(ok=False, error="TikTok: Netzwerkfehler")] * 3)
    p = make_pipeline(settings, publishers={"tiktok": tt})
    clip_id = _clip(settings)
    p.schedule_approved(now=NOW)
    with dbmod.session_scope() as s:
        s.exec(select(Post)).one().created_at = NOW - POST_MAX_AGE - timedelta(hours=1)
    p.publish_due(now=NOW + timedelta(hours=2))
    post = _post("tiktok")
    assert post.status == "failed" and "aufgegeben" in post.error
    assert _clip_row(clip_id).status == "failed"


def test_quota_error_moves_post_to_next_free_slot(settings, db, monkeypatch):
    MediaFakes(monkeypatch)
    settings.config.publish.tiktok.enabled = True
    quota = PostResult(
        ok=False,
        error="TikTok: Zu viele offene Entwürfe in der Inbox (max. 5 in 24 h) – Entwürfe posten",
        retry_after_s=6 * 3600,
    )
    tt = FakePublisher("tiktok", results=[quota])
    p = make_pipeline(settings, publishers={"tiktok": tt})
    _clip(settings, key="One")
    _clip(settings, key="Two")
    p.schedule_approved(now=NOW)
    with dbmod.session_scope() as s:
        first, second = sorted(s.exec(select(Post)).all(), key=lambda x: x.scheduled_at)
        first_id, other_slot = first.id, second.scheduled_at
    t = first.scheduled_at
    p.publish_due(now=t)
    with dbmod.session_scope() as s:
        post = s.get(Post, first_id)
    assert post.status == "scheduled" and post.attempts == 0
    assert post.scheduled_at >= t + timedelta(hours=6)  # retry_after
    assert abs(post.scheduled_at - other_slot) >= timedelta(hours=1)  # kein Doppel-Slot
    assert "Fehlversuch" in post.error


def test_youtube_early_upload_window_and_best_effort_retry(settings, db, monkeypatch):
    MediaFakes(monkeypatch)
    yt = FakePublisher(
        "youtube",
        results=[
            PostResult(ok=False, error="quotaExceeded"),
            PostResult(ok=True, remote_id="v1", status="scheduled"),
        ],
    )
    p = make_pipeline(settings, publishers={"youtube": yt})
    clip_id = _clip(settings, platforms=("youtube",))
    p.schedule_approved(now=NOW)
    slot = _post("youtube").scheduled_at

    # mehr als 24 h vorher: noch nicht hochladen
    assert p.publish_due(now=slot - timedelta(hours=30)) == 0 and yt.requests == []
    early = slot - timedelta(hours=10)
    assert p.publish_due(now=early) == 0
    post = _post("youtube")
    assert post.attempts == 0 and post.status == "scheduled" and post.scheduled_at == slot  # Slot bleibt
    assert p.publish_due(now=early + timedelta(minutes=10)) == 0 and len(yt.requests) == 1  # Backoff
    assert p.publish_due(now=early + timedelta(minutes=31)) == 1
    assert yt.requests[-1].publish_at == slot
    post = _post("youtube")
    assert post.status == "scheduled" and post.remote_id == "v1"
    assert _clip_row(clip_id).status == "posted"  # liegt geplant auf YouTube
    assert p.publish_due(now=slot + timedelta(minutes=1)) == 0 and len(yt.requests) == 2
    post = _post("youtube")
    assert post.status == "posted" and post.posted_at == slot


def test_disabled_platform_cancels_and_reject_cancels_posts(settings, db, monkeypatch):
    MediaFakes(monkeypatch)
    settings.config.publish.tiktok.enabled = True
    p = make_pipeline(
        settings, publishers={"youtube": FakePublisher("youtube"), "tiktok": FakePublisher("tiktok")}
    )
    c1 = _clip(settings, platforms=("tiktok",), key="One")
    c2 = _clip(settings, platforms=("youtube",), key="Two")
    p.schedule_approved(now=NOW)
    p2 = make_pipeline(settings, publishers={"youtube": FakePublisher("youtube")})  # TikTok inzwischen aus
    p2.reject_clip(c2, "doch nicht")
    p2.publish_due(now=NOW + timedelta(hours=3))
    with dbmod.session_scope() as s:
        posts = {x.clip_id: x for x in s.exec(select(Post)).all()}
    assert posts[c1].status == "canceled" and "deaktiviert" in posts[c1].error
    assert posts[c2].status == "canceled"
    assert _clip_row(c2).status == "rejected"


def test_schedule_skips_platforms_not_enabled(settings, db, monkeypatch):
    MediaFakes(monkeypatch)
    p = make_pipeline(settings, publishers={"youtube": FakePublisher("youtube")})
    cid = _clip(settings, platforms=("instagram",))
    assert p.schedule_approved(now=NOW) == 0
    clip = _clip_row(cid)
    assert clip.status == "approved" and "aktiviert" in clip.error


def test_interrupted_upload_is_not_reuploaded_automatically(settings, db, monkeypatch):
    """Neustart mitten im Upload: der Upload kann schon angekommen sein -> nicht blind wiederholen
    (Doppelpost), sondern mit Hinweis als fehlgeschlagen markieren; erneut einplanen geht von Hand."""
    MediaFakes(monkeypatch)
    settings.config.publish.tiktok.enabled = True
    tt = FakePublisher("tiktok")
    p = make_pipeline(settings, publishers={"tiktok": tt})
    clip_id = _clip(settings)
    p.schedule_approved(now=NOW)
    with dbmod.session_scope() as s:
        post = s.exec(select(Post)).one()
        post.status = "uploading"  # Absturz während des Uploads
    later = NOW + timedelta(hours=3)
    assert p.publish_due(now=later) == 0 and tt.requests == []
    post = _post("tiktok")
    assert post.status == "failed" and post.error == INTERRUPTED_UPLOAD
    clip = _clip_row(clip_id)
    assert clip.status == "failed" and "requeue" in clip.error
    assert p.publish_due(now=later) == 0  # bleibt so

    planned = p.requeue_clip(clip_id, now=later)
    assert [x[0] for x in planned] == ["tiktok"] and planned[0][1] > later
    post = _post("tiktok")
    assert post.status == "scheduled" and post.attempts == 0 and post.error is None
    assert _clip_row(clip_id).status == "scheduled"
    assert p.publish_due(now=planned[0][1]) == 1 and len(tt.requests) == 1
    assert _clip_row(clip_id).status == "posted"


def test_requeue_rejects_missing_video_and_unfailed_clips(settings, db, monkeypatch):
    import pytest

    MediaFakes(monkeypatch)
    settings.config.publish.tiktok.enabled = True
    p = make_pipeline(settings, publishers={"tiktok": FakePublisher("tiktok")})
    clip_id = _clip(settings)
    p.schedule_approved(now=NOW)
    with pytest.raises(ValueError, match="keine fehlgeschlagenen"):
        p.requeue_clip(clip_id)
    Path(_clip_row(clip_id).video_path).unlink()
    with pytest.raises(ValueError, match="existiert nicht mehr"):
        p.requeue_clip(clip_id)
    with pytest.raises(ValueError, match="nicht gefunden"):
        p.requeue_clip(999)


def test_publisher_exception_is_contained(settings, db, monkeypatch):
    MediaFakes(monkeypatch)
    settings.config.publish.tiktok.enabled = True
    tt = FakePublisher("tiktok")

    def boom(req):
        raise RuntimeError("socket closed")

    tt.publish = boom
    p = make_pipeline(settings, publishers={"tiktok": tt})
    _clip(settings)
    p.schedule_approved(now=NOW)
    assert p.publish_due(now=NOW + timedelta(hours=2)) == 0
    post = _post("tiktok")
    assert post.attempts == 1 and "socket closed" in post.error
    with dbmod.session_scope() as s:
        run = s.exec(select(JobRun).where(JobRun.job == "publish")).all()[-1]
    assert run.ok is True and "1 fehlgeschlagen" in run.message


# --------------------------------------------------------------------------- Stats / Feedback


def _posted(clip_id: int, platform: str, posted_at, remote: str) -> int:
    with dbmod.session_scope() as s:
        post = Post(
            clip_id=clip_id,
            platform=platform,
            status="posted",
            scheduled_at=posted_at,
            title=f"t {remote}",
            description="d",
            remote_id=remote,
            posted_at=posted_at,
        )
        s.add(post)
        s.flush()
        return int(post.id)


def test_fetch_stats_windows(settings, db):
    yt = FakePublisher("youtube", metrics=PostMetrics(views=500, likes=10))
    p = make_pipeline(settings, publishers={"youtube": yt})
    cid = _clip(settings, platforms=("youtube",), status="posted")
    _posted(cid, "youtube", NOW, "v1")
    assert p.fetch_stats(now=NOW + timedelta(hours=10)) == 0  # noch zu früh
    assert p.fetch_stats(now=NOW + timedelta(hours=25)) == 1
    assert p.fetch_stats(now=NOW + timedelta(hours=73)) == 1
    with dbmod.session_scope() as s:
        hours = sorted(m.hours_after for m in s.exec(select(Metric)).all())
    assert hours == [24, 72]
    settings.config.feedback.enabled = False
    assert p.fetch_stats(now=NOW + timedelta(hours=80)) == 0


def test_fetch_stats_skips_stale_windows(settings, db):
    yt = FakePublisher("youtube", metrics=PostMetrics(views=500))
    p = make_pipeline(settings, publishers={"youtube": yt})
    cid = _clip(settings, platforms=("youtube",), status="posted")
    _posted(cid, "youtube", NOW, "v1")
    # PC war 2 Tage aus: 24-h-Messung wäre falsch -> nur die 72-h-Messung
    assert p.fetch_stats(now=NOW + timedelta(hours=74)) == 1
    with dbmod.session_scope() as s:
        assert [m.hours_after for m in s.exec(select(Metric)).all()] == [72]


def test_performance_examples_best_and_worst(settings, db):
    p = make_pipeline(settings)
    views = [10, 5000, 300, 90000, 42, 700]
    with dbmod.session_scope() as s:
        pass
    for i, v in enumerate(views):
        cid = _clip(settings, platforms=("youtube",), status="posted", key=f"K{i}")
        pid = _posted(cid, "youtube", NOW, f"v{i}")
        with dbmod.session_scope() as s:
            s.add(Metric(post_id=pid, hours_after=24, views=v // 2))
            s.add(Metric(post_id=pid, hours_after=72, views=v))
    ex = p.performance_examples(4)
    assert [e.views for e in ex] == [90000, 5000, 42, 10]
    assert ex[0].hours_after == 72 and ex[0].hook_text == "hook K3" and ex[0].platform == "youtube"
    assert p.performance_examples(0) == []
    assert len(p.performance_examples(20)) == 6


def test_feedback_examples_reach_the_editor(settings, db, monkeypatch):
    fakes = MediaFakes(monkeypatch)
    settings.config.feedback.examples_in_prompt = 2
    cid = _clip(settings, platforms=("youtube",), status="posted", key="Old")
    pid = _posted(cid, "youtube", NOW, "v0")
    with dbmod.session_scope() as s:
        s.add(Metric(post_id=pid, hours_after=24, views=777))
    p = make_pipeline(settings)
    new_id = p.add_candidate(make_cand("Fresh"))
    p.process_candidate(new_id)
    assert [e.views for e in fakes.examples_seen[-1]] == [777]


# --------------------------------------------------------------------------- Aufräumen


def test_cleanup(settings, db):
    p = make_pipeline(settings)
    work = settings.config.paths.work_dir / "twitch_clip_Old"
    work.mkdir(parents=True)
    old_file, new_file = work / "source.mp4", settings.config.paths.work_dir / "fresh.mp4"
    old_file.write_bytes(b"x" * 10)
    new_file.write_bytes(b"y")
    past = time.time() - 3 * 86400
    os.utime(old_file, (past, past))
    os.utime(work, (past, past))

    rejected = _clip(settings, platforms=("youtube",), status="rejected", key="Rej")
    kept = _clip(settings, platforms=("youtube",), status="rejected", key="RejNew")
    with dbmod.session_scope() as s:
        s.get(Clip, rejected).reviewed_at = NOW - timedelta(days=8)
        s.get(Clip, kept).reviewed_at = NOW - timedelta(days=1)
        s.add(JobRun(job="process", started_at=NOW - timedelta(days=30)))

    p.cleanup(now=NOW)
    assert not old_file.exists() and not work.exists() and new_file.exists()
    r1, r2 = _clip_row(rejected), _clip_row(kept)
    assert not Path(r1.video_path).exists() and "gelöscht" in r1.error
    assert Path(r2.video_path).exists()
    with dbmod.session_scope() as s:
        runs = s.exec(select(JobRun)).all()
        assert all(r.started_at > NOW - timedelta(days=20) for r in runs)
        assert any(r.job == "cleanup" and r.ok for r in runs)
        assert s.exec(select(Candidate)).first() is not None


def test_youtube_retry_after_due_failure_uses_backoff(settings, db, monkeypatch):
    MediaFakes(monkeypatch)
    settings.config.publish.youtube.native_schedule = True
    yt = FakePublisher("youtube", results=[PostResult(ok=False, error=f"HTTP 500 #{i}") for i in range(5)])
    p = make_pipeline(settings, publishers={"youtube": yt})
    _clip(settings, platforms=("youtube",))
    p.schedule_approved(now=NOW)
    slot = _post("youtube").scheduled_at
    with dbmod.session_scope() as s:  # Vorab-Upload überspringen: direkt zur Fälligkeit
        s.exec(select(Post)).one().scheduled_at = NOW - timedelta(minutes=1)
    p.publish_due(now=NOW)
    post = _post("youtube")
    # HTTP 500 = vorübergehend: kein Fehlversuch, aber Backoff
    assert post.attempts == 0 and post.scheduled_at == NOW + TRANSIENT_POST_DELAYS[0]
    # in der Backoff-Zeit kein neuer (Vorab-)Versuch, obwohl der Termin im 24-h-Fenster liegt
    assert p.publish_due(now=NOW + timedelta(minutes=5)) == 0 and len(yt.requests) == 1
    p.publish_due(now=NOW + RETRY_STEP)
    post = _post("youtube")
    assert len(yt.requests) == 2 and post.scheduled_at == NOW + RETRY_STEP + TRANSIENT_POST_DELAYS[1]
    assert slot > NOW


def test_youtube_real_failure_counts_attempt_and_blocks_early_retry(settings, db, monkeypatch):
    MediaFakes(monkeypatch)
    yt = FakePublisher("youtube", results=[PostResult(ok=False, error="YouTube lehnt den Titel ab.")] * 2)
    p = make_pipeline(settings, publishers={"youtube": yt})
    _clip(settings, platforms=("youtube",))
    p.schedule_approved(now=NOW)
    with dbmod.session_scope() as s:  # Vorab-Upload überspringen: direkt zur Fälligkeit
        s.exec(select(Post)).one().scheduled_at = NOW - timedelta(minutes=1)
    p.publish_due(now=NOW)
    post = _post("youtube")
    assert post.attempts == 1 and post.scheduled_at == NOW + RETRY_STEP
    assert p.publish_due(now=NOW + timedelta(minutes=5)) == 0 and len(yt.requests) == 1


def test_unlisted_youtube_is_not_uploaded_early(settings, db, monkeypatch):
    """publishAt gibt es nur für 'public' – unlisted würde sofort sichtbar, also erst zum Slot hochladen."""
    MediaFakes(monkeypatch)
    settings.config.publish.youtube.privacy = "unlisted"
    yt = FakePublisher("youtube")
    p = make_pipeline(settings, publishers={"youtube": yt})
    _clip(settings, platforms=("youtube",))
    p.schedule_approved(now=NOW)
    slot = _post("youtube").scheduled_at
    assert p.publish_due(now=slot - timedelta(hours=10)) == 0 and yt.requests == []
    assert p.publish_due(now=slot) == 1 and yt.requests[0].publish_at is None
