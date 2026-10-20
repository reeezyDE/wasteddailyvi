"""Animierte Wort-für-Wort-Captions als ASS (libass via ffmpeg).

Stil "Wasted": Großbuchstaben, schwere Schrift, weiß mit dicker schwarzer Kontur + Schatten;
das gerade gesprochene Wort in brand.color_primary und leicht vergrößert (Pop), highlight_words in
brand.color_secondary. 1–3 Wörter pro Zeile (brand.caption_max_words / caption_max_chars),
Umbruch an Satzzeichen und Pausen > 0.45 s.
"""

from __future__ import annotations

import codecs
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from wdvi.config import Settings
from wdvi.render import _fonts
from wdvi.render.timeline import map_interval
from wdvi.schemas import LayoutKind, TimeMap, Word

log = logging.getLogger(__name__)

BREAK_AFTER = (".", ",", "!", "?", ";", ":", "…")
BRIDGE_GAP_S = 0.25  # kleinere Lücken zwischen Events werden überbrückt (kein Flackern)
LINGER_S = 0.35  # letzte Zeile bleibt nach dem letzten Wort noch kurz stehen
MIN_WORD_S = 0.05
CAPTION_X_RATIO = 0.5
MAX_W_RATIO = 0.76  # Zeilenbreite: rechts ~12 % frei für die Plattform-Buttons
MAX_W_RATIO_SEAM = 0.90  # auf der Split-Naht gibt es keine Buttons
CHUNK_POP_FROM = 0.72  # neue Zeile skaliert von 72 % hoch
CHUNK_POP_MS = 120
FADE_IN_MS = 60
FADE_OUT_MS = 90

# Zeichen, die am Wortrand wegfallen (Satzzeichen wirken in Captions unruhig); ! ? ' bleiben
_EDGE_STRIP = ".,;:…\"“”„«»()[]<>–—-*_~^`´|/"
_EMOJI = re.compile("[\U0001f000-\U0001faff☀-➿⬀-⯿️‍⃣]")
_STOP = {"a", "an", "the", "and", "or", "of", "to", "in", "on", "is", "it", "at", "for", "with"}

# Schwere Beleidigungen (rassistisch, homo-/transfeindlich, ableistisch) werden in eingebrannten Captions
# maskiert ("N****") – zweite Absicherung, falls Claude so einen Clip durchlässt (Werbefreundlichkeit,
# Community-Richtlinien). Normale Flüche (fuck, shit …) bleiben. rot13, damit die Wörter nicht im Klartext im
# Code stehen; verglichen wird das Wort ohne Satz-/Sonderzeichen in Kleinbuchstaben.
_SLURS_ROT13 = (
    "avttre", "avttref", "avttn", "avttnf", "avttnm", "avttnu", "avttnuf", "snttbg", "snttbgf",
    "snt", "sntf", "ergneq", "ergneqf", "ergneqrq", "genaal", "genaavrf", "xvxr", "xvxrf", "fcvp",
    "fcvpf", "jrgonpx", "jrgonpxf",
)
_SLURS = frozenset(codecs.decode(w, "rot13") for w in _SLURS_ROT13)
_LETTERS = re.compile(r"[^a-z]")


def chunk_words(words: list[Word], max_words: int, max_chars: int, gap_s: float = 0.45) -> list[list[Word]]:
    """Wörter (zeitlich sortiert) zu Zeilen gruppieren: max max_words Wörter / max_chars Zeichen,
    neue Zeile nach Satzzeichen und bei Pausen > gap_s. Leere Wörter fallen weg."""
    max_words = max(1, int(max_words))
    chunks: list[list[Word]] = []
    cur: list[Word] = []
    cur_len = 0
    for w in words:
        text = w.text.strip()
        if not text:
            continue
        if cur:
            prev = cur[-1]
            if (len(cur) >= max_words or cur_len + 1 + len(text) > max_chars
                    or w.start - prev.end > gap_s or prev.text.strip().endswith(BREAK_AFTER)):
                chunks.append(cur)
                cur, cur_len = [], 0
        cur.append(w)
        cur_len = len(text) if len(cur) == 1 else cur_len + 1 + len(text)
    if cur:
        chunks.append(cur)
    return chunks


# --------------------------------------------------------------------------- Hilfen


def ass_color(hex_color: str, alpha: int = 0) -> str:
    """'#RRGGBB' -> '&HAABBGGRR' (Style-Format)."""
    r, g, b = _rgb(hex_color)
    return f"&H{alpha:02X}{b:02X}{g:02X}{r:02X}"


def _tag_color(hex_color: str) -> str:
    """'#RRGGBB' -> '&HBBGGRR&' (Override-Tag-Format)."""
    r, g, b = _rgb(hex_color)
    return f"&H{b:02X}{g:02X}{r:02X}&"


def _rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except (ValueError, IndexError):
        return 255, 255, 255


def ass_time(t: float) -> str:
    cs = max(0, int(round(t * 100)))
    return _cs_time(cs)


def _cs_time(cs: int) -> str:
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, c = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def is_slur(word: str) -> bool:
    """Steht das Wort (ohne Satz-/Sonderzeichen, z. B. "n-word's") auf der Liste schwerer Beleidigungen?"""
    return _LETTERS.sub("", word.lower()) in _SLURS


def mask_word(word: str) -> str:
    """Schwere Beleidigung -> erster Buchstabe + Sternchen ("Faggot" -> "F*****"); sonst unverändert."""
    if not is_slur(word):
        return word
    return word[:1] + "*" * (len(word) - 1)


def mask_slurs(text: str) -> str:
    """mask_word für jedes Wort eines Textes (Leerzeichen/Zeilenumbrüche bleiben erhalten)."""
    return re.sub(r"\S+", lambda m: _mask_token(m.group(0)), text or "")


def _mask_token(token: str) -> str:
    """Satzzeichen am Rand behalten ("nigga," -> "n****,")."""
    m = re.match(r"^(\W*)(.*?)(\W*)$", token, flags=re.S)
    if not m or not m.group(2):
        return token
    return m.group(1) + mask_word(m.group(2)) + m.group(3)


def display_text(raw: str, uppercase: bool) -> str:
    """Wort für die Anzeige: Rand-Satzzeichen weg, Emojis weg, ASS-Sonderzeichen entschärft, schwere
    Beleidigungen maskiert."""
    t = _EMOJI.sub("", raw.strip()).strip(_EDGE_STRIP + " ")
    t = t.replace("\\", "/").replace("{", "(").replace("}", ")")
    t = mask_word(t)
    if uppercase:
        t = t.upper()
    return t


def _norm(token: str) -> str:
    return re.sub(r"[^\w']", "", token.lower())


def _highlight_set(highlight_words: list[str]) -> set[str]:
    out: set[str] = set()
    for entry in highlight_words or []:
        toks = [_norm(t) for t in str(entry).split()]
        toks = [t for t in toks if t]
        if len(toks) == 1:
            out.add(toks[0])
        else:  # Phrasen: nur die bedeutungstragenden Wörter
            out.update(t for t in toks if t not in _STOP and len(t) > 2)
    return out


@dataclass
class _Ev:
    start: int  # Zentisekunden
    end: int
    chunk: int
    idx: int  # aktives Wort in der Zeile


# --------------------------------------------------------------------------- ASS


def build_ass(words: list[Word], timemap: TimeMap, highlight_words: list[str], layout: LayoutKind,
              settings: Settings, out_path: Path) -> Path:
    """Wörter (Quellzeit) über timemap in Ausgabezeit umrechnen (herausgeschnittene Wörter fallen weg),
    gruppieren, ASS schreiben (PlayResX/Y = render.width/height). Im Split-Layout sitzen Captions auf
    der Naht zwischen Facecam und Gameplay; sonst bei captions.y_ratio. Leere Wortliste -> gültige ASS
    ohne Events."""
    cfg = settings.config
    r, brand, cap = cfg.render, cfg.brand, cfg.render.captions
    W, H = int(r.width), int(r.height)
    fpath = str(_fonts.font_path(cfg.paths.fonts_dir, brand.font_captions))
    family = _fonts.family_name(fpath)
    # captions.font_size = Em-Größe in Pixeln (wie bei Pillow/CSS); libass misst anders -> umrechnen
    ass_fs = max(10, round(cap.font_size * _fonts.ass_size_factor(fpath)))
    measure_font = _fonts.load(fpath, cap.font_size)

    seam = layout == "facecam_split"
    x = round(W * CAPTION_X_RATIO)
    y = round((r.split_facecam_ratio if seam else cap.y_ratio) * H)
    max_w = W * (MAX_W_RATIO_SEAM if seam else MAX_W_RATIO)

    # 1) Wörter in Ausgabezeit (teilweise geschnittene werden geklemmt)
    total = timemap.duration
    mapped: list[Word] = []
    for w in sorted(words, key=lambda w: (w.start, w.end)):
        if not display_text(w.text, brand.caption_uppercase):
            continue
        iv = map_interval(timemap, w.start, max(w.end, w.start))
        if iv is None:
            continue
        s, e = iv
        if s >= total:
            continue
        mapped.append(Word(text=w.text.strip(), start=s, end=max(e, s + MIN_WORD_S), prob=w.prob))

    chunks = chunk_words(mapped, brand.caption_max_words, brand.caption_max_chars)

    # 2) ein Event je Wort: aktiv von Wortstart bis Start des nächsten Wortes (letztes: + Nachlauf)
    events: list[_Ev] = []
    for ci, ch in enumerate(chunks):
        for wi, w in enumerate(ch):
            st = round(w.start * 100)
            en = round(ch[wi + 1].start * 100) if wi + 1 < len(ch) else round((w.end + LINGER_S) * 100)
            events.append(_Ev(st, en, ci, wi))
    events.sort(key=lambda e: (e.start, e.chunk, e.idx))
    total_cs = round(total * 100)
    for i, ev in enumerate(events):
        if i + 1 < len(events):
            nxt = events[i + 1].start
            if ev.end > nxt or nxt - ev.end < round(BRIDGE_GAP_S * 100):
                ev.end = nxt  # keine Überlappung, kleine Lücken überbrücken
        ev.end = min(ev.end, total_cs)
    events = [e for e in events if e.end > e.start]

    # 3) Text je Event
    hl = _highlight_set(highlight_words)
    disp = [[display_text(w.text, brand.caption_uppercase) for w in ch] for ch in chunks]
    fits = [_fit_scale(d, measure_font, cap.active_scale, cap.outline, max_w) for d in disp]
    white, primary, secondary = (_tag_color(brand.color_text), _tag_color(brand.color_primary),
                                 _tag_color(brand.color_secondary))
    lines: list[str] = []
    seen_chunks: set[int] = set()
    for i, ev in enumerate(events):
        first = ev.chunk not in seen_chunks
        seen_chunks.add(ev.chunk)
        prev_end = events[i - 1].end if i > 0 else -1
        next_start = events[i + 1].start if i + 1 < len(events) else None
        last_of_chunk = next_start is None or events[i + 1].chunk != ev.chunk
        fade_in = FADE_IN_MS if first and prev_end < ev.start else 0
        fade_out = FADE_OUT_MS if last_of_chunk and (next_start is None or next_start > ev.end) else 0
        text = _event_text(disp[ev.chunk], [_norm(w.text) in hl for w in chunks[ev.chunk]], ev.idx, first,
                           fits[ev.chunk], cap.active_scale, cap.pop_ms, white, primary, secondary)
        head = f"{{\\an5\\pos({x},{y})"
        if fade_in or fade_out:
            head += f"\\fad({fade_in},{fade_out})"
        head += "}"
        lines.append(f"Dialogue: 0,{_cs_time(ev.start)},{_cs_time(ev.end)},Main,,0,0,0,,{head}{text}")

    header = [
        "[Script Info]",
        "; WastedDailyVI captions",
        "ScriptType: v4.00+",
        f"PlayResX: {W}",
        f"PlayResY: {H}",
        "ScaledBorderAndShadow: yes",
        "WrapStyle: 2",
        "YCbCr Matrix: None",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
        "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Main,{family},{ass_fs},{ass_color(brand.color_text)},{ass_color(brand.color_primary)},"
        f"{ass_color(brand.color_outline)},{ass_color('#000000', 0x80)},0,0,0,0,100,100,0,0,1,"
        f"{cap.outline},{cap.shadow},5,0,0,0,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(header + lines) + "\n", encoding="utf-8")
    log.debug("Captions: %d words, %d lines, %d events -> %s", len(mapped), len(chunks), len(lines),
              out_path.name)
    return out_path


def _fit_scale(words: list[str], font, active_scale: int, outline: int, max_w: float) -> float:
    """Skalierung (<= 1), damit die Zeile inkl. vergrößertem aktivem Wort in max_w passt."""
    if not words:
        return 1.0
    try:
        widths = [font.getlength(w) for w in words]
        space = font.getlength(" ")
    except Exception:
        return 1.0
    total = sum(widths) + space * (len(words) - 1)
    total += max(widths) * max(0.0, active_scale / 100.0 * 1.04 - 1.0) + 2 * outline
    return min(1.0, max_w / total) if total > 0 else 1.0


def _event_text(words: list[str], highlighted: list[bool], active: int, first: bool, fit: float,
                active_scale: int, pop_ms: int, white: str, primary: str, secondary: str) -> str:
    base = round(100 * fit)
    act = round(active_scale * fit)
    over = round(act * 1.06)  # kurzes Überschwingen
    p1 = max(20, round(pop_ms * 0.6))
    p2 = max(p1 + 10, pop_ms)
    runs: list[str] = []
    for i, word in enumerate(words):
        if i == active:
            s0 = round(base * CHUNK_POP_FROM) if first else base
            if first:
                p1a, p2a = round(CHUNK_POP_MS * 0.7), CHUNK_POP_MS + 20
            else:
                p1a, p2a = p1, p2
            tags = (f"\\c{primary}\\fscx{s0}\\fscy{s0}\\t(0,{p1a},0.6,\\fscx{over}\\fscy{over})"
                    f"\\t({p1a},{p2a},\\fscx{act}\\fscy{act})")
        else:
            color = secondary if highlighted[i] else white
            if first:
                s0 = round(base * CHUNK_POP_FROM)
                tags = f"\\c{color}\\fscx{s0}\\fscy{s0}\\t(0,{CHUNK_POP_MS},0.5,\\fscx{base}\\fscy{base})"
            else:
                tags = f"\\c{color}\\fscx{base}\\fscy{base}"
        runs.append(f"{{{tags}}}{word}")
    return " ".join(runs)
