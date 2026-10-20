"""Audio: Schnitt-Audio + synthetische SFX (keine Lizenzprobleme) + optionale Musik mit Ducking
+ Loudness-Normalisierung (zweistufiges loudnorm auf render.target_lufs, True Peak -1 dB)."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from wdvi.config import Settings


def synth_sfx(name: str, sr: int = 48000) -> np.ndarray:
    """float32 mono in [-1, 1]: pop, whoosh, boom, ding, riser, record_scratch."""
    raise NotImplementedError


def build_audio(cut_audio: Path, sfx_events: list[tuple[float, str]], settings: Settings, out_wav: Path,
                music_track: Path | None = None) -> Path:
    """cut_audio = Ton des bereits geschnittenen Clips (Ausgabezeit). sfx_events = (Ausgabezeit, Name).
    Ergebnis: 48 kHz Stereo WAV, normalisiert."""
    raise NotImplementedError
