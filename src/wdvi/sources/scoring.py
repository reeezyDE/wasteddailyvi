"""Billige Vorbewertung (ohne LLM), um die besten Kandidaten zuerst zu verarbeiten."""

from __future__ import annotations

from datetime import datetime

from wdvi.schemas import CandidateIn


def prescore(c: CandidateIn, now: datetime) -> float:
    """Höher = besser. Basis: Aufruf-/Upvote-Geschwindigkeit (pro Stunde seit created_at), log-skaliert,
    je Quelle normalisiert, Bonus für Twitch is_featured / youtube_moment.moment_score.
    Muss für alle SourceKinds definiert sein und darf nie werfen (fehlende Werte -> 0)."""
    raise NotImplementedError


def same_twitch_moment(a: CandidateIn, b: CandidateIn, tolerance_s: float = 20.0) -> bool:
    """Zwei Twitch-Clips zeigen denselben Moment (gleicher broadcaster + video_id, vod_offset nah)."""
    raise NotImplementedError
