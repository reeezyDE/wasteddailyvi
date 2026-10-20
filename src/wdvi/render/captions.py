"""Animierte Wort-für-Wort-Captions als ASS (libass via ffmpeg).

Stil "Wasted": Großbuchstaben, schwere Schrift, weiß mit dicker schwarzer Kontur + Schatten;
das gerade gesprochene Wort in brand.color_primary und leicht vergrößert (Pop), highlight_words in
brand.color_secondary. 1–3 Wörter pro Zeile (brand.caption_max_words / caption_max_chars),
Umbruch an Satzzeichen und Pausen > 0.45 s.
"""

from __future__ import annotations

from pathlib import Path

from wdvi.config import Settings
from wdvi.schemas import LayoutKind, TimeMap, Word


def chunk_words(words: list[Word], max_words: int, max_chars: int, gap_s: float = 0.45) -> list[list[Word]]:
    raise NotImplementedError


def build_ass(words: list[Word], timemap: TimeMap, highlight_words: list[str], layout: LayoutKind,
              settings: Settings, out_path: Path) -> Path:
    """Wörter (Quellzeit) über timemap in Ausgabezeit umrechnen (herausgeschnittene Wörter fallen weg),
    gruppieren, ASS schreiben (PlayResX/Y = render.width/height). Im Split-Layout sitzen Captions auf
    der Naht zwischen Facecam und Gameplay; sonst bei captions.y_ratio. Leere Wortliste -> gültige ASS
    ohne Events."""
    raise NotImplementedError
