"""Musik-Erkennung (AudioSet-Tagger, optional) + Sprachanteil.

Musik im Hintergrund ist das größte Copyright-Risiko für Clip-Kanäle (Labels nutzen Content ID).
"""

from __future__ import annotations

from pathlib import Path

from wdvi.config import Settings
from wdvi.schemas import Transcript


def music_probability(path: str | Path, settings: Settings) -> float | None:
    """Max. Wahrscheinlichkeit der AudioSet-Klasse "Music" (inkl. Unterklassen wie Song/Pop music)
    über 10-s-Fenster. None, wenn torch/transformers bzw. das Modell nicht verfügbar sind."""
    raise NotImplementedError


def speech_ratio(transcript: Transcript | None, duration: float) -> float:
    """Anteil der Dauer, der von Wörtern abgedeckt ist (0..1)."""
    raise NotImplementedError
