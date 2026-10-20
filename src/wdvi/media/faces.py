"""Gesichtserkennung (OpenCV YuNet, Fallback Haar) und Layout-Entscheidung 16:9 -> 9:16."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from wdvi.config import Settings
from wdvi.schemas import FaceDet, LayoutHint, MediaInfo


class FaceDetector:
    def __init__(self, settings: Settings) -> None:
        """Lädt YuNet-ONNX aus cache_dir (lädt es bei Bedarf von opencv_zoo); sonst Haar-Cascade."""
        raise NotImplementedError

    def detect(self, img_bgr: np.ndarray) -> list[FaceDet]:
        raise NotImplementedError


def analyze_layout(path: str | Path, info: MediaInfo, detector: FaceDetector, samples: int = 16) -> LayoutHint:
    """Entscheidet das Layout:
    - Höhe >= Breite -> vertical_native
    - kleines Gesicht (Breite < ~22 % des Bildes) stabil an ähnlicher Position in >=40 % der Samples
      -> facecam_split; facecam_box = geschätzter Webcam-Rahmen um das Gesicht (Seitenverhältnis ~16:9
      oder 4:3, an Bildrand geklemmt); gameplay_box = Bildbereich ohne Facecam-Ecke (mittig, 16:9)
    - großes Gesicht in den meisten Samples -> face_track (track = geglättete Gesichtsmitten)
    - sonst gameplay_fit
    """
    raise NotImplementedError
