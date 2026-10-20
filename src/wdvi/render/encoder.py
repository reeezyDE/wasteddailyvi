"""Encoder-Auswahl (NVENC, wenn verfügbar und funktionsfähig; sonst libx264)."""

from __future__ import annotations

from wdvi.config import RenderConfig


def detect_encoder(pref: str = "auto") -> str:
    """'h264_nvenc' oder 'libx264'. 'auto' testet NVENC mit einem 1-Frame-Encode (Ergebnis gecacht)."""
    raise NotImplementedError


def video_codec_args(encoder: str, cfg: RenderConfig) -> list[str]:
    """ffmpeg-Argumente für Qualität (CRF/CQ), Profil high, yuv420p, GOP = 2*fps, +faststart."""
    raise NotImplementedError
