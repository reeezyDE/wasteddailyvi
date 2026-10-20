"""Audio-/Engagement-Signale."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from wdvi.media import _ff

WINDOW_S = 0.5
HOP_S = 0.25
PEAK_ABOVE_MEDIAN_DB = 6.0


def rms_db(audio: np.ndarray, sr: int, window_s: float = WINDOW_S, hop_s: float = HOP_S) -> tuple[np.ndarray, np.ndarray]:
    """RMS-Pegel in dB je Fenster -> (Fenstermitten in s, dB). Leer, wenn das Audio kürzer als ein Fenster ist."""
    win, hop = int(window_s * sr), max(1, int(hop_s * sr))
    if win <= 0 or audio.size < win:
        return np.zeros(0), np.zeros(0)
    frames = np.lib.stride_tricks.sliding_window_view(audio.astype(np.float64, copy=False), win)[::hop]
    rms = np.sqrt(np.mean(frames * frames, axis=1))
    db = 20.0 * np.log10(rms + 1e-10)
    centers = (np.arange(len(db)) * hop + win / 2) / sr
    return centers, db


def _pick_separated(items: list[tuple[float, float]], top_k: int, min_gap_s: float) -> list[tuple[float, float]]:
    """Greedy nach Stärke absteigend; nimmt nur Punkte mit Abstand >= min_gap_s zu schon gewählten."""
    chosen: list[tuple[float, float]] = []
    for t, v in sorted(items, key=lambda x: -x[1]):
        if all(abs(t - ct) >= min_gap_s for ct, _ in chosen):
            chosen.append((t, v))
            if len(chosen) >= top_k:
                break
    return chosen


def loudness_peaks(path: str | Path, top_k: int = 5, min_gap_s: float = 2.0) -> list[float]:
    """Zeitpunkte (s) der lautesten Momente (RMS über ~0.5-s-Fenster, relativ zum Median)."""
    if top_k <= 0:
        return []
    audio = _ff.decode_audio(path, _ff.SAMPLE_RATE)
    centers, db = rms_db(audio, _ff.SAMPLE_RATE)
    if db.size == 0:
        return []
    threshold = float(np.median(db)) + PEAK_ABOVE_MEDIAN_DB
    loud = [(float(t), float(v)) for t, v in zip(centers, db, strict=True) if v > threshold and v > -60.0]
    return [round(t, 2) for t, _ in _pick_separated(loud, top_k, min_gap_s)]


def _heat_fields(p) -> tuple[float, float, float] | None:
    """Liest (start, end, value) aus dict oder Objekt (start/end/value bzw. start_time/end_time/value)."""
    def get(name: str):
        return p.get(name) if isinstance(p, dict) else getattr(p, name, None)

    start = get("start_time")
    if start is None:
        start = get("start")
    end = get("end_time")
    if end is None:
        end = get("end")
    value = get("value")
    try:
        s, v = float(start), float(value)
        e = float(end) if end is not None else s
    except (TypeError, ValueError):
        return None
    return s, max(s, e), v


def heatmap_peaks(heatmap: list[dict] | list, top_k: int = 5, min_gap_s: float = 30.0) -> list[tuple[float, float, float]]:
    """Aus yt-dlp-heatmap ([{start_time,end_time,value}] oder HeatPoint) die stärksten Spitzen
    als (start, end, value), zeitlich getrennt."""
    if not heatmap or top_k <= 0:
        return []
    points = [f for f in (_heat_fields(p) for p in heatmap) if f is not None]
    by_center = {(s + e) / 2: (s, e, v) for s, e, v in points}
    picked = _pick_separated([(c, v) for c, (_, _, v) in by_center.items()], top_k, min_gap_s)
    return [by_center[c] for c, _ in picked]
