"""Billige Vorbewertung (ohne LLM), um die besten Kandidaten zuerst zu verarbeiten.

Heuristik (dokumentiert, damit man sie im Dashboard nachvollziehen kann):

    velocity = metric / max(Stunden seit created_at, 0.5)
    metric   = view_count            (Twitch, Kick, YouTube-Video)
             = Reddit-Score * 20     (grobe Umrechnung Upvotes -> Views)
    score    = log10(1 + velocity) * Quellgewicht   (Twitch 1.0, Kick 0.9, Reddit 1.0, YouTube-Video 0.5)
             + 0.3, falls Twitch is_featured

    youtube_moment: moment_score/25 (Claudes 0–100 -> 0–4) + 0.25 * log10(1 + velocity des Elternvideos)

Typische Werte: 10 000 Views in 2 h -> ~3.7; 500 Upvotes in 3 h -> ~3.5; Moment mit Score 80 -> ~3.2–4.2.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

from wdvi.schemas import CandidateIn, SourceKind

SOURCE_WEIGHT: dict[SourceKind, float] = {
    SourceKind.twitch_clip: 1.0,
    SourceKind.kick_clip: 0.9,
    SourceKind.reddit_post: 1.0,
    SourceKind.youtube_video: 0.5,
    SourceKind.youtube_moment: 0.25,  # nur für den Velocity-Anteil
}
REDDIT_SCORE_FACTOR = 20.0
FEATURED_BONUS = 0.3
MIN_AGE_H = 0.5


def _num(value: object) -> float:
    """Beliebiger Wert -> endliche Zahl >= 0 (sonst 0)."""
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return f if math.isfinite(f) and f > 0 else 0.0


def _age_hours(c: CandidateIn, now: datetime) -> float:
    created = c.created_at if c.created_at.tzinfo else c.created_at.replace(tzinfo=UTC)
    now = now if now.tzinfo else now.replace(tzinfo=UTC)
    hours = (now - created).total_seconds() / 3600.0
    return max(hours, MIN_AGE_H) if math.isfinite(hours) else MIN_AGE_H


def prescore(c: CandidateIn, now: datetime) -> float:
    """Höher = besser. Basis: Aufruf-/Upvote-Geschwindigkeit (pro Stunde seit created_at), log-skaliert,
    je Quelle normalisiert, Bonus für Twitch is_featured / youtube_moment.moment_score.
    Muss für alle SourceKinds definiert sein und darf nie werfen (fehlende Werte -> 0)."""
    try:
        hours = _age_hours(c, now)
        if c.source == SourceKind.reddit_post:
            metric = _num((c.engagement or {}).get("score")) * REDDIT_SCORE_FACTOR
        else:
            metric = _num(c.view_count)
        velocity_part = math.log10(1.0 + metric / hours)
        extra = c.extra or {}
        score = velocity_part * SOURCE_WEIGHT.get(c.source, 0.5)
        if c.source == SourceKind.youtube_moment:
            score += min(_num(extra.get("moment_score")), 100.0) / 25.0
        if extra.get("is_featured") is True:
            score += FEATURED_BONUS
        return round(score, 4) if math.isfinite(score) else 0.0
    except Exception:  # Vorbewertung darf die Discovery nie abbrechen
        return 0.0


def same_twitch_moment(a: CandidateIn, b: CandidateIn, tolerance_s: float = 20.0) -> bool:
    """Zwei Twitch-Clips zeigen denselben Moment (gleicher broadcaster + video_id, vod_offset nah)."""
    try:
        if a.source != SourceKind.twitch_clip or b.source != SourceKind.twitch_clip:
            return False
        if a.source_id == b.source_id:
            return True
        ea, eb = a.extra or {}, b.extra or {}
        bid_a, bid_b = str(ea.get("broadcaster_id") or ""), str(eb.get("broadcaster_id") or "")
        vid_a, vid_b = str(ea.get("video_id") or ""), str(eb.get("video_id") or "")
        if not bid_a or bid_a != bid_b or not vid_a or vid_a != vid_b:
            return False
        off_a, off_b = ea.get("vod_offset"), eb.get("vod_offset")
        if off_a is None or off_b is None:
            return False
        off_a, off_b = float(off_a), float(off_b)
        if abs(off_a - off_b) <= tolerance_s:
            return True
        # unterschiedlich lange Clips: Überlappung >= 50 % des kürzeren Clips
        dur_a, dur_b = _num(a.duration_s), _num(b.duration_s)
        if dur_a <= 0 or dur_b <= 0:
            return False
        overlap = min(off_a + dur_a, off_b + dur_b) - max(off_a, off_b)
        return overlap >= 0.5 * min(dur_a, dur_b)
    except (TypeError, ValueError):
        return False
