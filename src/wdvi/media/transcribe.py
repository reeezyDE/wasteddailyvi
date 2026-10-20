"""Transkription mit faster-whisper (Wort-Zeitstempel). Modell wird lazy und nur einmal geladen."""

from __future__ import annotations

from pathlib import Path

from wdvi.config import Settings
from wdvi.schemas import Transcript


class Transcriber:
    def __init__(self, settings: Settings) -> None:
        raise NotImplementedError

    def transcribe(self, path: str | Path, language: str | None = None) -> Transcript:
        """word_timestamps=True, vad_filter=True. Leere Transkripte sind erlaubt (Gameplay ohne Sprache)."""
        raise NotImplementedError
