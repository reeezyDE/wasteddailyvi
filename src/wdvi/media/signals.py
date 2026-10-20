"""Audio-/Engagement-Signale."""

from __future__ import annotations

from pathlib import Path


def loudness_peaks(path: str | Path, top_k: int = 5, min_gap_s: float = 2.0) -> list[float]:
    """Zeitpunkte (s) der lautesten Momente (RMS über ~0.5-s-Fenster, relativ zum Median)."""
    raise NotImplementedError


def heatmap_peaks(heatmap: list[dict] | list, top_k: int = 5, min_gap_s: float = 30.0) -> list[tuple[float, float, float]]:
    """Aus yt-dlp-heatmap ([{start_time,end_time,value}] oder HeatPoint) die stärksten Spitzen
    als (start, end, value), zeitlich getrennt."""
    raise NotImplementedError
