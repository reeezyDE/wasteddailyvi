"""Scheduler: Job-Registrierung, Intervalle, keine parallelen Zyklen."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from wdvi import scheduler


class FakePipeline:
    def __init__(self) -> None:
        self.sources = {"twitch": object(), "reddit": object()}
        self.calls: list[tuple[str, dict]] = []

    def _rec(self, name):
        def f(*a, **kw):
            self.calls.append((name, kw))
            if name == "publish_due":
                raise RuntimeError("boom")
            return 0

        return f

    def __getattr__(self, name):
        if name in (
            "discover",
            "process_cycle",
            "schedule_approved",
            "publish_due",
            "fetch_stats",
            "cleanup",
        ):
            return self._rec(name)
        raise AttributeError(name)


def test_jobs_registered(settings):
    settings.config.sources.twitch.poll_minutes = 7
    now = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    sched = scheduler.build_scheduler(FakePipeline(), settings, now=now)
    jobs = {j.id: j for j in sched.get_jobs()}
    assert set(jobs) == {
        "discover:twitch",
        "discover:reddit",
        "process",
        "schedule",
        "publish",
        "stats",
        "cleanup",
    }
    assert jobs["discover:twitch"].trigger.interval == timedelta(minutes=7)
    assert jobs["discover:reddit"].trigger.interval == timedelta(
        minutes=settings.config.sources.reddit.poll_minutes
    )
    assert jobs["discover:twitch"].kwargs == {"source_name": "twitch"}
    assert jobs["process"].trigger.interval == timedelta(minutes=2)
    assert jobs["schedule"].trigger.interval == timedelta(minutes=1)
    assert jobs["publish"].trigger.interval == timedelta(minutes=1)
    assert jobs["stats"].trigger.interval == timedelta(hours=3)
    assert "hour='4'" in str(jobs["cleanup"].trigger)
    for j in jobs.values():
        assert j.max_instances == 1 and j.coalesce is True and j.misfire_grace_time >= 60
    # Discovery startet bald nach dem Start, versetzt je Quelle
    starts = sorted(jobs[k].next_run_time for k in ("discover:reddit", "discover:twitch"))
    assert starts[0] < starts[1] < now + timedelta(minutes=2)


def test_no_stats_job_without_feedback(settings):
    settings.config.feedback.enabled = False
    sched = scheduler.build_scheduler(FakePipeline(), settings)
    assert "stats" not in {j.id for j in sched.get_jobs()}


def test_job_wrappers_call_pipeline_and_swallow_errors(settings, caplog):
    fake = FakePipeline()
    sched = scheduler.build_scheduler(fake, settings)
    jobs = {j.id: j for j in sched.get_jobs()}
    jobs["discover:twitch"].func(**jobs["discover:twitch"].kwargs)
    jobs["process"].func()
    with caplog.at_level(logging.ERROR):
        assert jobs["publish"].func() is None  # Exception wird geloggt, nicht geworfen
    assert ("discover", {"source_name": "twitch"}) in fake.calls
    assert ("process_cycle", {}) in fake.calls
    assert any("publish" in r.getMessage() for r in caplog.records)


def test_start_scheduler_runs_and_stops(settings):
    sched = scheduler.start_scheduler(FakePipeline(), settings)
    try:
        assert sched.running
        assert str(sched.timezone) == "UTC"
    finally:
        sched.shutdown(wait=False)


def test_skip_noise_filter():
    f = scheduler._SkipNoise()
    rec = logging.LogRecord(
        "apscheduler.scheduler",
        logging.WARNING,
        __file__,
        1,
        'Execution of job "process" skipped: maximum number of running instances reached (1)',
        None,
        None,
    )
    assert f.filter(rec) is False
    ok = logging.LogRecord(
        "apscheduler.scheduler", logging.WARNING, __file__, 1, "Run time of job was missed", None, None
    )
    assert f.filter(ok) is True
