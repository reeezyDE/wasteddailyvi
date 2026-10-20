"""Animierte Wort-für-Wort-Captions als ASS (libass via ffmpeg).

Stil "Wasted": Großbuchstaben, schwere Schrift, weiß mit dicker schwarzer Kontur + Schatten;
das gerade gesprochene Wort in brand.color_primary und leicht vergrößert (Pop), highlight_words in
brand.color_secondary. 1–3 Wörter pro Zeile (brand.caption_max_words / caption_max_chars),
Umbruch an Satzzeichen und Pausen > 0.45 s.

Edit-Engine v2 (word_colors / layout "shots"): 2–4 Wörter pro Chunk (breitenbasiert, bei "shots" auch
zweizeilig), etwas größer; Wörter aus word_colors immer in ihrer Bedeutungsfarbe (PLAYSTATION blau, XBOX grün).
render.caption_glow legt eine weichgezeichnete Farbebene unter den Text (Halo). Ohne word_colors, ohne Glow und
ohne "shots" ist die Ausgabe identisch mit v1.
"""

from __future__ import annotations

import codecs
import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from wdvi.config import Settings
from wdvi.render import _fonts
from wdvi.render.timeline import map_interval
from wdvi.schemas import ColorName, LayoutKind, TimeMap, Word, WordColor

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

# --- v2
SHOTS_LAYOUT = "shots"  # Multi-Shot-Clip: Captions an fester Höhe (captions.y_ratio)
V2_FONT_SCALE = 1.08  # v2-Captions etwas größer
V2_MAX_WORDS = 4
V2_HARD_BREAK = (".", "!", "?", ";", ":", "…")  # Komma trennt in v2 nur weich ("CHAT, SHOULD I PLAY")
V2_MIN_FIT = 0.84  # einzeilig darf höchstens so weit verkleinert werden, sonst 2 Zeilen / kürzerer Chunk
GLOW_ALPHA = 0x70  # ~55 % deckend
GLOW_BLUR = 9  # \blur-Stärke (PlayRes-Pixel)
GLOW_BORD_EXTRA = 9  # Glow-Kontur = outline + das

# Bedeutungsfarben (kräftig); pink/orange = Markenfarben (Default-Werte, siehe resolve_color)
COLOR_HEX: dict[ColorName, str] = {
    "white": "#FFFFFF",
    "pink": "#FF2D87",
    "orange": "#FF8A00",
    "yellow": "#FFE135",
    "green": "#31E36B",
    "blue": "#2D9CFF",
    "purple": "#A35BFF",
    "red": "#FF3B3B",
}

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


def caption_y_ratio(cfg, layout: str) -> float:
    """Vertikale Caption-Mitte (0..1): Split -> Naht, gameplay_fit -> unter dem Bild, sonst (auch "shots",
    wo das Layout alle 1–2 s wechselt) fest bei y_ratio."""
    r = cfg.render
    if layout == "facecam_split":
        return r.split_facecam_ratio
    if layout == "gameplay_fit":
        return r.captions.y_ratio_fit
    return r.captions.y_ratio  # face_track, vertical_native, "shots"


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


_PROFANITY = re.compile(r"^(mother)?(f)(u)(ck(?:ing|in|ed|er|ers|s)?)$|^(s)(h)(it(?:ty|s)?)$|^(b)(i)(tch(?:es)?)$",
                        re.IGNORECASE)


def censor_word(word: str) -> str:
    """Gängige Flüche entschärfen: fucking -> f*cking, shit -> sh*t, bitch -> b*tch (Rest unverändert)."""
    m = _PROFANITY.match(word)
    if not m:
        return word
    if m.group(2):  # (mother)f u ck...
        return (m.group(1) or "") + m.group(2) + "*" + m.group(4)
    if m.group(5):
        return m.group(5) + m.group(6) + "*" + m.group(7)[1:]
    return m.group(8) + "*" + m.group(10)


def display_text(raw: str, uppercase: bool, censor: bool = False) -> str:
    """Wort für die Anzeige: Rand-Satzzeichen weg, Emojis weg, ASS-Sonderzeichen entschärft, schwere
    Beleidigungen maskiert (und mit censor auch gängige Flüche)."""
    t = _EMOJI.sub("", raw.strip()).strip(_EDGE_STRIP + " ")
    t = t.replace("\\", "/").replace("{", "(").replace("}", ")")
    t = mask_word(t)
    if censor:
        t = censor_word(t)
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


# --------------------------------------------------------------------------- Bedeutungsfarben (v2)


def resolve_color(name: str, brand=None) -> str:
    """ColorName -> '#RRGGBB'. pink/orange folgen der Marke (brand.color_primary/secondary), unbekannt -> weiß."""
    if brand is not None:
        if name == "pink":
            return brand.color_primary
        if name == "orange":
            return brand.color_secondary
        if name == "white":
            return brand.color_text
    return COLOR_HEX.get(name, COLOR_HEX["white"])  # type: ignore[call-overload]


def _key(token: str) -> str:
    """Vergleichsform: Kleinbuchstaben ohne Satz-/Sonderzeichen ("PS5's," -> "ps5s")."""
    return re.sub(r"[\W_]", "", token.lower())


def _color_entries(word_colors) -> list[tuple[str, str]]:
    """word_colors (Liste von WordColor/Dicts/Paaren oder Mapping Wort -> Farbe) -> [(Schlüssel, Farbe)],
    längste Einträge zuerst (damit "GTA 6" vor "GTA" greift)."""
    if not word_colors:
        return []
    items: Iterable = word_colors.items() if isinstance(word_colors, Mapping) else word_colors
    out: dict[str, str] = {}
    for it in items:
        if isinstance(it, WordColor):
            word, color = it.word, it.color
        elif isinstance(it, Mapping):
            word, color = it.get("word", ""), it.get("color", "")
        else:
            try:
                word, color = it
            except (TypeError, ValueError):
                continue
        key = _key("".join(str(word).split()))
        if key and str(color) in COLOR_HEX:
            out.setdefault(key, str(color))
    return sorted(out.items(), key=lambda kv: -len(kv[0]))


def match_word_colors(tokens: list[str], word_colors) -> list[str | None]:
    """Je Token die Bedeutungsfarbe (ColorName) oder None. Groß-/Kleinschreibung und Satzzeichen egal;
    mehrteilige Einträge ("GTA 6") greifen über aufeinanderfolgende Tokens, "GTA6" passt auch auf "GTA 6".
    Plural/Genitiv ("XBOXs", "Xbox's") zählt mit."""
    entries = _color_entries(word_colors)
    out: list[str | None] = [None] * len(tokens)
    if not entries:
        return out
    keys = [_key(t) for t in tokens]
    i = 0
    while i < len(tokens):
        hit = None
        for key, color in entries:
            j, acc = i, ""
            while j < len(tokens) and len(acc) < len(key):
                acc += keys[j]
                j += 1
                if acc == key or (j - i == 1 and len(key) > 2 and acc in (key + "s",)):
                    hit = (j, color)
                    break
                if not key.startswith(acc):
                    break
            if hit:
                break
        if hit:
            for k in range(i, hit[0]):
                out[k] = hit[1]
            i = hit[0]
        else:
            i += 1
    return out


@dataclass
class _Ev:
    start: int  # Zentisekunden
    end: int
    chunk: int
    idx: int  # aktives Wort in der Zeile


# --------------------------------------------------------------------------- v2-Chunking


def _line_px(words: list[str], font, active_scale: int, outline: int) -> float:
    """Zeilenbreite in px inkl. vergrößertem aktivem Wort und Kontur (wie _fit_scale)."""
    if not words:
        return 0.0
    try:
        widths = [font.getlength(w) for w in words]
        space = font.getlength(" ")
    except Exception:
        return 0.0
    total = sum(widths) + space * (len(words) - 1)
    return total + max(widths) * max(0.0, active_scale / 100.0 * 1.04 - 1.0) + 2 * outline


@dataclass
class _Chunk:
    words: list[Word]
    brk: int | None = None  # Index des ersten Wortes der 2. Zeile (None = einzeilig)


def chunk_words_v2(words: list[Word], disp: list[str], font, max_w: float, *, active_scale: int, outline: int,
                   two_lines: bool = False, glue: list[bool] | None = None, max_words: int = V2_MAX_WORDS,
                   gap_s: float = 0.45) -> list[_Chunk]:
    """v2: 2–4 Wörter pro Chunk, breitenbasiert. Harte Grenzen wie v1 (Satzzeichen, Pausen > gap_s); innerhalb
    eines Satzstücks wählt ein DP die Aufteilung mit möglichst wenigen Chunks, vermeidet Einzelwörter und trennt
    keine zusammengehörigen Farbwörter (glue[i] = Wort i gehört zum Vorgänger, z. B. "GTA" "6").
    disp = Anzeigetexte (gleich lang wie words)."""
    glue = glue or [False] * len(words)
    segments: list[list[int]] = []
    cur: list[int] = []
    for i, w in enumerate(words):
        if cur:
            prev = words[cur[-1]]
            if w.start - prev.end > gap_s or prev.text.strip().endswith(V2_HARD_BREAK):
                segments.append(cur)
                cur = []
        cur.append(i)
    if cur:
        segments.append(cur)

    def layout(idx: list[int]) -> tuple[float, int | None] | None:
        """(Strafkosten, Umbruch) oder None (passt nicht)."""
        toks = [disp[k] for k in idx]
        w1 = _line_px(toks, font, active_scale, outline)
        if w1 <= max_w or len(toks) == 1:
            return 0.0, None
        if w1 * 0.93 <= max_w:
            return 0.1, None  # minimal verkleinert ist ruhiger als ein Zeilenumbruch
        best: tuple[float, int] | None = None
        if two_lines:
            for b in range(1, len(toks)):
                if glue[idx[b]]:
                    continue
                wt = _line_px(toks[:b], font, active_scale, outline)
                wb = _line_px(toks[b:], font, active_scale, outline)
                worst = max(wt, wb)
                if worst * V2_MIN_FIT > max_w:
                    continue
                pen = 0.35 + abs(wt - wb) / max(wt, wb, 1.0) * 0.8 + (0.8 if worst > max_w else 0.0)
                if best is None or pen < best[0]:
                    best = (pen, b)
        if w1 * V2_MIN_FIT <= max_w and (best is None or best[0] > 0.6):
            return 0.6, None  # leicht verkleinert einzeilig (ruhiger als ein unausgewogener Umbruch)
        return best

    out: list[_Chunk] = []
    for seg in segments:
        n = len(seg)
        cost = [0.0] + [float("inf")] * n
        back: list[tuple[int, int | None]] = [(0, None)] * (n + 1)
        for i in range(n):
            if cost[i] == float("inf"):
                continue
            for j in range(i + 1, min(n, i + max_words) + 1):
                lay = layout(seg[i:j])
                if lay is None:
                    break  # länger passt erst recht nicht
                pen, brk = lay
                c = cost[i] + 1.0 + pen
                if j - i == 1 and n > 1:
                    c += 2.5  # Einzelwort nur, wenn es nicht anders geht
                if j < n and glue[seg[j]]:
                    c += 6.0  # "GTA | 6" nicht trennen
                if j < n and not words[seg[j - 1]].text.strip().endswith(","):
                    c += 0.3  # lieber nach einem Komma trennen
                if any(words[seg[k]].text.strip().endswith(",") for k in range(i, j - 1)):
                    c += 0.5  # Komma mitten im Chunk (wird nicht angezeigt: "TELLING YOU YOU")
                dur = words[seg[j - 1]].end - words[seg[i]].start
                if dur > 2.6:
                    c += 1.0  # zu lange stehende Chunks vermeiden
                if c < cost[j]:
                    cost[j] = c
                    back[j] = (i, brk)
        bounds: list[tuple[int, int, int | None]] = []
        j = n
        while j > 0:
            i, brk = back[j]
            bounds.append((i, j, brk))
            j = i
        for i, j, brk in reversed(bounds):
            out.append(_Chunk([words[k] for k in seg[i:j]], brk))
    return out


# --------------------------------------------------------------------------- ASS


def build_ass(words: list[Word], timemap: TimeMap, highlight_words: list[str], layout: LayoutKind | str,
              settings: Settings, out_path: Path, *, word_colors=None) -> Path:
    """Wörter (Quellzeit) über timemap in Ausgabezeit umrechnen (herausgeschnittene Wörter fallen weg),
    gruppieren, ASS schreiben (PlayResX/Y = render.width/height). Im Split-Layout sitzen Captions auf
    der Naht zwischen Facecam und Gameplay; sonst bei captions.y_ratio (auch layout "shots"). Leere
    Wortliste -> gültige ASS ohne Events.

    word_colors (list[WordColor] oder Mapping Wort -> ColorName): diese Wörter immer in ihrer Farbe (auch aktiv,
    Pop bleibt). Mit word_colors oder layout "shots" gilt der v2-Stil (2–4 Wörter, größer); render.caption_glow
    legt eine weiche Farbebene (Style "Glow") unter den Text."""
    cfg = settings.config
    r, brand, cap = cfg.render, cfg.brand, cfg.render.captions
    W, H = int(r.width), int(r.height)
    fpath = str(_fonts.font_path(cfg.paths.fonts_dir, brand.font_captions))
    family = _fonts.family_name(fpath)
    entries = _color_entries(word_colors)
    v2 = layout == SHOTS_LAYOUT or bool(entries)
    glow = bool(getattr(r, "caption_glow", False))
    em = round(cap.font_size * V2_FONT_SCALE) if v2 else cap.font_size
    # captions.font_size = Em-Größe in Pixeln (wie bei Pillow/CSS); libass misst anders -> umrechnen
    ass_fs = max(10, round(em * _fonts.ass_size_factor(fpath)))
    measure_font = _fonts.load(fpath, em)

    seam = layout == "facecam_split"
    x = round(W * CAPTION_X_RATIO)
    y = round(caption_y_ratio(cfg, layout) * H)
    max_w = W * (MAX_W_RATIO_SEAM if seam else MAX_W_RATIO)

    # 1) Wörter in Ausgabezeit (teilweise geschnittene werden geklemmt); Farben vorher auf Quellwörtern
    total = timemap.duration
    src = sorted(words, key=lambda w: (w.start, w.end))
    src_colors = match_word_colors([w.text for w in src], entries)
    mapped: list[Word] = []
    named: list[str | None] = []
    for w, col in zip(src, src_colors, strict=True):
        if not display_text(w.text, brand.caption_uppercase, r.caption_censor):
            continue
        iv = map_interval(timemap, w.start, max(w.end, w.start))
        if iv is None:
            continue
        s, e = iv
        if s >= total:
            continue
        mapped.append(Word(text=w.text.strip(), start=s, end=max(e, s + MIN_WORD_S), prob=w.prob))
        named.append(col)
    color_of = {id(w): c for w, c in zip(mapped, named, strict=True)}

    if v2:
        mdisp = [display_text(w.text, brand.caption_uppercase, r.caption_censor) for w in mapped]
        glue = [i > 0 and c is not None and c == named[i - 1] for i, c in enumerate(named)]
        cks = chunk_words_v2(mapped, mdisp, measure_font, max_w, active_scale=cap.active_scale,
                             outline=cap.outline, two_lines=layout == SHOTS_LAYOUT, glue=glue)
    else:
        cks = [_Chunk(ch) for ch in chunk_words(mapped, brand.caption_max_words, brand.caption_max_chars)]
    chunks = [c.words for c in cks]

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
    disp = [[display_text(w.text, brand.caption_uppercase, r.caption_censor) for w in ch] for ch in chunks]
    white, primary, secondary = (_tag_color(brand.color_text), _tag_color(brand.color_primary),
                                 _tag_color(brand.color_secondary))
    # je Chunk: Zeilen als (Wortindex von, bis), Fit-Skalierung, Farben (inaktiv / aktiv / Glow / Glow aktiv)
    plans: list[tuple[list[tuple[int, int]], float, list[str], list[str], list[str], list[str]]] = []
    for ci, ch in enumerate(chunks):
        brk = cks[ci].brk
        spans = [(0, len(ch))] if brk is None else [(0, brk), (brk, len(ch))]
        fit = min(_fit_scale(disp[ci][a:b], measure_font, cap.active_scale, cap.outline, max_w) for a, b in spans)
        inact, act, halo, halo_act = [], [], [], []
        for w in ch:
            col = color_of.get(id(w))
            if col is not None:
                hexc = resolve_color(col, brand)
                inact.append(_tag_color(hexc))
                act.append(_tag_color(hexc))
                halo.append(_tag_color(brand.color_primary if col == "white" else hexc))
                halo_act.append(halo[-1])
            else:
                h = _norm(w.text) in hl
                inact.append(secondary if h else white)
                # v2: aktives Wort nur Pop (bleibt weiß/Highlight) – Farbe gehört den Bedeutungswörtern
                act.append((secondary if h else white) if v2 else primary)
                halo.append(secondary if h else primary)
                halo_act.append(halo[-1] if v2 else primary)
        plans.append((spans, fit, inact, act, halo, halo_act))

    # zweite Zeile: Abstand der Zeilenmitten (Versalhöhe + Luft), erste Zeile bleibt auf y
    line_step = round(em * 1.02)
    lines: list[str] = []
    glow_lines: list[str] = []
    seen_chunks: set[int] = set()
    for i, ev in enumerate(events):
        first = ev.chunk not in seen_chunks
        seen_chunks.add(ev.chunk)
        prev_end = events[i - 1].end if i > 0 else -1
        next_start = events[i + 1].start if i + 1 < len(events) else None
        last_of_chunk = next_start is None or events[i + 1].chunk != ev.chunk
        fade_in = FADE_IN_MS if first and prev_end < ev.start else 0
        fade_out = FADE_OUT_MS if last_of_chunk and (next_start is None or next_start > ev.end) else 0
        spans, fit, inact, act, halo, halo_act = plans[ev.chunk]
        fade = f"\\fad({fade_in},{fade_out})" if fade_in or fade_out else ""
        for li, (a, b) in enumerate(spans):
            ly = y + li * round(line_step * fit)
            active = ev.idx - a if a <= ev.idx < b else -1
            text = _event_text(disp[ev.chunk][a:b], inact[a:b], act[a:b], active, first, fit, cap.active_scale,
                               cap.pop_ms)
            head = f"{{\\an5\\pos({x},{ly}){fade}}}"
            lines.append(f"Dialogue: 0,{_cs_time(ev.start)},{_cs_time(ev.end)},Main,,0,0,0,,{head}{text}")
            if glow:
                gtext = _event_text(disp[ev.chunk][a:b], halo[a:b], halo_act[a:b], active, first, fit,
                                    cap.active_scale, cap.pop_ms, glow=True)
                ghead = f"{{\\an5\\pos({x},{ly})\\blur{GLOW_BLUR}{fade}}}"
                glow_lines.append(f"Dialogue: 0,{_cs_time(ev.start)},{_cs_time(ev.end)},Glow,,0,0,0,,{ghead}{gtext}")

    styles = [
        f"Style: Main,{family},{ass_fs},{ass_color(brand.color_text)},{ass_color(brand.color_primary)},"
        f"{ass_color(brand.color_outline)},{ass_color('#000000', 0x80)},0,0,0,0,100,100,0,0,1,"
        f"{cap.outline},{cap.shadow},5,0,0,0,1",
    ]
    if glow:
        # Halo: Füllung + breite Kontur in Wortfarbe, halbtransparent, weichgezeichnet, ohne Schatten
        styles.append(
            f"Style: Glow,{family},{ass_fs},{ass_color(brand.color_primary, GLOW_ALPHA)},"
            f"{ass_color(brand.color_primary, GLOW_ALPHA)},{ass_color(brand.color_primary, GLOW_ALPHA)},"
            f"{ass_color('#000000', 0xFF)},0,0,0,0,100,100,0,0,1,{cap.outline + GLOW_BORD_EXTRA},0,5,0,0,0,1")
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
        *styles,
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Glow-Events zuerst: libass zeichnet bei gleichem Layer in Dateireihenfolge -> Halo liegt unter dem Text
    out_path.write_text("\n".join(header + glow_lines + lines) + "\n", encoding="utf-8")
    log.debug("Captions: %d words, %d lines, %d events -> %s", len(mapped), len(chunks), len(lines),
              out_path.name)
    return out_path


def _fit_scale(words: list[str], font, active_scale: int, outline: int, max_w: float) -> float:
    """Skalierung (<= 1), damit die Zeile inkl. vergrößertem aktivem Wort in max_w passt."""
    if not words:
        return 1.0
    total = _line_px(words, font, active_scale, outline)
    return min(1.0, max_w / total) if total > 0 else 1.0


def _event_text(words: list[str], colors: list[str], act_colors: list[str], active: int, first: bool, fit: float,
                active_scale: int, pop_ms: int, *, glow: bool = False) -> str:
    """Override-Text einer Zeile. colors/act_colors = Tag-Farbe je Wort (inaktiv/aktiv); active = -1: kein aktives
    Wort in dieser Zeile. glow=True: Farbe auch für die Kontur (Halo-Ebene)."""
    base = round(100 * fit)
    act = round(active_scale * fit)
    over = round(act * 1.06)  # kurzes Überschwingen
    p1 = max(20, round(pop_ms * 0.6))
    p2 = max(p1 + 10, pop_ms)
    runs: list[str] = []
    for i, word in enumerate(words):
        if i == active:
            col = f"{act_colors[i]}\\3c{act_colors[i]}" if glow else act_colors[i]
            s0 = round(base * CHUNK_POP_FROM) if first else base
            if first:
                p1a, p2a = round(CHUNK_POP_MS * 0.7), CHUNK_POP_MS + 20
            else:
                p1a, p2a = p1, p2
            tags = (f"\\c{col}\\fscx{s0}\\fscy{s0}\\t(0,{p1a},0.6,\\fscx{over}\\fscy{over})"
                    f"\\t({p1a},{p2a},\\fscx{act}\\fscy{act})")
        else:
            color = f"{colors[i]}\\3c{colors[i]}" if glow else colors[i]
            if first:
                s0 = round(base * CHUNK_POP_FROM)
                tags = f"\\c{color}\\fscx{s0}\\fscy{s0}\\t(0,{CHUNK_POP_MS},0.5,\\fscx{base}\\fscy{base})"
            else:
                tags = f"\\c{color}\\fscx{base}\\fscy{base}"
        runs.append(f"{{{tags}}}{word}")
    return " ".join(runs)
