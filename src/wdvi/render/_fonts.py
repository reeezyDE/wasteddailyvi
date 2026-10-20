"""Schrift-Helfer: Pfade, Familienname (für ASS), libass-Größenfaktor, Glyphen-Prüfung."""

from __future__ import annotations

import logging
import struct
from functools import lru_cache
from pathlib import Path

from PIL import ImageFont

log = logging.getLogger(__name__)

_FALLBACKS = ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "DejaVuSans-Bold.ttf")


def font_path(fonts_dir: Path, filename: str) -> Path:
    p = Path(filename)
    return p if p.is_absolute() else Path(fonts_dir) / filename


@lru_cache(maxsize=64)
def load(path: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """TrueType-Font in size px (Em-Größe). Fehlt die Datei: Ersatzschrift + Warnung (nie Absturz)."""
    try:
        return ImageFont.truetype(path, max(1, int(size)))
    except OSError:
        log.warning("Font %s not found, using fallback font", path)
    for fb in _FALLBACKS:
        try:
            return ImageFont.truetype(fb, max(1, int(size)))
        except OSError:
            continue
    return ImageFont.load_default(size=max(1, int(size)))


@lru_cache(maxsize=16)
def family_name(path: str) -> str:
    """Echter Familienname aus der TTF (z. B. "Archivo Black") – libass sucht danach."""
    try:
        return ImageFont.truetype(path, 20).getname()[0] or Path(path).stem
    except OSError:
        return Path(path).stem.split("-")[0]


@lru_cache(maxsize=16)
def ass_size_factor(path: str) -> float:
    """libass skaliert so, dass Fontsize = usWinAscent + usWinDescent (in Pixeln) ist, nicht die Em-Größe.
    Faktor = (winAscent + winDescent) / unitsPerEm; ASS-Fontsize = Em-Pixel * Faktor."""
    try:
        data = Path(path).read_bytes()
        num = struct.unpack(">H", data[4:6])[0]
        tables: dict[str, int] = {}
        for i in range(num):
            tag, _cs, off, _ln = struct.unpack(">4sIII", data[12 + 16 * i:28 + 16 * i])
            tables[tag.decode("latin-1")] = off
        upem = struct.unpack(">H", data[tables["head"] + 18:tables["head"] + 20])[0]
        off = tables["OS/2"]
        win_asc, win_desc = struct.unpack(">HH", data[off + 74:off + 78])
        if upem > 0 and win_asc + win_desc > 0:
            return (win_asc + win_desc) / upem
    except (OSError, KeyError, struct.error, UnicodeDecodeError) as e:
        log.debug("Cannot read font metrics of %s: %s", path, e)
    return 1.2


class GlyphChecker:
    """Prüft, ob eine Schrift ein Zeichen hat (Vergleich mit der .notdef-Glyphe)."""

    def __init__(self, font: ImageFont.FreeTypeFont | ImageFont.ImageFont) -> None:
        self._font = font
        self._cache: dict[str, bool] = {}
        try:
            m = font.getmask("\U0010ffff")
            self._notdef = (m.size, bytes(m))
        except Exception:
            self._notdef = None

    def has(self, ch: str) -> bool:
        if ch.isspace():
            return True
        if self._notdef is None:
            return True
        r = self._cache.get(ch)
        if r is None:
            try:
                m = self._font.getmask(ch)
                r = (m.size, bytes(m)) != self._notdef
            except Exception:
                r = False
            self._cache[ch] = r
        return r

    def filter(self, text: str) -> str:
        return "".join(ch for ch in text if self.has(ch))
