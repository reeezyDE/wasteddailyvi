"""Fasst alle Media-Analysen zu einer MediaAnalysis zusammen."""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

import cv2

from wdvi.config import Settings
from wdvi.media import audio_tags, dedup, frames, signals
from wdvi.media import faces as faces_mod
from wdvi.media import probe as probe_mod
from wdvi.media.faces import FaceDetector
from wdvi.media.transcribe import Transcriber
from wdvi.schemas import LayoutHint, MediaAnalysis, Transcript

log = logging.getLogger(__name__)

LAYOUT_SAMPLES = 16


def _frame_prefix(path: Path) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", path.stem)[:80] or "clip"


def analyze(path: str | Path, work_dir: Path, settings: Settings, transcriber: Transcriber,
            detector: FaceDetector, language: str | None = None) -> MediaAnalysis:
    """probe -> transcribe -> music_probability -> loudness_peaks -> analyze_layout -> sample_frames
    (llm.max_frames, Gesichter je Frame) -> phashes. Fehler in optionalen Schritten (Musik) loggen,
    nicht werfen."""
    path = Path(path)
    t0 = time.monotonic()
    if transcriber is None:
        transcriber = Transcriber(settings)
    if detector is None:
        detector = FaceDetector(settings)

    info = probe_mod.probe(path)  # Pflicht: ohne lesbares Video geht nichts

    # Sprache: Pflicht, sobald Audio da ist (Captions!) -> Fehler werden geworfen
    transcript: Transcript | None = None
    if info.has_audio:
        transcript = transcriber.transcribe(path, language=language)

    music_prob: float | None = None
    try:
        music_prob = audio_tags.music_probability(path, settings) if info.has_audio else 0.0
    except Exception as e:
        log.warning("Music detection failed for %s: %s", path.name, e)

    peaks: list[float] = []
    if info.has_audio:
        try:
            peaks = signals.loudness_peaks(path)
        except Exception as e:
            log.warning("Loudness analysis failed for %s: %s", path.name, e)

    try:
        layout = faces_mod.analyze_layout(path, info, detector, samples=LAYOUT_SAMPLES)
    except Exception as e:
        log.warning("Layout analysis failed for %s (%s) - using gameplay_fit", path.name, e)
        layout = LayoutHint(kind="vertical_native" if info.height >= info.width else "gameplay_fit",
                            confidence=0.0)

    llm = settings.config.llm
    # halb gleichmäßig, halb an den Lautstärke-Peaks: Claude soll Einschlag/Payoff sehen (Zoom, Schnitt)
    samples = frames.sample_frames(path, Path(work_dir) / "frames", n=llm.max_frames, width=llm.frame_width,
                                   end=info.duration, prefix=_frame_prefix(path), focus=peaks)
    for fs in samples:
        try:
            img = cv2.imread(fs.path)
            fs.faces = detector.detect(img) if img is not None else []
        except Exception as e:
            log.warning("Face detection failed on %s: %s", fs.path, e)

    hashes: list[str] = []
    try:
        hashes = dedup.phashes(path)
    except Exception as e:
        log.warning("pHash failed for %s: %s", path.name, e)

    result = MediaAnalysis(
        info=info,
        transcript=transcript,
        music_prob=music_prob,
        speech_ratio=audio_tags.speech_ratio(transcript, info.duration),
        loudness_peaks=peaks,
        frames=samples,
        layout=layout,
        phashes=hashes,
    )
    log.info("Analyzed %s in %.1fs: %.1fs %dx%d, lang=%s, speech=%.0f%%, music=%s, layout=%s (%.2f), %d frames",
             path.name, time.monotonic() - t0, info.duration, info.width, info.height,
             transcript.language if transcript else "-", result.speech_ratio * 100,
             "n/a" if music_prob is None else f"{music_prob:.2f}", layout.kind, layout.confidence, len(samples))
    return result
