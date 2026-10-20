"""Musik-Erkennung (AudioSet-Tagger, optional) + Sprachanteil.

Musik im Hintergrund ist das größte Copyright-Risiko für Clip-Kanäle (Labels nutzen Content ID).
"""

from __future__ import annotations

import logging
import math
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

from wdvi.config import Settings
from wdvi.media import _ff
from wdvi.schemas import Transcript

log = logging.getLogger(__name__)

MODEL_ID = "MIT/ast-finetuned-audioset-10-10-0.4593"
WINDOW_S = 10.0
MAX_WINDOWS = 6
RETRY_AFTER_S = 3600.0  # nach fehlgeschlagenem Modell-Laden (z. B. offline) erst wieder nach 1 h versuchen

# AudioSet-Klassen, die Musik bedeuten (zusätzlich zählt jedes Label mit "music" im Namen)
MUSIC_LABELS = frozenset({
    "Music", "Song", "Singing", "Rapping", "Choir",
    "Pop music", "Hip hop music", "Rock music", "Electronic music", "Background music", "Theme music",
    "Soundtrack music", "Video game music", "Dance music", "Jingle (music)", "Jingle", "Lullaby",
    "Rhythm and blues", "Soul music", "Reggae", "Country", "Funk", "Disco", "Jazz", "Blues", "Folk music",
    "Classical music", "Opera", "Heavy metal", "Punk rock", "Grunge", "Progressive rock", "Rock and roll",
    "Psychedelic rock", "Electronic dance music", "House music", "Techno", "Dubstep", "Drum and bass",
    "Electronica", "Trance music", "Ambient music", "Salsa music", "Flamenco", "Ska", "Swing music",
    "Bluegrass", "Gospel music", "Christian music", "Christmas music", "Carnatic music", "Afrobeat",
    "Music of Latin America", "Music of Africa", "Music of Asia", "Middle Eastern music", "Vocal music",
    "A capella", "Independent music", "New-age music", "Wedding music", "Happy music", "Sad music",
    "Tender music", "Exciting music", "Angry music", "Scary music", "Funny music",
})

_lock = threading.Lock()
_pipe: Any | None = None
_unavailable = False  # torch/transformers nicht installiert -> nie wieder versuchen
_failed_at: float | None = None


def is_music_label(label: str) -> bool:
    return label in MUSIC_LABELS or "music" in label.lower()


def _get_pipeline() -> Any | None:
    """AudioSet-Tagger einmalig laden (prozessweit). None, wenn nicht verfügbar."""
    global _pipe, _unavailable, _failed_at
    with _lock:
        if _pipe is not None:
            return _pipe
        if _unavailable or (_failed_at is not None and time.monotonic() - _failed_at < RETRY_AFTER_S):
            return None
        try:
            import torch
            from transformers import pipeline
        except ImportError:
            _unavailable = True
            log.info("Music detection disabled: torch/transformers not installed (pip extra 'music')")
            return None
        try:
            _pipe = pipeline("audio-classification", model=MODEL_ID,
                             device=0 if torch.cuda.is_available() else -1, top_k=None)
            log.info("Loaded music tagger %s", MODEL_ID)
        except Exception as e:
            _failed_at = time.monotonic()
            log.warning("Music detection unavailable: could not load %s (%s: %s)", MODEL_ID, type(e).__name__, e)
            return None
        return _pipe


def _score_window(pipe: Any, window: np.ndarray, sr: int) -> dict[str, float]:
    """Alle AudioSet-Wahrscheinlichkeiten eines Fensters.

    AST ist multi-label (sigmoid) trainiert; die Pipeline-Nachbearbeitung nutzt je nach
    transformers-Version softmax, daher rechnen wir die Logits selbst um.
    """
    import torch

    inputs = pipe.feature_extractor(window, sampling_rate=sr, return_tensors="pt")
    inputs = {k: v.to(pipe.device) for k, v in inputs.items()}
    with torch.inference_mode():
        logits = pipe.model(**inputs).logits[0]
    probs = torch.sigmoid(logits.float()).cpu().numpy()
    id2label = pipe.model.config.id2label
    return {str(id2label[i]): float(p) for i, p in enumerate(probs)}


def _windows(audio: np.ndarray, sr: int, window_s: float = WINDOW_S, max_n: int = MAX_WINDOWS) -> list[np.ndarray]:
    """Bis zu max_n Fenster à window_s, gleichmäßig über den Clip verteilt (kurzer Clip = ein Fenster)."""
    win = int(window_s * sr)
    if audio.size <= win:
        return [audio]
    n = min(max_n, math.ceil(audio.size / win))
    starts = np.linspace(0, audio.size - win, n).astype(int)
    return [audio[s:s + win] for s in starts]


def music_probability(path: str | Path, settings: Settings) -> float | None:
    """Max. Wahrscheinlichkeit der AudioSet-Klasse "Music" (inkl. Unterklassen wie Song/Pop music)
    über 10-s-Fenster. None, wenn torch/transformers bzw. das Modell nicht verfügbar sind."""
    pipe = _get_pipeline()
    if pipe is None:
        return None
    try:
        audio = _ff.decode_audio(path, _ff.SAMPLE_RATE)
        if audio.size < _ff.SAMPLE_RATE // 2:
            return 0.0  # kein Audio -> keine Musik
        best = 0.0
        for w in _windows(audio, _ff.SAMPLE_RATE):
            if float(np.sqrt(np.mean(w.astype(np.float64) ** 2))) < 1e-4:  # Stille überspringen
                continue
            scores = _score_window(pipe, w, _ff.SAMPLE_RATE)
            best = max(best, max((s for lbl, s in scores.items() if is_music_label(lbl)), default=0.0))
        return round(float(best), 4)
    except Exception as e:
        log.warning("Music detection failed for %s (%s: %s)", Path(path).name, type(e).__name__, e)
        return None


def speech_ratio(transcript: Transcript | None, duration: float) -> float:
    """Anteil der Dauer, der von Wörtern abgedeckt ist (0..1)."""
    if transcript is None or duration <= 0:
        return 0.0
    spans = sorted((max(0.0, w.start), min(duration, w.end)) for w in transcript.words() if w.end > w.start)
    covered, cur_s, cur_e = 0.0, None, None
    for s, e in spans:
        if e <= s:
            continue
        if cur_e is None or s > cur_e:
            if cur_e is not None:
                covered += cur_e - cur_s
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    if cur_e is not None:
        covered += cur_e - cur_s
    return round(min(1.0, max(0.0, covered / duration)), 4)
