"""Bildkomposition Frame für Frame (numpy/OpenCV) -> rohe BGR-Frames für ffmpeg.

Layouts (Ausgabe render.width x render.height):
- facecam_split: oben Facecam (split_facecam_ratio der Höhe, cover-crop aus facecam_box), unten
  Gameplay (cover-crop aus gameplay_box), dünne Trennlinie in color_accent
- face_track: 9:16-Fenster volle Höhe, x folgt geglättetem Gesichtstrack (kritisch gedämpft, kein Zittern)
- gameplay_fit: Hintergrund = Bild cover-skaliert, stark weichgezeichnet (auf 1/4 Auflösung) und
  abgedunkelt; Vordergrund = ganzes Bild auf volle Breite, leicht gezoomt (1.15), vertikal mittig
- vertical_native: cover-skaliert
Zooms (EditPlan.zooms, in Ausgabezeit umgerechnet): weiches Ein-/Ausblenden (ease-in-out 0.25 s),
subpixel-genau via cv2.warpAffine; focus face -> Facecam-Panel bzw. Gesicht, gameplay -> Gameplay-Panel,
center -> ganzes Bild. Overlays (RGBA-PNGs) mit Zeitfenstern und Pop-Animation werden alpha-geblendet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from wdvi.config import Settings
from wdvi.schemas import LayoutHint


@dataclass
class TimedOverlay:
    png: Path
    start: float  # Ausgabezeit
    end: float
    x: int  # linke obere Ecke im Ausgabebild (vor Pop-Skalierung, Pop skaliert um die Mitte)
    y: int
    pop: bool = True
    fade_out: float = 0.15


@dataclass
class ZoomOut:
    start: float  # Ausgabezeit
    end: float
    scale: float
    focus: str


@dataclass
class CompositionSpec:
    layout: LayoutHint
    zooms: list[ZoomOut] = field(default_factory=list)
    overlays: list[TimedOverlay] = field(default_factory=list)


def compose_video(cut_video: Path, spec: CompositionSpec, settings: Settings, ass_path: Path | None,
                  audio_wav: Path, out_mp4: Path, encoder: str) -> Path:
    """Liest cut_video (bereits geschnitten, CFR render.fps) Frame für Frame, komponiert, pipet BGR nach
    ffmpeg (-f rawvideo -pix_fmt bgr24), brennt ASS ein (ass-Filter mit fontsdir=assets/fonts), muxt
    audio_wav, encodiert mit encoder. Gibt out_mp4 zurück."""
    raise NotImplementedError
