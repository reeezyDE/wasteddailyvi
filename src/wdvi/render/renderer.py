"""Einstiegspunkt Rendering: Quellclip + EditPlan -> fertiges 9:16-MP4 + Thumbnail."""

from __future__ import annotations

from pathlib import Path

from wdvi.config import Settings
from wdvi.schemas import CandidateIn, EditPlan, MediaAnalysis, RenderResult


class RenderError(RuntimeError):
    pass


def render_clip(source: Path, analysis: MediaAnalysis, plan: EditPlan, c: CandidateIn, settings: Settings,
                work_dir: Path, out_dir: Path) -> RenderResult:
    """1. timemap = build_timemap(plan)
    2. ffmpeg: Segmente schneiden + concat (Video CFR render.fps, Audio mit 10-ms-Crossfades) -> cut.mp4
    3. Captions-ASS aus analysis.transcript (Wörter) + plan.highlight_words
    4. Overlays: Hook (0..hook_seconds, oben), Credit (ganze Dauer, oben links, wenn show_credit),
       Wasserzeichen (unten rechts klein), Emojis an emphasis-Zeitpunkten (0.7 s, über den Captions)
    5. Audio: SFX aus emphasis.sfx + automatisch "whoosh" am Start jedes Zooms (wenn render.sfx)
    6. compose_video -> out_dir/<candidate-key-sicher>.mp4, Thumbnail (JPEG) aus dem stärksten Moment
    Wirft RenderError mit ffmpeg-stderr-Auszug."""
    raise NotImplementedError
