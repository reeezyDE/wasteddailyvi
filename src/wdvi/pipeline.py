"""Orchestrierung: Discover -> Verarbeiten (Download, Analyse, Claude, Render) -> Review -> Slots -> Posten -> Stats.

Alle Methoden sind synchron und idempotent; der Scheduler ruft sie periodisch auf. Schwere Arbeit
(Whisper/Render) läuft seriell in `process_cycle` (ein Worker, GPU).
"""

from __future__ import annotations

from datetime import datetime

from wdvi.config import Settings


class Pipeline:
    def __init__(self, settings: Settings, *, llm=None, sources=None, publishers=None,
                 transcriber=None, detector=None) -> None:
        """Abhängigkeiten injizierbar (Tests); sonst lazy aus settings gebaut."""
        raise NotImplementedError

    # -- discover
    def discover(self, source_name: str | None = None, now: datetime | None = None) -> int:
        """Quelle(n) abfragen, neue Kandidaten upserten (key unique; view_count/engagement aktualisieren),
        prescore setzen, CreatorRule block -> status skipped. Gibt Anzahl neuer Kandidaten zurück.
        Schreibt JobRun "discover:<name>"."""
        raise NotImplementedError

    # -- process
    def process_cycle(self, now: datetime | None = None) -> int:
        """Bis zu filters.candidates_per_cycle beste 'new'-Kandidaten (jünger als max_candidate_age_hours,
        günstige Filter bestanden, LLM-Rate-Limit beachtet) verarbeiten. youtube_video -> mine_youtube.
        Gibt Anzahl erzeugter Clips zurück."""
        raise NotImplementedError

    def process_candidate(self, candidate_id: int) -> int | None:
        """download -> analyze -> Sprach-/Musik-/Dedup-Filter -> plan_clip -> render_clip -> Clip
        (pending_review; bei require_approval=False direkt approved). Gibt Clip-ID oder None zurück.
        Jeder Ablehnungsgrund landet in Candidate.status_reason."""
        raise NotImplementedError

    def mine_youtube(self, candidate_id: int) -> int:
        """fetch_video_context + mine_moments -> youtube_moment-Kandidaten anlegen; Eltern -> 'mined'."""
        raise NotImplementedError

    # -- review (vom Dashboard aufgerufen)
    def approve_clip(self, clip_id: int, platforms: list[str], titles: dict[str, str] | None = None,
                     descriptions: dict[str, str] | None = None) -> None:
        raise NotImplementedError

    def reject_clip(self, clip_id: int, reason: str | None = None) -> None:
        raise NotImplementedError

    # -- publish
    def schedule_approved(self, now: datetime | None = None) -> int:
        """Für approved-Clips je gewählter Plattform einen Post im nächsten Slot anlegen; Clip -> scheduled."""
        raise NotImplementedError

    def publish_due(self, now: datetime | None = None) -> int:
        """Fällige Posts hochladen (YouTube native_schedule: schon vorab, sobald geplant). Max 3 Versuche
        mit Backoff; Clip -> posted, wenn alle Posts durch sind."""
        raise NotImplementedError

    # -- feedback
    def fetch_stats(self, now: datetime | None = None) -> int:
        raise NotImplementedError

    def performance_examples(self, limit: int) -> list:
        """Beste und schlechteste Posts (nach Views) als PerformanceExample für den Prompt."""
        raise NotImplementedError

    # -- housekeeping
    def cleanup(self, now: datetime | None = None) -> None:
        """work_dir-Dateien > 2 Tage löschen; abgelehnte Clip-Videos > 7 Tage löschen."""
        raise NotImplementedError
