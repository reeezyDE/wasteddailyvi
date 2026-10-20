"""APScheduler-Jobs (BackgroundScheduler, je Job max_instances=1, coalesce)."""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from wdvi.config import Settings
from wdvi.pipeline import Pipeline

log = logging.getLogger(__name__)

PROCESS_MINUTES = 2
PUBLISH_MINUTES = 1
STATS_HOURS = 3
CLEANUP_HOUR_UTC = 4


class _SkipNoise(logging.Filter):
    """ "maximum number of running instances reached" ist bei langen Verarbeitungszyklen normal."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "maximum number of running instances reached" not in record.getMessage()


def _safe(fn: Callable[..., Any], name: str) -> Callable[..., Any]:
    """Job-Wrapper: Exceptions loggen statt den Scheduler-Thread zu stören (JobRun schreibt die Pipeline)."""

    @functools.wraps(fn)
    def run(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except Exception:
            log.exception("scheduled job %s failed", name)
            return None

    return run


def _quiet_apscheduler() -> None:
    logging.getLogger("apscheduler.executors.default").setLevel(
        logging.WARNING
    )  # "Running job ..." je Minute
    sched_log = logging.getLogger("apscheduler.scheduler")
    if sched_log.level < logging.WARNING:  # "Adding job tentatively …" je Job beim Start
        sched_log.setLevel(logging.WARNING)
    if not any(isinstance(f, _SkipNoise) for f in sched_log.filters):
        sched_log.addFilter(_SkipNoise())


def build_scheduler(pipeline: Pipeline, settings: Settings, now: datetime | None = None):
    """Scheduler mit allen Jobs (noch nicht gestartet) – getrennt, damit Tests die Jobs prüfen können."""
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    now = now or datetime.now(UTC)
    sched = BackgroundScheduler(
        timezone=UTC,
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 120},
    )
    once = {"coalesce": True, "max_instances": 1, "replace_existing": True}  # explizit je Job (nie parallel)

    # Discovery je aktivierter Quelle, leicht versetzt gestartet
    for i, name in enumerate(sorted(pipeline.sources)):
        src_cfg = getattr(settings.config.sources, name, None)
        minutes = max(1, int(getattr(src_cfg, "poll_minutes", 15) or 15))
        sched.add_job(
            _safe(pipeline.discover, f"discover:{name}"),
            IntervalTrigger(minutes=minutes, timezone=UTC),
            kwargs={"source_name": name},
            id=f"discover:{name}",
            name=f"discover:{name}",
            next_run_time=now + timedelta(seconds=15 + 20 * i),
            misfire_grace_time=120,
            **once,
        )

    # Verarbeitung: ein Zyklus zur Zeit (max_instances=1 + Lock in Pipeline.process_cycle)
    sched.add_job(
        _safe(pipeline.process_cycle, "process"),
        IntervalTrigger(minutes=PROCESS_MINUTES, timezone=UTC),
        id="process",
        name="process",
        next_run_time=now + timedelta(seconds=90),
        misfire_grace_time=300,
        **once,
    )
    sched.add_job(
        _safe(pipeline.schedule_approved, "schedule"),
        IntervalTrigger(minutes=PUBLISH_MINUTES, timezone=UTC),
        id="schedule",
        name="schedule",
        next_run_time=now + timedelta(seconds=30),
        misfire_grace_time=60,
        **once,
    )
    sched.add_job(
        _safe(pipeline.publish_due, "publish"),
        IntervalTrigger(minutes=PUBLISH_MINUTES, timezone=UTC),
        id="publish",
        name="publish",
        next_run_time=now + timedelta(seconds=45),
        misfire_grace_time=60,
        **once,
    )
    if settings.config.feedback.enabled:
        sched.add_job(
            _safe(pipeline.fetch_stats, "stats"),
            IntervalTrigger(hours=STATS_HOURS, timezone=UTC),
            id="stats",
            name="stats",
            next_run_time=now + timedelta(minutes=5),
            misfire_grace_time=1800,
            **once,
        )
    sched.add_job(
        _safe(pipeline.cleanup, "cleanup"),
        CronTrigger(hour=CLEANUP_HOUR_UTC, minute=0, timezone=UTC),
        id="cleanup",
        name="cleanup",
        misfire_grace_time=6 * 3600,
        **once,
    )
    return sched


def start_scheduler(pipeline: Pipeline, settings: Settings):
    """discover je Quelle (poll_minutes), process_cycle (alle 2 min), schedule_approved + publish_due
    (jede Minute), fetch_stats (alle 3 h), cleanup (täglich 04:00). Gibt den gestarteten Scheduler zurück."""
    _quiet_apscheduler()
    sched = build_scheduler(pipeline, settings)
    sched.start()
    log.info("scheduler started: %s", ", ".join(j.id for j in sched.get_jobs()))
    return sched
