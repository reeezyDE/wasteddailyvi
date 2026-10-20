"""Fasst alle Media-Analysen zu einer MediaAnalysis zusammen."""

from __future__ import annotations

from pathlib import Path

from wdvi.config import Settings
from wdvi.media.faces import FaceDetector
from wdvi.media.transcribe import Transcriber
from wdvi.schemas import MediaAnalysis


def analyze(path: str | Path, work_dir: Path, settings: Settings, transcriber: Transcriber,
            detector: FaceDetector, language: str | None = None) -> MediaAnalysis:
    """probe -> transcribe -> music_probability -> loudness_peaks -> analyze_layout -> sample_frames
    (llm.max_frames, Gesichter je Frame) -> phashes. Fehler in optionalen Schritten (Musik) loggen,
    nicht werfen."""
    raise NotImplementedError
