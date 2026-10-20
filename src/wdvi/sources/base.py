"""Gemeinsame Schnittstelle aller Quellen."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from wdvi.config import Settings
from wdvi.schemas import CandidateIn


class SourceError(RuntimeError):
    """Quelle vorübergehend nicht erreichbar / falsch konfiguriert (wird geloggt, Job läuft weiter)."""


class Source(Protocol):
    name: str  # "twitch" | "kick" | "youtube" | "reddit"

    def is_configured(self) -> bool:
        """True, wenn alle nötigen Secrets/Configs vorhanden sind."""
        ...

    def discover(self, now: datetime) -> list[CandidateIn]:
        """Neue Kandidaten holen. Darf Duplikate liefern (DB dedupliziert über CandidateIn.key).

        Muss schnell sein (nur Metadaten, keine Downloads). Wirft SourceError bei Problemen.
        """
        ...


def build_sources(settings: Settings) -> list[Source]:
    """Alle in config aktivierten Quellen instanziieren (Reihenfolge egal)."""
    from wdvi.sources.kick import KickSource
    from wdvi.sources.reddit import RedditSource
    from wdvi.sources.twitch import TwitchSource
    from wdvi.sources.youtube import YouTubeSource

    cfg = settings.config.sources
    out: list[Source] = []
    if cfg.twitch.enabled:
        out.append(TwitchSource(settings))
    if cfg.kick.enabled:
        out.append(KickSource(settings))
    if cfg.youtube.enabled:
        out.append(YouTubeSource(settings))
    if cfg.reddit.enabled:
        out.append(RedditSource(settings))
    return out
