"""Downloads via yt-dlp (Python-API)."""

from __future__ import annotations

from pathlib import Path

from wdvi.config import Settings
from wdvi.schemas import CandidateIn


class DownloadError(RuntimeError):
    pass


def download(c: CandidateIn, dest_dir: Path, settings: Settings) -> Path:
    """Lädt den Kandidaten als MP4 (H.264/AAC bevorzugt, max 1080p) nach dest_dir/<sicherer-key>.mp4.

    - youtube_moment: nur den Abschnitt [start_s, end_s] (yt-dlp download_ranges, force_keyframes_at_cuts)
    - existiert die Datei schon: nicht erneut laden
    - wirft DownloadError mit verständlicher Meldung
    """
    raise NotImplementedError
