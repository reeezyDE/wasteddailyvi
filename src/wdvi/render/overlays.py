"""PNG-Overlays mit Pillow: Hook-Titel, Schlussfrage (Outro), Credit-Tag, Wasserzeichen, Emojis
(Twemoji, CC-BY 4.0)."""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from itertools import pairwise
from pathlib import Path

import httpx
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from wdvi.config import Settings
from wdvi.render import _fonts
from wdvi.render.captions import COLOR_HEX, match_word_colors, resolve_color

__all__ = ["COLOR_HEX", "emoji_png", "render_credit_png", "render_hook_png", "render_outro_png",
           "render_watermark_png", "split_emojis"]

log = logging.getLogger(__name__)

TWEMOJI_URL = "https://raw.githubusercontent.com/jdecked/twemoji/main/assets/svg/{name}.svg"
EMOJI_RETRY_S = 900.0  # nach Fehlschlag (z. B. offline) so lange nicht erneut versuchen
HOOK_MAX_W = 0.88  # Pillenbreite max. 88 % der Bildbreite
HOOK_MAX_SIZE = 0.10  # max. Schriftgröße (Anteil der Bildbreite)
HOOK_MAX_SIZE_COMPACT = 0.075

# Emoji-Cluster (inkl. ZWJ-Sequenzen, Hautfarben, Flaggen, Keycaps)
_EMOJI_CLUSTER = re.compile(
    "(?:[\U0001f1e6-\U0001f1ff]{2})"
    "|(?:[0-9#*]️?⃣)"
    "|(?:[\U0001f000-\U0001faff☀-➿⬀-⯿←-⇿⌀-⏿〰〽㊗㊙]"
    "[️\U0001f3fb-\U0001f3ff]*"
    "(?:‍[\U0001f000-\U0001faff☀-➿⬀-⯿♀♂][️\U0001f3fb-\U0001f3ff]*)*)"
)

_failed: dict[str, float] = {}
_failed_lock = threading.Lock()


# --------------------------------------------------------------------------- Grundlagen


def _rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except (ValueError, IndexError):
        return 255, 255, 255


def _font(settings: Settings, filename: str, size: int):
    return _fonts.load(str(_fonts.font_path(settings.config.paths.fonts_dir, filename)), int(size))


def _save(img: Image.Image, out: Path) -> Path:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp.png")
    img.save(tmp, "PNG", optimize=False)
    os.replace(tmp, out)
    return out


def _rounded_mask(w: int, h: int, radius: int, ss: int = 4) -> Image.Image:
    """Kantengeglättete Rounded-Rect-Maske (Supersampling)."""
    big = Image.new("L", (w * ss, h * ss), 0)
    ImageDraw.Draw(big).rounded_rectangle((0, 0, w * ss - 1, h * ss - 1), radius=radius * ss, fill=255)
    return big.resize((w, h), Image.LANCZOS)


def _gradient(w: int, h: int, c1: tuple[int, int, int], c2: tuple[int, int, int]) -> Image.Image:
    t = np.linspace(0.0, 1.0, max(1, w), dtype=np.float32)[None, :, None]
    a = np.array(c1, np.float32)[None, None, :]
    b = np.array(c2, np.float32)[None, None, :]
    row = a + (b - a) * t
    arr = np.repeat(row, max(1, h), axis=0)
    # leichter vertikaler Glanz (oben heller) für Tiefe
    shade = np.linspace(1.08, 0.92, max(1, h), dtype=np.float32)[:, None, None]
    arr = np.clip(arr * shade, 0, 255).astype(np.uint8)
    return Image.fromarray(arr, "RGB")


def split_emojis(text: str) -> list[tuple[str, bool]]:
    """Text in (Stück, ist_emoji)-Teile zerlegen."""
    parts: list[tuple[str, bool]] = []
    pos = 0
    for m in _EMOJI_CLUSTER.finditer(text):
        if m.start() > pos:
            parts.append((text[pos:m.start()], False))
        parts.append((m.group(0), True))
        pos = m.end()
    if pos < len(text):
        parts.append((text[pos:], False))
    return parts


# --------------------------------------------------------------------------- Emoji


def _twemoji_names(emoji: str) -> list[str]:
    cps = [f"{ord(c):x}" for c in emoji]
    full = "-".join(cps)
    no_fe0f = "-".join(c for c in cps if c != "fe0f")
    # Twemoji-Dateinamen: ohne fe0f, außer in ZWJ-Sequenzen
    first = full if "200d" in cps else no_fe0f
    names: list[str] = []
    for n in (first, full, no_fe0f):
        if n and n not in names:
            names.append(n)
    return names


def emoji_png(emoji: str, size: int, cache_dir: Path) -> Path | None:
    """Twemoji-SVG (raw.githubusercontent.com/jdecked/twemoji) -> PNG in size px, gecacht. None bei Fehler."""
    try:
        emoji = (emoji or "").strip()
        if not emoji or size <= 0:
            return None
        names = _twemoji_names(emoji)
        edir = Path(cache_dir) / "emoji"
        for name in names:
            png = edir / f"{name}_{int(size)}.png"
            if png.is_file() and png.stat().st_size > 0:
                return png
        key = names[0]
        with _failed_lock:
            failed_at = _failed.get(key)
        if failed_at is not None and time.monotonic() - failed_at < EMOJI_RETRY_S:
            return None
        edir.mkdir(parents=True, exist_ok=True)
        for name in names:
            svg = _fetch_svg(name, edir)
            if svg is None:
                continue
            png = edir / f"{name}_{int(size)}.png"
            try:
                import cairosvg  # erst hier: braucht libcairo (unter Windows oft nicht vorhanden)

                data = cairosvg.svg2png(bytestring=svg, output_width=int(size), output_height=int(size))
            except (ImportError, OSError) as e:
                data = _png_fallback(name, int(size))  # fertiges 72-px-PNG hochskalieren
                if data is None:
                    raise
                log.info("cairosvg unavailable (%s) - using Twemoji PNG for %s", e, name)
            tmp = png.with_name(png.name + ".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, png)
            return png
        with _failed_lock:
            _failed[key] = time.monotonic()
        log.info("Emoji %r not found on Twemoji (%s)", emoji, ", ".join(names))
    except Exception as e:  # Netzwerk, cairo, Dateisystem – Emoji ist Deko, nie fatal
        log.warning("Emoji %r could not be rendered: %s", emoji, e)
        try:
            with _failed_lock:
                _failed[_twemoji_names(emoji)[0]] = time.monotonic()
        except Exception:
            pass
    return None


def _png_fallback(name: str, size: int) -> bytes | None:
    """Twemoji-72x72-PNG laden und auf size px skalieren (ohne libcairo). None, wenn nicht vorhanden."""
    import io

    url = TWEMOJI_URL.format(name=name).replace("/svg/", "/72x72/").replace(".svg", ".png")
    with httpx.Client(timeout=httpx.Timeout(10.0, connect=5.0), follow_redirects=True) as client:
        r = client.get(url)
    if r.status_code != 200 or not r.content.startswith(b"\x89PNG"):
        return None
    with Image.open(io.BytesIO(r.content)) as im:
        out = io.BytesIO()
        im.convert("RGBA").resize((size, size), Image.LANCZOS).save(out, "PNG")
    return out.getvalue()


def _fetch_svg(name: str, edir: Path) -> bytes | None:
    cached = edir / f"{name}.svg"
    if cached.is_file() and cached.stat().st_size > 0:
        return cached.read_bytes()
    url = TWEMOJI_URL.format(name=name)
    with httpx.Client(timeout=httpx.Timeout(10.0, connect=5.0), follow_redirects=True) as client:
        r = client.get(url)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    data = r.content
    if b"<svg" not in data[:2000]:
        return None
    tmp = cached.with_name(cached.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, cached)
    return data


def _emoji_image(emoji: str, size: int, settings: Settings) -> Image.Image | None:
    p = emoji_png(emoji, size, settings.config.paths.cache_dir)
    if p is None:
        return None
    try:
        return Image.open(p).convert("RGBA")
    except OSError:
        return None


# --------------------------------------------------------------------------- Hook


def _paste_with_outline(img: Image.Image, em: Image.Image, x: int, y: int, width: int) -> None:
    """Emoji mit dunkler Kontur + Schatten einsetzen (hebt sich vom Verlauf ab)."""
    a = em.getchannel("A")
    ring = a.filter(ImageFilter.MaxFilter(2 * width + 1)) if width > 0 else a
    shadow = Image.new("RGBA", em.size, (0, 0, 0, 0))
    shadow.putalpha(ring.point(lambda v: v * 200 // 255))
    pad = Image.new("RGBA", img.size, (0, 0, 0, 0))
    pad.alpha_composite(shadow, (x, y + width))
    pad.alpha_composite(shadow, (x, y))
    pad.alpha_composite(em, (x, y))
    img.alpha_composite(pad)


def _hook_tokens(text: str) -> list[tuple[str, bool]]:
    """Wörter + Emojis als Tokens (Emoji-Tokens separat, auch wenn am Wort klebend)."""
    tokens: list[tuple[str, bool]] = []
    for word in text.split():
        for piece, is_emoji in split_emojis(word):
            piece = piece if is_emoji else piece.strip()
            if piece:
                tokens.append((piece, is_emoji))
    return tokens


def _line_width(tokens: list[tuple[str, bool]], font, emoji_px: int, stroke: int) -> float:
    if not tokens:
        return 0.0
    space = font.getlength(" ") + stroke  # Kontur frisst Wortabstand -> ausgleichen
    w = 0.0
    for i, (tok, is_emoji) in enumerate(tokens):
        if i:
            w += space
        w += emoji_px if is_emoji else font.getlength(tok)
    return w + 2 * stroke


def _hook_layouts(tokens: list[tuple[str, bool]]) -> list[list[list[tuple[str, bool]]]]:
    """Kandidaten: eine Zeile oder zwei Zeilen (jede Trennstelle)."""
    out = [[tokens]]
    for i in range(1, len(tokens)):
        if tokens[i][1] and i == len(tokens) - 1:
            continue  # Emoji nicht allein in Zeile 2
        out.append([tokens[:i], tokens[i:]])
    return out


def render_hook_png(text: str, settings: Settings, out: Path, *, compact: bool = False) -> Path:
    """Großer Hook-Text (font_hook, Großbuchstaben, max 2 Zeilen, auto-skaliert auf ~90 % Breite) auf
    abgerundetem Verlaufsbalken (color_primary -> color_secondary), schwarze Kontur. RGBA, Breite = render.width.
    compact=True: kleinere Schrift, bevorzugt einzeilig (Split-Layout: Facecam-Gesicht nicht verdecken)."""
    cfg = settings.config
    brand = cfg.brand
    W = int(cfg.render.width)
    fpath = str(_fonts.font_path(cfg.paths.fonts_dir, brand.font_hook))
    checker = _fonts.GlyphChecker(_fonts.load(fpath, 64))

    raw = " ".join((text or "").split()).upper()
    tokens = [(tok if is_e else checker.filter(tok), is_e) for tok, is_e in _hook_tokens(raw)]
    # Emojis, die nicht geladen werden können (offline/unbekannt), gar nicht erst einplanen
    tokens = [t for t in tokens if t[0] and (not t[1] or emoji_png(t[0], 72, cfg.paths.cache_dir) is not None)]
    if not tokens:
        tokens = [(checker.filter(brand.name.upper()) or "WASTED", False)]

    pill_max_w = round(W * HOOK_MAX_W)
    pad_x = round(W * 0.042)
    max_size, min_size = round(W * (HOOK_MAX_SIZE_COMPACT if compact else HOOK_MAX_SIZE)), max(12, round(W * 0.02))
    two_line_factor = 0.75 if compact else 0.9

    def fits(layout, size: int) -> bool:
        font = _fonts.load(fpath, size)
        stroke = max(2, round(size * 0.075))
        return all(_line_width(ln, font, round(size * 0.92), stroke) <= pill_max_w - 2 * pad_x for ln in layout)

    # Kandidaten (1 Zeile / 2 Zeilen) mit jeweils größter passender Schrift bewerten
    best: tuple[float, list, int] | None = None
    for layout in _hook_layouts(tokens):
        lo, hi = min_size, max_size
        if not fits(layout, lo):
            continue
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if fits(layout, mid):
                lo = mid
            else:
                hi = mid - 1
        balance = 0.0
        if len(layout) == 2:  # ausgewogene Zeilen bevorzugen
            f = _fonts.load(fpath, lo)
            w1, w2 = (_line_width(ln, f, round(lo * 0.92), 0) for ln in layout)
            balance = abs(w1 - w2) / max(w1, w2, 1.0)
        score = lo * (1.0 if len(layout) == 1 else two_line_factor) - balance * 12
        if best is None or score > best[0]:
            best = (score, layout, lo)
    best_layout, size = (best[1], best[2]) if best else ([tokens], min_size)

    font = _fonts.load(fpath, size)
    stroke = max(2, round(size * 0.075))
    emoji_px = round(size * 0.92)
    cap_top = font.getbbox("H")[1]  # Abstand Oberkante -> Großbuchstaben
    cap_h = font.getbbox("H")[3] - cap_top
    line_gap = round(size * 0.12)
    line_h = cap_h + 2 * stroke
    text_h = len(best_layout) * line_h + (len(best_layout) - 1) * line_gap
    pad_y = round(size * 0.30)
    widths = [_line_width(ln, font, emoji_px, stroke) for ln in best_layout]
    pill_w = min(pill_max_w, round(max(widths) + 2 * pad_x))
    pill_h = round(text_h + 2 * pad_y)
    radius = min(pill_h // 2, round(size * 0.42))

    margin = round(size * 0.35)  # Platz für Schatten
    img_h = pill_h + 2 * margin
    img = Image.new("RGBA", (W, img_h), (0, 0, 0, 0))
    px0 = (W - pill_w) // 2
    py0 = margin

    # Schatten unter der Pille
    mask = _rounded_mask(pill_w, pill_h, radius)
    shadow = Image.new("L", (W, img_h), 0)
    shadow.paste(mask.point(lambda v: v * 120 // 255), (px0, py0 + round(size * 0.09)))
    shadow = shadow.filter(ImageFilter.GaussianBlur(round(size * 0.14)))
    img.paste(Image.new("RGBA", (W, img_h), (0, 0, 0, 255)), (0, 0), shadow)

    # Pille: dunkler Rand + Verlauf
    border = max(3, round(size * 0.05))
    outer = _rounded_mask(pill_w + 2 * border, pill_h + 2 * border, radius + border)
    img.paste(Image.new("RGBA", outer.size, (*_rgb(brand.color_outline), 255)), (px0 - border, py0 - border), outer)
    grad = _gradient(pill_w, pill_h, _rgb(brand.color_primary), _rgb(brand.color_secondary)).convert("RGBA")
    img.paste(grad, (px0, py0), mask)

    # Text: weiß, dicke schwarze Kontur, harter Versatzschatten
    draw = ImageDraw.Draw(img)
    y = py0 + pad_y + stroke
    for ln, lw in zip(best_layout, widths, strict=True):
        x = (W - lw) / 2 + stroke
        space = font.getlength(" ") + stroke
        for i, (tok, is_emoji) in enumerate(ln):
            if i:
                x += space
            if is_emoji:
                em = _emoji_image(tok, emoji_px, settings)
                if em is not None:
                    ey = round(y + cap_h / 2 - emoji_px / 2)
                    _paste_with_outline(img, em, round(x), ey, max(2, stroke // 2))
                x += emoji_px
                continue
            ty = y - cap_top
            draw.text((x, ty + round(size * 0.06)), tok, font=font, fill=(0, 0, 0, 150),
                      stroke_width=stroke, stroke_fill=(0, 0, 0, 150))
            draw.text((x, ty), tok, font=font, fill=(*_rgb(brand.color_text), 255),
                      stroke_width=stroke, stroke_fill=(*_rgb(brand.color_outline), 255))
            x += font.getlength(tok)
        y += line_h + line_gap
    return _save(img, out)


# --------------------------------------------------------------------------- Outro (Schlussfrage)

OUTRO_MAX_W = 0.88  # Panelbreite max. 88 % der Bildbreite
OUTRO_MAX_SIZE = 0.105  # max. Schriftgröße (Anteil der Bildbreite)
OUTRO_MAX_LINES = 3
OUTRO_TAB = "COMMENT BELOW"
_OUTRO_STOP = {"a", "an", "the", "and", "or", "of", "to", "in", "on", "is", "it", "at", "for", "with", "you", "your",
               "i", "we", "he", "she", "they", "be", "do", "does", "did", "would", "will", "should", "could", "can",
               "are", "was", "were", "this", "that", "than", "what", "who", "which", "why", "how", "when", "more",
               "better", "best", "worse", "worst", "really", "actually", "ever", "now", "my", "his", "her", "if",
               "der", "die", "das", "ist", "und", "oder", "wer", "wie", "besser", "als"}


def _partitions(n: int, k: int):
    """Alle Aufteilungen von n Tokens in k nicht leere, aufeinanderfolgende Zeilen (Grenzindizes)."""
    if k == 1:
        yield [0, n]
        return
    if k == 2:
        for a in range(1, n):
            yield [0, a, n]
        return
    for a in range(1, n - 1):
        for b in range(a + 1, n):
            yield [0, a, b, n]


def _outro_colors(tokens: list[tuple[str, bool]], brand, word_colors) -> list[str | None]:
    """'#RRGGBB' je Token oder None (weiß). Erst word_colors (Bedeutungsfarben), sonst automatisch die
    1–2 stärksten Schlüsselwörter in Markenfarben (pink, dann orange)."""
    words = [t if not e else "" for t, e in tokens]
    named = match_word_colors(words, word_colors) if word_colors else [None] * len(tokens)
    if any(named):
        return [resolve_color(c, brand) if c and c != "white" else None for c in named]
    cands = []
    for i, (tok, is_emoji) in enumerate(tokens):
        key = re.sub(r"[\W_]", "", tok.lower())
        if is_emoji or not key or key in _OUTRO_STOP or key.isdigit():
            continue
        weight = len(key)
        if i + 1 < len(tokens) and tokens[i + 1][0].strip("?!.,").isdigit():
            weight += 2  # "GTA 6", "PS 5": Eigennamen mit Zahl sind fast immer das Thema
        if weight < 4:
            continue
        cands.append((-weight, i))
    picked = sorted(i for _, i in sorted(cands)[:2])
    out: list[str | None] = [None] * len(tokens)
    for n, i in enumerate(picked):
        out[i] = brand.color_primary if n == 0 else brand.color_secondary
    # "GTA 6": Zahl direkt hinter einem farbigen Wort bekommt dieselbe Farbe
    for i in range(1, len(tokens)):
        if out[i] is None and out[i - 1] and tokens[i][0].strip("?!.,").isdigit():
            out[i] = out[i - 1]
    return out


def render_outro_png(text: str, settings: Settings, out: Path, *, word_colors=None) -> Path:
    """Schlussfrage als große Karte (für Kommentare): dunkles Glas-Panel mit Marken-Verlaufsrand und Farb-Glow,
    Reiter "COMMENT BELOW", Frage in font_hook (Großbuchstaben, bis 3 Zeilen, auto-skaliert), Schlüsselwörter
    farbig (word_colors wie bei den Captions, sonst automatisch in Markenfarben). Bewusst anders als die
    Hook-Pille (kein Vollverlauf). RGBA, Breite = render.width."""
    cfg = settings.config
    brand = cfg.brand
    W = int(cfg.render.width)
    fpath = str(_fonts.font_path(cfg.paths.fonts_dir, brand.font_hook))
    checker = _fonts.GlyphChecker(_fonts.load(fpath, 64))

    raw = " ".join((text or "").split()).upper()
    tokens = [(tok if is_e else checker.filter(tok), is_e) for tok, is_e in _hook_tokens(raw)]
    tokens = [t for t in tokens if t[0] and (not t[1] or emoji_png(t[0], 72, cfg.paths.cache_dir) is not None)]
    if not tokens:
        tokens = [("WHAT DO YOU THINK?", False)]
    colors = _outro_colors(tokens, brand, word_colors)

    panel_max_w = round(W * OUTRO_MAX_W)
    pad_x = round(W * 0.06)
    avail = panel_max_w - 2 * pad_x
    max_size, min_size = round(W * OUTRO_MAX_SIZE), max(12, round(W * 0.03))

    def widths(bounds, size: int) -> list[float]:
        font = _fonts.load(fpath, size)
        stroke = max(1, round(size * 0.035))
        return [_line_width(tokens[a:b], font, round(size * 0.9), stroke) for a, b in pairwise(bounds)]

    # je Zeilenzahl: ausgewogenste Aufteilung, dann größte passende Schrift; weniger Zeilen leicht bevorzugt
    best: tuple[float, list[int], int] | None = None
    ref = 100
    for k in range(1, min(OUTRO_MAX_LINES, len(tokens)) + 1):
        parts = list(_partitions(len(tokens), k))
        # keine Zeile nur aus einem Emoji
        parts = [bnd for bnd in parts if not any(b - a == 1 and tokens[a][1] for a, b in pairwise(bnd))] or parts
        bounds = min(parts, key=lambda bnd: max(widths(bnd, ref)))
        lo, hi = min_size, max_size
        if max(widths(bounds, lo)) > avail:
            continue
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if max(widths(bounds, mid)) <= avail:
                lo = mid
            else:
                hi = mid - 1
        score = lo * (1.0, 0.88, 0.72)[k - 1]
        if best is None or score > best[0]:
            best = (score, bounds, lo)
    bounds, size = (best[1], best[2]) if best else ([0, len(tokens)], min_size)

    font = _fonts.load(fpath, size)
    stroke = max(1, round(size * 0.035))
    emoji_px = round(size * 0.9)
    cap_top = font.getbbox("H")[1]
    cap_h = font.getbbox("H")[3] - cap_top
    line_gap = round(size * 0.26)
    line_h = cap_h + 2 * stroke
    lines = [list(range(a, b)) for a, b in pairwise(bounds)]
    line_ws = widths(bounds, size)
    text_h = len(lines) * line_h + (len(lines) - 1) * line_gap

    tab_size = max(12, round(W * 0.03))
    tab_font = _fonts.load(str(_fonts.font_path(cfg.paths.fonts_dir, brand.font_captions)), tab_size)
    tab_text = _fonts.GlyphChecker(tab_font).filter(OUTRO_TAB)
    tab_h = round(tab_size * 1.9)
    tab_w = round(tab_font.getlength(tab_text) + tab_size * 1.6)

    pad_top = round(tab_h * 0.5 + size * 0.42)
    pad_bot = round(size * 0.5)
    panel_w = max(min(panel_max_w, round(max(line_ws) + 2 * pad_x)), tab_w + 2 * round(W * 0.05), round(W * 0.6))
    panel_h = round(text_h + pad_top + pad_bot)
    radius = round(W * 0.05)
    border = max(3, round(W * 0.0055))

    margin = round(W * 0.06)  # Platz für Glow/Schatten
    img_h = panel_h + tab_h // 2 + 2 * margin
    img = Image.new("RGBA", (W, img_h), (0, 0, 0, 0))
    px0 = (W - panel_w) // 2
    py0 = margin + tab_h // 2

    # Farb-Glow + weicher Schatten
    mask = _rounded_mask(panel_w, panel_h, radius)
    glow = Image.new("L", (W, img_h), 0)
    glow.paste(mask.point(lambda v: v * 150 // 255), (px0, py0))
    glow = glow.filter(ImageFilter.GaussianBlur(round(W * 0.03)))
    img.paste(Image.new("RGBA", (W, img_h), (*_rgb(brand.color_primary), 255)), (0, 0), glow)
    shadow = Image.new("L", (W, img_h), 0)
    shadow.paste(mask.point(lambda v: v * 170 // 255), (px0, py0 + round(W * 0.012)))
    shadow = shadow.filter(ImageFilter.GaussianBlur(round(W * 0.016)))
    img.alpha_composite(_solid_with_alpha((0, 0, 0), shadow))

    # Rand: Markenverlauf; innen dunkles Glas mit leichtem Glanz oben
    outer = _rounded_mask(panel_w + 2 * border, panel_h + 2 * border, radius + border)
    ring = _gradient(panel_w + 2 * border, panel_h + 2 * border, _rgb(brand.color_primary),
                     _rgb(brand.color_secondary)).convert("RGBA")
    img.paste(ring, (px0 - border, py0 - border), outer)
    glass = np.zeros((panel_h, panel_w, 4), np.float32)
    t = np.linspace(0.0, 1.0, panel_h, dtype=np.float32)[:, None]
    glass[..., 0] = 26 - 12 * t
    glass[..., 1] = 22 - 10 * t
    glass[..., 2] = 34 - 14 * t
    glass[..., 3] = 255
    hl = np.clip(1.0 - t / 0.45, 0, 1) * 18  # Glanz oben
    glass[..., :3] += hl[..., None]
    glass_img = Image.fromarray(np.clip(glass, 0, 255).astype(np.uint8), "RGBA")
    img.paste(glass_img, (px0, py0), mask)

    # Reiter oben mittig (Markenfarbe, dunkle Schrift)
    tx0 = (W - tab_w) // 2
    ty0 = py0 - tab_h // 2
    tab_mask = _rounded_mask(tab_w, tab_h, tab_h // 2)
    img.paste(Image.new("RGBA", (tab_w, tab_h), (*_rgb(brand.color_outline), 255)), (tx0, ty0),
              tab_mask)
    inner = 3
    tab_in = _gradient(tab_w - 2 * inner, tab_h - 2 * inner, _rgb(brand.color_primary),
                       _rgb(brand.color_secondary)).convert("RGBA")
    img.paste(tab_in, (tx0 + inner, ty0 + inner), _rounded_mask(tab_w - 2 * inner, tab_h - 2 * inner,
                                                                (tab_h - 2 * inner) // 2))
    draw = ImageDraw.Draw(img)
    tb = tab_font.getbbox("H")
    draw.text(((W - tab_font.getlength(tab_text)) / 2, ty0 + (tab_h - (tb[3] - tb[1])) / 2 - tb[1]), tab_text,
              font=tab_font, fill=(255, 255, 255, 255))

    # Frage: weiß, Schlüsselwörter farbig, feine Kontur + weicher Schatten
    txt = Image.new("RGBA", (W, img_h), (0, 0, 0, 0))
    tdraw = ImageDraw.Draw(txt)
    shadow_layer = Image.new("RGBA", (W, img_h), (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(shadow_layer)
    y = py0 + pad_top + stroke
    space = font.getlength(" ") + stroke
    for idx, lw in zip(lines, line_ws, strict=True):
        x = (W - lw) / 2 + stroke
        for n, i in enumerate(idx):
            tok, is_emoji = tokens[i]
            if n:
                x += space
            if is_emoji:
                em = _emoji_image(tok, emoji_px, settings)
                if em is not None:
                    txt.alpha_composite(em, (round(x), round(y + cap_h / 2 - emoji_px / 2)))
                x += emoji_px
                continue
            ty = y - cap_top
            fill = _rgb(colors[i]) if colors[i] else _rgb(brand.color_text)
            sdraw.text((x, ty + round(size * 0.05)), tok, font=font, fill=(0, 0, 0, 200), stroke_width=stroke,
                       stroke_fill=(0, 0, 0, 200))
            tdraw.text((x, ty), tok, font=font, fill=(*fill, 255), stroke_width=stroke,
                       stroke_fill=(*_rgb(brand.color_outline), 255))
            x += font.getlength(tok)
        y += line_h + line_gap
    img.alpha_composite(shadow_layer.filter(ImageFilter.GaussianBlur(max(1, round(size * 0.04)))))
    img.alpha_composite(txt)
    return _save(img, out)


def _solid_with_alpha(rgb: tuple[int, int, int], alpha: Image.Image) -> Image.Image:
    layer = Image.new("RGBA", alpha.size, (*rgb, 0))
    layer.putalpha(alpha)
    return layer


# --------------------------------------------------------------------------- Credit / Wasserzeichen

_PLATFORM_COLORS = {"twitch": "#B38CFF", "kick": "#53FC18", "youtube": "#FF4D4D", "reddit": "#FF7A45"}


def _draw_camera_icon(size: int, color: tuple[int, int, int]) -> Image.Image:
    """Ersatz-Icon (Videokamera), falls Twemoji nicht erreichbar ist."""
    ss = 4
    s = size * ss
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    body = (0, round(s * 0.25), round(s * 0.68), round(s * 0.75))
    d.rounded_rectangle(body, radius=round(s * 0.1), fill=(*color, 255))
    d.polygon([(round(s * 0.72), round(s * 0.5)), (s - 1, round(s * 0.28)), (s - 1, round(s * 0.72))],
              fill=(*color, 255))
    return img.resize((size, size), Image.LANCZOS)


def render_credit_png(platform_label: str, creator_name: str, settings: Settings, out: Path) -> Path:
    """Kleines halbtransparentes Pill-Label, z. B. "🎥 TWITCH · xQc" (Emoji als Bild, nicht als Font)."""
    cfg = settings.config
    H, W = int(cfg.render.height), int(cfg.render.width)
    size = max(14, round(H * 0.0158))  # 30 px bei 1920
    fpath = str(_fonts.font_path(cfg.paths.fonts_dir, cfg.brand.font_captions))
    font = _fonts.load(fpath, size)
    checker = _fonts.GlyphChecker(font)

    platform = checker.filter((platform_label or "").strip().upper())
    name = " ".join(checker.filter(creator_name or "").split())
    max_name_w = W * 0.42
    if name and font.getlength(name) > max_name_w:
        ell = "…" if checker.has("…") else "..."
        while name and font.getlength(name + ell) > max_name_w:
            name = name[:-1]
        name = name.rstrip() + ell
    sep = " · " if checker.has("·") else " | "
    pieces: list[tuple[str, tuple[int, int, int]]] = []
    if platform:
        pieces.append((platform, _rgb(_PLATFORM_COLORS.get(platform.lower(), cfg.brand.color_secondary))))
    if platform and name:
        pieces.append((sep, (200, 200, 200)))
    if name:
        pieces.append((name, _rgb(cfg.brand.color_text)))
    if not pieces:
        pieces.append(("CLIP", _rgb(cfg.brand.color_text)))

    # Icon: Emoji auf hellem Kreis (🎥 ist dunkel und ginge auf der dunklen Pille unter)
    pill_h = round(size * 1.85)
    icon_px = pill_h - 2 * max(3, round(size * 0.14))
    emo_px = round(icon_px * 0.74)
    icon = Image.new("RGBA", (icon_px, icon_px), (0, 0, 0, 0))
    icon.paste(Image.new("RGBA", (icon_px, icon_px), (245, 245, 250, 255)), (0, 0),
               _rounded_mask(icon_px, icon_px, icon_px // 2))
    emo = _emoji_image("🎥", emo_px, settings) or _draw_camera_icon(emo_px, (30, 30, 36))
    icon.alpha_composite(emo, ((icon_px - emo_px) // 2, (icon_px - emo_px) // 2))
    gap = round(size * 0.4)
    pad_x = round(size * 0.6)
    pad_l = (pill_h - icon_px) // 2
    text_w = sum(font.getlength(t) for t, _ in pieces)
    pill_w = round(pad_l + icon_px + gap + text_w + pad_x)

    img = Image.new("RGBA", (pill_w, pill_h), (0, 0, 0, 0))
    mask = _rounded_mask(pill_w, pill_h, pill_h // 2)
    img.paste(Image.new("RGBA", (pill_w, pill_h), (12, 12, 16, 165)), (0, 0), mask)
    img.alpha_composite(icon, (pad_l, (pill_h - icon_px) // 2))
    draw = ImageDraw.Draw(img)
    bbox = font.getbbox("H")
    ty = (pill_h - (bbox[3] - bbox[1])) / 2 - bbox[1]
    x = float(pad_l + icon_px + gap)
    for t, color in pieces:
        draw.text((x, ty), t, font=font, fill=(*color, 255))
        x += font.getlength(t)
    return _save(img, out)


def render_watermark_png(settings: Settings, out: Path) -> Path:
    """Dezentes "WASTED DAILY VI" (WASTED in color_wasted)."""
    cfg = settings.config
    H = int(cfg.render.height)
    size = max(12, round(H * 0.0185))  # ~36 px bei 1920
    font = _fonts.load(str(_fonts.font_path(cfg.paths.fonts_dir, cfg.brand.font_hook)), size)
    stroke = max(1, round(size * 0.08))
    parts = [("WASTED", _rgb(cfg.brand.color_wasted)), (" DAILY VI", _rgb(cfg.brand.color_text))]
    text_w = sum(font.getlength(t) for t, _ in parts)
    bbox = font.getbbox("WASTED DAILY VI")
    pad = stroke + 4
    w = round(text_w + 2 * pad)
    h = round(bbox[3] - bbox[1] + 2 * pad + 3)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    x = float(pad)
    y = pad - bbox[1]
    for t, color in parts:
        draw.text((x + 2, y + 2), t, font=font, fill=(0, 0, 0, 120), stroke_width=stroke, stroke_fill=(0, 0, 0, 120))
        draw.text((x, y), t, font=font, fill=(*color, 255), stroke_width=stroke, stroke_fill=(0, 0, 0, 220))
        x += font.getlength(t)
    # halbtransparent
    alpha = img.getchannel("A").point(lambda v: v * 78 // 100)
    img.putalpha(alpha)
    return _save(img, out)
