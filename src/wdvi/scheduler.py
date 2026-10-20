"""APScheduler-Jobs (BackgroundScheduler, je Job max_instances=1, coalesce)."""

from __future__ import annotations

from wdvi.config import Settings
from wdvi.pipeline import Pipeline


def start_scheduler(pipeline: Pipeline, settings: Settings):
    """discover je Quelle (poll_minutes), process_cycle (alle 2 min), schedule_approved + publish_due
    (jede Minute), fetch_stats (alle 3 h), cleanup (täglich 04:00). Gibt den gestarteten Scheduler zurück."""
    raise NotImplementedError
