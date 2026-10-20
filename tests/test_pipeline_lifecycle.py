"""Lebenszyklus nativ geplanter YouTube-Videos (unschedule, Sichtbarkeitsprüfung nach publishAt),
nicht einplanbare Clips in Health, process-url-Schutz gegen Duplikate, Aufräumen geteilter Videodateien und
Credit/Block-Regeln für Reddit-Reposts."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from sqlmodel import select

from tests.test_pipeline_support import NOW, FakePublisher, FakeSource, MediaFakes, make_cand, make_pipeline
from wdvi import db as dbmod
from wdvi.dashboard import _data as dash_data
from wdvi.models import Candidate, Clip, CreatorRule, JobRun, Post
from wdvi.pipeline import REMOTE_CHECK_AFTER, REMOTE_CHECK_UNTIL, candidate_from_in, creator_keys
from wdvi.publish import slots, text
from wdvi.publish.base import PublishError
from wdvi.publish.youtube import LOCKED_PRIVATE_HINT
from wdvi.schemas import PostResult, SourceKind


class LifecycleYouTube(FakePublisher):
    """FakePublisher mit den YouTube-Extras unschedule() und check_status()."""

    def __init__(self, visibility: list[str | None] | None = None, unschedule_error: Exception | None = None):
        super().__init__("youtube", results=[PostResult(ok=True, remote_id="v1", status="scheduled")])
        self.visibility = list(visibility or [])  # Antworten von check_status der Reihe nach
        self.unschedule_error = unschedule_error
        self.unscheduled: list[str] = []
        self.checks: list[str] = []

    def unschedule(self, remote_id: str) -> str:
        self.unscheduled.append(remote_id)
        if self.unschedule_error is not None:
            raise self.unschedule_error
        return "Bei YouTube zurückgezogen: Das Video ist jetzt privat ohne Veröffentlichungstermin (nicht gelöscht)."

    def check_status(self, remote_id: str) -> str | None:
        self.checks.append(remote_id)
        return self.visibility.pop(0) if self.visibility else "public"


def _clip(settings, platforms=("youtube",), status="approved", key="ClipA", video: Path | None = None) -> int:
    if video is None:
        video = settings.config.paths.clips_dir / f"{key}.mp4"
        video.parent.mkdir(parents=True, exist_ok=True)
        video.write_bytes(b"x")
    with dbmod.session_scope() as s:
        cand = s.exec(select(Candidate).where(Candidate.key == f"twitch_clip:{key}")).first()
        if cand is None:
            cand = candidate_from_in(make_cand(key), now=NOW, status="rendered")
            s.add(cand)
            s.flush()
        clip = Clip(
            candidate_id=cand.id,
            status=status,
            virality_score=80,
            video_path=str(video),
            plan={"hook_text": f"hook {key}", "hashtags": ["GTA6"]},
            titles={p: f"title {key} {p}" for p in platforms},
            descriptions={p: f"desc {key}" for p in platforms},
            platforms=list(platforms),
            reviewed_at=NOW,
            created_at=NOW,
        )
        s.add(clip)
        s.flush()
        return int(clip.id)


def _posts() -> dict[str, Post]:
    with dbmod.session_scope() as s:
        return {p.platform: p for p in s.exec(select(Post)).all()}


def _clip_row(clip_id: int) -> Clip:
    with dbmod.session_scope() as s:
        return s.get(Clip, clip_id)


def _last_run(job: str) -> JobRun:
    with dbmod.session_scope() as s:
        return s.exec(select(JobRun).where(JobRun.job == job).order_by(JobRun.id)).all()[-1]


def _scheduled_on_youtube(settings, monkeypatch, yt, platforms=("youtube",)):
    """Clip freigegeben, eingeplant und bei YouTube vorab als privat + publishAt hochgeladen."""
    MediaFakes(monkeypatch)
    settings.config.publish.tiktok.enabled = "tiktok" in platforms
    pubs = {"youtube": yt}
    if "tiktok" in platforms:
        pubs["tiktok"] = FakePublisher("tiktok")
    p = make_pipeline(settings, publishers=pubs)
    clip_id = _clip(settings, platforms=platforms)
    assert p.schedule_approved(now=NOW) == len(platforms)
    slot = _posts()["youtube"].scheduled_at.replace(tzinfo=NOW.tzinfo)
    assert p.publish_due(now=slot - timedelta(hours=2)) == 1  # Vorab-Upload
    post = _posts()["youtube"]
    assert post.status == "scheduled" and post.remote_id == "v1"
    return p, clip_id, slot


# --------------------------------------------------------------------------- Abbrechen -> unschedule


def test_cancel_post_unschedules_video_on_youtube(settings, db, monkeypatch):
    yt = LifecycleYouTube()
    p, clip_id, _slot = _scheduled_on_youtube(settings, monkeypatch, yt)
    ok, msg = p.cancel_post(_posts()["youtube"].id)
    assert ok and yt.unscheduled == ["v1"]
    assert "YouTube-Post abgebrochen" in msg and "zurückgezogen" in msg and "zusätzlich" not in msg
    post = _posts()["youtube"]
    assert post.status == "canceled" and "zurückgezogen" in post.error
    assert _clip_row(clip_id).status == "pending_review"  # letzter Post weg -> zurück in die Review
    # nicht mehr geplant -> zweiter Versuch wird abgelehnt, ohne YouTube erneut anzufassen
    ok, msg = p.cancel_post(post.id)
    assert not ok and "nicht (mehr) geplant" in msg and yt.unscheduled == ["v1"]


def test_cancel_post_warns_when_unschedule_fails(settings, db, monkeypatch):
    yt = LifecycleYouTube(unschedule_error=PublishError("YouTube erlaubt das Zurückziehen nicht (youtube.force-ssl)"))
    p, _clip_id, _slot = _scheduled_on_youtube(settings, monkeypatch, yt)
    ok, msg = p.cancel_post(_posts()["youtube"].id)
    assert ok and "zusätzlich" in msg and "force-ssl" in msg
    post = _posts()["youtube"]
    assert post.status == "canceled" and "ACHTUNG" in post.error and "v1" in post.error


def test_cancel_refuses_running_upload(settings, db, monkeypatch):
    MediaFakes(monkeypatch)
    p = make_pipeline(settings, publishers={"youtube": LifecycleYouTube()})
    clip_id = _clip(settings)
    p.schedule_approved(now=NOW)
    with dbmod.session_scope() as s:
        s.exec(select(Post)).one().status = "uploading"
    ok, msg = p.cancel_post(_posts()["youtube"].id)
    assert not ok and "läuft gerade" in msg
    assert p.cancel_post(9999) == (False, "Post #9999 nicht gefunden.")
    assert _clip_row(clip_id).status == "scheduled"


def test_reject_clip_unschedules_remote_posts(settings, db, monkeypatch):
    yt = LifecycleYouTube()
    p, clip_id, slot = _scheduled_on_youtube(settings, monkeypatch, yt, platforms=("youtube", "tiktok"))
    p.reject_clip(clip_id, "doch nicht")
    posts = _posts()
    assert yt.unscheduled == ["v1"]
    assert {x.status for x in posts.values()} == {"canceled"}
    assert "zurückgezogen" in posts["youtube"].error
    clip = _clip_row(clip_id)
    assert clip.status == "rejected" and clip.error == "doch nicht"
    # nach dem Termin wird nichts als "gepostet" gezählt
    p.publish_due(now=slot + REMOTE_CHECK_UNTIL)
    assert _posts()["youtube"].status == "canceled" and yt.checks == []


def test_reject_clip_without_unschedule_tells_user_to_delete(settings, db, monkeypatch):
    yt = LifecycleYouTube(unschedule_error=PublishError("Netz weg"))
    p, clip_id, _slot = _scheduled_on_youtube(settings, monkeypatch, yt)
    p.reject_clip(clip_id)
    assert "selbst löschen" in _clip_row(clip_id).error and "v1" in _clip_row(clip_id).error


def test_block_rule_unschedules_on_youtube(settings, db, monkeypatch):
    yt = LifecycleYouTube()
    p, clip_id, slot = _scheduled_on_youtube(settings, monkeypatch, yt)
    with dbmod.session_scope() as s:
        s.add(CreatorRule(platform_label="Twitch", handle="xqc", rule="block", note="Opt-out"))
    p.publish_due(now=slot - timedelta(hours=1))
    assert yt.unscheduled == ["v1"]
    post = _posts()["youtube"]
    assert post.status == "canceled" and "zurückgezogen" in post.error
    clip = _clip_row(clip_id)
    assert clip.status == "rejected" and "selbst löschen" not in clip.error


# --------------------------------------------------------------------------- Sichtbarkeit nach publishAt


def test_public_after_publish_at_counts_as_posted(settings, db, monkeypatch):
    yt = LifecycleYouTube(visibility=["public"])
    p, clip_id, slot = _scheduled_on_youtube(settings, monkeypatch, yt)
    p.publish_due(now=slot + timedelta(minutes=1))  # YouTube braucht einen Moment -> noch nicht prüfen
    assert yt.checks == [] and _posts()["youtube"].status == "scheduled"
    p.publish_due(now=slot + REMOTE_CHECK_AFTER)
    post = _posts()["youtube"]
    assert yt.checks == ["v1"] and post.status == "posted" and post.error is None
    assert post.posted_at.replace(tzinfo=NOW.tzinfo) == slot
    assert _clip_row(clip_id).status == "posted"


def test_still_private_after_publish_at_is_flagged(settings, db, monkeypatch):
    """Ungeprüftes API-Projekt: YouTube lässt das Video privat – nicht still als "gepostet" zählen."""
    yt = LifecycleYouTube(visibility=["private"])
    p, _clip_id, slot = _scheduled_on_youtube(settings, monkeypatch, yt)
    p.publish_due(now=slot + REMOTE_CHECK_AFTER + timedelta(minutes=1))
    post = _posts()["youtube"]
    assert post.status == "posted" and post.error == LOCKED_PRIVATE_HINT
    assert "Audit" in post.error and "privat" in post.error
    run = _last_run("publish")
    assert "noch privat" in run.message
    # Dashboard: Hinweis mit Chip "Privat" unter "Gepostet"
    (shown,) = [x for x in dash_data.posted_posts(settings.config.paths.clips_dir) if x["id"] == post.id]
    assert shown["note_kind"] == "private" and "Audit" in shown["error"]


def test_status_unknown_is_retried_then_flagged(settings, db, monkeypatch):
    yt = LifecycleYouTube(visibility=[None, None, None])
    p, _clip_id, slot = _scheduled_on_youtube(settings, monkeypatch, yt)
    t = slot + REMOTE_CHECK_AFTER
    p.publish_due(now=t)
    assert yt.checks == ["v1"] and _posts()["youtube"].status == "scheduled"
    p.publish_due(now=t + timedelta(minutes=1))  # Abstand zwischen zwei Prüfungen
    assert len(yt.checks) == 1
    p.publish_due(now=t + timedelta(minutes=6))
    assert len(yt.checks) == 2 and _posts()["youtube"].status == "scheduled"
    p.publish_due(now=slot + REMOTE_CHECK_UNTIL)  # letzte Prüfung ohne Ergebnis -> mit Hinweis abschließen
    post = _posts()["youtube"]
    assert post.status == "posted" and "nicht geprüft" in post.error
    assert dash_data.note_kind(post.error) == "check"


def test_status_check_after_downtime_is_retried_before_flagging(settings, db, monkeypatch):
    """PC/Container war über den Termin hinaus aus: die erste Prüfung kommt erst lange nach publishAt und
    scheitert (Netz nach dem Start noch nicht da) – nicht sofort mit "Prüfen" abschließen, sondern weiter
    versuchen; dann wird das gesperrte Video korrekt als "Privat" erkannt."""
    yt = LifecycleYouTube(visibility=[None, "private"])
    p, _clip_id, slot = _scheduled_on_youtube(settings, monkeypatch, yt)
    t = slot + timedelta(hours=3)
    p.publish_due(now=t)
    assert yt.checks == ["v1"] and _posts()["youtube"].status == "scheduled"
    p.publish_due(now=t + timedelta(minutes=6))
    post = _posts()["youtube"]
    assert len(yt.checks) == 2 and post.status == "posted" and post.error == LOCKED_PRIVATE_HINT
    assert p._remote_check_since == {} and p._remote_check_after == {}
    # bleibt es unprüfbar, wird nach dem Mindestzeitraum mit Hinweis abgeschlossen
    yt2 = LifecycleYouTube(visibility=[None] * 10)
    p2 = make_pipeline(settings, publishers={"youtube": yt2})
    with dbmod.session_scope() as s:
        row = s.exec(select(Post)).one()
        row.status, row.error = "scheduled", None
    window = REMOTE_CHECK_UNTIL - REMOTE_CHECK_AFTER  # Mindestzeitraum ab der ersten Prüfung
    p2.publish_due(now=t)
    p2.publish_due(now=t + window - timedelta(minutes=4))
    assert _posts()["youtube"].status == "scheduled" and len(yt2.checks) == 2
    p2.publish_due(now=t + window + timedelta(minutes=1))
    post = _posts()["youtube"]
    assert post.status == "posted" and "nicht geprüft" in post.error and len(yt2.checks) == 3


def test_video_missing_after_publish_at_fails(settings, db, monkeypatch):
    yt = LifecycleYouTube(visibility=["missing"])
    p, clip_id, slot = _scheduled_on_youtube(settings, monkeypatch, yt)
    p.publish_due(now=slot + REMOTE_CHECK_AFTER)
    post = _posts()["youtube"]
    assert post.status == "failed" and "nicht (mehr) vorhanden" in post.error
    clip = _clip_row(clip_id)
    assert clip.status == "failed" and "requeue" in clip.error


def test_scheduled_page_flag_while_waiting_for_check(settings, db, monkeypatch):
    yt = LifecycleYouTube()
    p, _clip_id, slot = _scheduled_on_youtube(settings, monkeypatch, yt)
    with dbmod.session_scope() as s:  # Termin (echte Uhr) gerade vorbei, Prüfung steht noch aus
        s.exec(select(Post)).one().scheduled_at = NOW.replace(year=2000)
    (shown,) = dash_data.scheduled_posts(settings.config.paths.clips_dir)
    assert shown["awaiting_check"] is True


# --------------------------------------------------------------------------- nicht einplanbar -> Health


def test_unschedulable_clip_marks_schedule_job_failed(settings, db, monkeypatch):
    MediaFakes(monkeypatch)
    p = make_pipeline(settings, publishers={"youtube": FakePublisher("youtube")})
    clip_id = _clip(settings)

    def full(platform, taken, now, settings):
        raise PublishError("Kein freier youtube-Slot in den nächsten 14 Tagen – mehr Slots eintragen.")

    monkeypatch.setattr(slots, "next_slot", full)
    assert p.schedule_approved(now=NOW) == 0
    run = _last_run("schedule")
    assert run.ok is False and f"Clip #{clip_id}" in run.message and "14 Tagen" in run.message
    assert "nicht einplanbar" in run.message
    assert "Kein freier Slot für youtube" in _clip_row(clip_id).error
    # Health-Seite zeigt den Job als Fehler
    (job,) = [j for j in dash_data.latest_jobs(settings) if j["job"] == "schedule"]
    assert job["state"] == "error"
    # wieder Slots frei -> Job ist wieder ok
    MediaFakes(monkeypatch)
    assert p.schedule_approved(now=NOW) == 1 and _last_run("schedule").ok is True


def test_disabled_platform_choice_marks_schedule_job_failed(settings, db, monkeypatch):
    MediaFakes(monkeypatch)
    p = make_pipeline(settings, publishers={"youtube": FakePublisher("youtube")})
    clip_id = _clip(settings, platforms=("instagram",))
    p.schedule_approved(now=NOW)
    run = _last_run("schedule")
    assert run.ok is False and f"Clip #{clip_id}" in run.message


# --------------------------------------------------------------------------- process-url: kein Duplikat


def test_add_candidate_refuses_open_clip_unless_forced(settings, db, monkeypatch):
    fakes = MediaFakes(monkeypatch)
    cand = make_cand("Dup")
    p = make_pipeline(settings, sources=[FakeSource("twitch", [cand])])
    p.discover(now=NOW)
    assert p.process_cycle(now=NOW) == 1
    assert [st for _, st in p.open_clips(cand.key)] == ["pending_review"]
    for status in ("pending_review", "approved", "scheduled"):
        with dbmod.session_scope() as s:
            s.exec(select(Clip)).one().status = status
        with pytest.raises(ValueError, match="--force"):
            p.add_candidate(cand)
    assert len(fakes.downloads) == 1
    cid = p.add_candidate(cand, force=True)
    assert p.process_candidate(cid) is not None
    # abgelehnte/gepostete Clips blockieren nicht
    with dbmod.session_scope() as s:
        for clip in s.exec(select(Clip)).all():
            clip.status = "rejected"
    assert p.open_clips(cand.key) == []
    assert p.add_candidate(cand) == cid


# --------------------------------------------------------------------------- Aufräumen: geteilte Dateien


def test_cleanup_keeps_video_shared_with_open_clip(settings, db):
    p = make_pipeline(settings)
    video = settings.config.paths.clips_dir / "shared.mp4"
    video.write_bytes(b"x" * 100)
    old = _clip(settings, status="rejected", key="Shared", video=video)
    open_clip = _clip(settings, status="pending_review", key="Shared", video=video)
    with dbmod.session_scope() as s:
        s.get(Clip, old).reviewed_at = NOW - timedelta(days=30)
    p.cleanup(now=NOW)
    assert video.exists() and "gelöscht" not in (_clip_row(old).error or "")
    # der andere Clip wird abgelehnt -> beim nächsten Aufräumen darf die Datei weg
    with dbmod.session_scope() as s:
        c = s.get(Clip, open_clip)
        c.status, c.reviewed_at = "rejected", NOW - timedelta(days=30)
    p.cleanup(now=NOW)
    assert not video.exists()


def test_cleanup_old_posted_videos_respect_shared_files(settings, db):
    settings.config.cleanup.keep_posted_days = 14
    p = make_pipeline(settings)
    shared = settings.config.paths.clips_dir / "both.mp4"
    shared.write_bytes(b"x")
    a = _clip(settings, status="posted", key="Both", video=shared)
    b = _clip(settings, status="posted", key="Both", video=shared)
    fresh = settings.config.paths.clips_dir / "fresh.mp4"
    fresh.write_bytes(b"x")
    c = _clip(settings, status="posted", key="Fresh", video=fresh)
    d = _clip(settings, status="scheduled", key="Fresh", video=fresh)
    with dbmod.session_scope() as s:
        for cid in (a, b, c):
            clip = s.get(Clip, cid)
            clip.created_at = clip.reviewed_at = NOW - timedelta(days=30)
    p.cleanup(now=NOW)
    assert not shared.exists()  # beide alt -> blockieren sich nicht gegenseitig
    assert fresh.exists()  # noch eingeplanter Clip braucht die Datei
    assert "gelöscht" not in (_clip_row(c).error or "") and _clip_row(d).status == "scheduled"


# --------------------------------------------------------------------------- Reddit-Repost: Credit + Regeln


def _reddit_linked() -> object:
    return make_cand(
        "1gx9abc",
        source=SourceKind.reddit_post,
        creator="shroud",
        handle="shroud",
        platform="Twitch",
        extra={
            "author": "clipposter",
            "linked_url": "https://www.twitch.tv/shroud/clip/FunnyBigLlamaPogChamp",
            "linked_platform": "Twitch",
            "via": "u/clipposter",
            "via_url": "https://reddit.com/u/clipposter",
        },
    )


def test_credit_line_names_reddit_poster_via():
    c = _reddit_linked()
    assert text.credit_line(c) == "🎥 Clip: shroud on Twitch – twitch.tv/shroud (via u/clipposter on Reddit)"
    # YouTube-Kandidaten nutzen extra.via für ihre Herkunft ("rss") -> kein "via" im Credit
    yt = make_cand("v", source=SourceKind.youtube_video, creator="GTA Guy", handle="gtaguy", platform="YouTube",
                   extra={"channel_id": "UC1", "via": "rss"})
    assert "via" not in text.credit_line(yt)


def test_creator_keys_include_reddit_poster():
    keys = creator_keys(_reddit_linked())
    assert ("twitch", "shroud") in keys and ("reddit", "clipposter") in keys
    assert ("twitch", "clipposter") not in keys  # der Reposter ist kein Twitch-Streamer


def test_block_rule_for_reddit_reposter_still_applies(settings, db, monkeypatch):
    MediaFakes(monkeypatch)
    with dbmod.session_scope() as s:
        s.add(CreatorRule(platform_label="Reddit", handle="u/clipposter", rule="block", note="Spam-Reposter"))
    c = _reddit_linked()
    p = make_pipeline(settings, sources=[FakeSource("reddit", [c])])
    p.discover(now=NOW)
    with dbmod.session_scope() as s:
        row = s.exec(select(Candidate).where(Candidate.key == c.key)).one()
    assert row.status == "skipped" and "Block-Liste" in row.status_reason
