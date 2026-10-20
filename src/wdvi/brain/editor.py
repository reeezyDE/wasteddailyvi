"""Claude als Viral-Redakteur: bewertet einen Clip und plant Schnitt + Verpackung."""

from __future__ import annotations

from wdvi.brain.claude import LLMClient
from wdvi.config import Settings
from wdvi.schemas import CandidateIn, EditPlan, MediaAnalysis, MomentPick, PerformanceExample
from wdvi.sources.youtube import VideoContext


def plan_clip(llm: LLMClient, c: CandidateIn, analysis: MediaAnalysis, examples: list[PerformanceExample],
              settings: Settings) -> EditPlan:
    """Baut den Prompt (Metadaten, Velocity, Transkript mit Zeitstempeln, Lautstärke-Peaks,
    Musik-Wahrscheinlichkeit, Layout-Vorschlag, Frames mit Zeitangabe, Performance-Beispiele),
    ruft llm.complete_json(SYSTEM_EDITOR, ..., EditPlan) und gibt sanitize_plan(...) zurück."""
    raise NotImplementedError


def sanitize_plan(plan: EditPlan, analysis: MediaAnalysis, settings: Settings) -> EditPlan:
    """Erzwingt harte Regeln, egal was Claude sagt:
    - trim in [0, duration], trim_end > trim_start; remove_ranges innerhalb trim, sortiert, ohne Überlappung,
      jede Lücke >= 0.25 s; behaltene Gesamtdauer <= max_output_duration_s (sonst trim_end kürzen)
    - zu kurz (< min_output_duration_s) -> keep=False
    - zooms/emphasis auf trim begrenzen, scale in [1.0, 1.5], max 1 Emoji pro Wort
    - is_leak_or_unreleased & leak_policy=reject -> keep=False; is_unsafe -> keep=False
    - virality_score < min_virality_score -> keep=False
    - Titel: YouTube <= 100 Zeichen, keine "#shorts"-Duplikate; Hashtags ohne '#', dedupliziert, max 8
    - hook_text max 6 Wörter / 32 Zeichen"""
    raise NotImplementedError


def mine_moments(llm: LLMClient, video: CandidateIn, ctx: VideoContext, settings: Settings) -> list[MomentPick]:
    """Aus Transkript + Heatmap + Kapiteln die besten Ausschnitte (je 15–55 s) wählen
    (max sources.youtube.moments_per_video), Grenzen an Satzgrenzen."""
    raise NotImplementedError
