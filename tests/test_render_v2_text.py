"""Edit-Engine v2: Bedeutungsfarben, Glow, "shots"-Layout, v2-Chunking (Captions) und Outro-Karte (Overlays)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from wdvi.render import captions, overlays
from wdvi.schemas import TimeMap, Word, WordColor

DIALOGUE = re.compile(r"^Dialogue: (\d+),(\d+):(\d\d):(\d\d)\.(\d\d),(\d+):(\d\d):(\d\d)\.(\d\d),(\w+),,0,0,0,,(.*)$")

BLUE = "&HFF9C2D&"  # #2D9CFF als Override-Farbe (BGR)
GREEN = "&H6BE331&"  # #31E36B
PINK = "&H872DFF&"  # brand.color_primary #FF2D87
ORANGE = "&H008AFF&"  # brand.color_secondary #FF8A00
WHITE = "&HFFFFFF&"


def _seq(text: str, t0: float = 0.3, step: float = 0.3) -> list[Word]:
    out, t = [], t0
    for tok in text.split():
        out.append(Word(text=tok, start=round(t, 2), end=round(t + step * 0.85, 2)))
        t += step
    return out


def _events(ass: Path, style: str = "Main") -> list[tuple[float, float, str, int]]:
    evs = []
    for line in ass.read_text(encoding="utf-8").splitlines():
        m = DIALOGUE.match(line)
        if m and m.group(10) == style:
            g = m.groups()
            s = int(g[1]) * 3600 + int(g[2]) * 60 + int(g[3]) + int(g[4]) / 100
            e = int(g[5]) * 3600 + int(g[6]) * 60 + int(g[7]) + int(g[8]) / 100
            evs.append((s, e, g[10], int(g[0])))
    return evs


def _runs(text: str) -> list[tuple[str, str]]:
    """(Tags, Wort) je Wort eines Events."""
    return re.findall(r"\{([^}]*)\}([^{ ]+)", text)


def _word_color(text: str, word: str) -> str | None:
    for tags, w in _runs(text):
        if w == word:
            m = re.search(r"\\c(&H[0-9A-F]{6}&)", tags)
            return m.group(1) if m else None
    return None


def _plain(text: str) -> str:
    return re.sub(r"\{[^}]*\}", "", text)


@pytest.fixture
def no_glow(settings):
    settings.config.render.caption_glow = False
    return settings


TM = TimeMap(segments=[(0.0, 30.0)])


# --------------------------------------------------------------------------- Farben


def test_color_hex_table():
    assert captions.COLOR_HEX["blue"] == "#2D9CFF"
    assert captions.COLOR_HEX["green"] == "#31E36B"
    assert captions.COLOR_HEX["yellow"] == "#FFE135"
    assert captions.COLOR_HEX["purple"] == "#A35BFF"
    assert captions.COLOR_HEX["red"] == "#FF3B3B"
    assert captions.COLOR_HEX["white"] == "#FFFFFF"
    assert set(captions.COLOR_HEX) == {"white", "pink", "orange", "yellow", "green", "blue", "purple", "red"}
    assert overlays.COLOR_HEX is captions.COLOR_HEX


def test_resolve_color_follows_brand(settings):
    brand = settings.config.brand
    brand.color_primary, brand.color_secondary = "#112233", "#445566"
    assert captions.resolve_color("pink", brand) == "#112233"
    assert captions.resolve_color("orange", brand) == "#445566"
    assert captions.resolve_color("blue", brand) == "#2D9CFF"
    assert captions.resolve_color("pink") == captions.COLOR_HEX["pink"]


def test_match_word_colors_case_punctuation_and_phrases():
    toks = ["Chat,", "PlayStation", "or", "XBOX?", "gta", "6!", "GTA6", "xbox's", "Xboxes"]
    wc = [WordColor(word="playstation", color="blue"), WordColor(word="Xbox", color="green"),
          WordColor(word="GTA 6", color="pink")]
    got = captions.match_word_colors(toks, wc)
    assert got == [None, "blue", None, "green", "pink", "pink", "pink", "green", None]


def test_match_word_colors_accepts_mapping_and_ignores_unknown():
    got = captions.match_word_colors(["PS5", "rocks"], {"ps5": "blue", "rocks": "rainbow"})
    assert got == ["blue", None]
    assert captions.match_word_colors(["a"], None) == [None]


def test_match_word_colors_longest_entry_wins():
    wc = [WordColor(word="GTA", color="orange"), WordColor(word="GTA 6", color="pink")]
    assert captions.match_word_colors(["GTA", "6", "GTA", "5"], wc) == ["pink", "pink", "orange", None]


# --------------------------------------------------------------------------- build_ass: word_colors


def test_word_colors_drawn_in_their_colour_also_when_active(no_glow, tmp_path):
    words = _seq("should I play on PlayStation or Xbox")
    wc = [WordColor(word="PlayStation", color="blue"), WordColor(word="xbox", color="green")]
    out = captions.build_ass(words, TM, [], "face_track", no_glow, tmp_path / "c.ass", word_colors=wc)
    evs = _events(out)
    ps = [e for e in evs if "PLAYSTATION" in e[2]]
    xb = [e for e in evs if "XBOX" in e[2]]
    assert ps and xb
    assert all(_word_color(e[2], "PLAYSTATION") == BLUE for e in ps)
    assert all(_word_color(e[2], "XBOX") == GREEN for e in xb)
    # aktiv: Farbe bleibt, aber Pop (Skalierungs-Animation) ist da
    active = next(e for e in ps if "\\t(" in dict((w, t) for t, w in _runs(e[2]))["PLAYSTATION"]
                  and e[0] == pytest.approx(words[4].start, abs=0.011))
    assert _word_color(active[2], "PLAYSTATION") == BLUE


def test_multiword_entry_colours_consecutive_words_and_stays_in_one_chunk(no_glow, tmp_path):
    words = _seq("I need GTA 6 right now")
    out = captions.build_ass(words, TM, [], "shots", no_glow, tmp_path / "c.ass",
                             word_colors=[WordColor(word="GTA 6", color="pink")])
    evs = _events(out)
    for _, _, text, _ in evs:
        plain = _plain(text)
        if "GTA" in plain or re.search(r"\b6\b", plain):
            assert "GTA" in plain and "6" in plain.split()  # nie getrennt
            assert _word_color(text, "GTA") == PINK and _word_color(text, "6") == PINK


def test_highlight_words_keep_v1_behaviour_without_match(no_glow, tmp_path):
    words = _seq("that was insane on xbox")
    out = captions.build_ass(words, TM, ["insane"], "face_track", no_glow, tmp_path / "c.ass",
                             word_colors=[WordColor(word="xbox", color="green")])
    evs = _events(out)
    inactive_insane = [e for e in evs if "INSANE" in e[2] and _word_color(e[2], "INSANE") != WHITE]
    assert inactive_insane and all(_word_color(e[2], "INSANE") == ORANGE for e in inactive_insane)


def test_v1_output_unchanged_with_empty_word_colors(no_glow, sample_transcript, tmp_path):
    a = captions.build_ass(sample_transcript.words(), TM, ["insane"], "face_track", no_glow, tmp_path / "a.ass")
    b = captions.build_ass(sample_transcript.words(), TM, ["insane"], "face_track", no_glow, tmp_path / "b.ass",
                           word_colors=[])
    c = captions.build_ass(sample_transcript.words(), TM, ["insane"], "face_track", no_glow, tmp_path / "c.ass",
                           word_colors={})
    assert a.read_bytes() == b.read_bytes() == c.read_bytes()
    text = a.read_text(encoding="utf-8")
    assert "Style: Glow" not in text and "\\blur" not in text
    assert all(line.startswith("Dialogue: 0,") and ",Main," in line
               for line in text.splitlines() if line.startswith("Dialogue"))


# --------------------------------------------------------------------------- Glow


def test_glow_layer_below_text(settings, tmp_path):
    settings.config.render.caption_glow = True
    words = _seq("is PlayStation better")
    out = captions.build_ass(words, TM, [], "shots", settings, tmp_path / "c.ass",
                             word_colors={"playstation": "blue"})
    text = out.read_text(encoding="utf-8")
    glow_style = next(ln for ln in text.splitlines() if ln.startswith("Style: Glow,"))
    f = glow_style.split(",")
    cap = settings.config.render.captions
    assert int(f[16]) > cap.outline and f[17] == "0"  # breite Glow-Kontur, kein Schatten
    assert f[3].startswith("&H70")  # halbtransparent
    main_style = next(ln for ln in text.splitlines() if ln.startswith("Style: Main,")).split(",")
    assert main_style[16] == str(cap.outline) and main_style[5] == "&H00000000"  # schwarze Kontur oben bleibt
    glow, main = _events(out, "Glow"), _events(out)
    assert len(glow) == len(main) and glow
    # Glow-Events stehen vor allen Main-Events (libass: gleiche Ebene -> Dateireihenfolge)
    lines = [ln for ln in text.splitlines() if ln.startswith("Dialogue")]
    last_glow = max(i for i, ln in enumerate(lines) if ",Glow," in ln)
    first_main = min(i for i, ln in enumerate(lines) if ",Main," in ln)
    assert last_glow < first_main
    for (gs, ge, gt, _), (ms, me, mt, _) in zip(glow, main, strict=True):
        assert (gs, ge) == (ms, me) and _plain(gt) == _plain(mt)
        assert "\\blur" in gt and "\\3c" in gt
        # PLAYSTATION glüht blau, weiße Wörter in Markenfarbe
        if "PLAYSTATION" in _plain(gt):
            assert _word_color(gt, "PLAYSTATION") == BLUE
        if "IS" in _plain(gt).split():
            assert _word_color(gt, "IS") == PINK


def test_glow_default_on_v1_layout_keeps_main_events(settings, sample_transcript, tmp_path):
    assert settings.config.render.caption_glow is True
    out = captions.build_ass(sample_transcript.words(), TM, [], "gameplay_fit", settings, tmp_path / "c.ass")
    assert len(_events(out)) == len(sample_transcript.words())
    assert len(_events(out, "Glow")) == len(sample_transcript.words())


# --------------------------------------------------------------------------- Layout "shots" / v2-Chunking


def test_caption_y_ratio_shots(settings):
    cfg = settings.config
    assert captions.caption_y_ratio(cfg, "shots") == cfg.render.captions.y_ratio


def test_shots_layout_fixed_position_and_larger_font(no_glow, tmp_path):
    words = _seq("no way he actually did that")
    v1 = captions.build_ass(words, TM, [], "face_track", no_glow, tmp_path / "a.ass").read_text(encoding="utf-8")
    v2 = captions.build_ass(words, TM, [], "shots", no_glow, tmp_path / "b.ass").read_text(encoding="utf-8")
    fs = [int(next(ln for ln in t.splitlines() if ln.startswith("Style: Main,")).split(",")[2]) for t in (v1, v2)]
    assert fs[1] > fs[0]
    y = round(no_glow.config.render.captions.y_ratio * 1920)
    assert f"\\pos(540,{y})" in v2


def test_v2_chunks_have_two_to_four_words(no_glow, tmp_path):
    words = _seq("chat should I play it on the new console or wait for the pc version honestly")
    evs = _events(captions.build_ass(words, TM, [], "shots", no_glow, tmp_path / "c.ass"))
    # zeitgleiche Zeilen eines Chunks zusammenfassen
    shown: dict[float, list[str]] = {}
    for s, _e, text, _ in evs:
        shown.setdefault(s, []).extend(_plain(text).split())
    sizes = {len(v) for v in shown.values()}
    assert sizes <= {2, 3, 4}


def test_v2_comma_is_soft_break(no_glow, tmp_path):
    words = _seq("chat, should I play")
    evs = _events(captions.build_ass(words, TM, [], "shots", no_glow, tmp_path / "c.ass"))
    first = [_plain(t) for s, _e, t, _ in evs if s == evs[0][0]]
    assert "CHAT" in " ".join(first) and "SHOULD" in " ".join(first)


def test_v2_long_chunk_uses_two_lines_in_shots(no_glow, tmp_path):
    words = _seq("absolutely unbelievable disgusting resist")
    evs = _events(captions.build_ass(words, TM, [], "shots", no_glow, tmp_path / "c.ass"))
    t0 = evs[0][0]
    same = [e for e in evs if e[0] == t0]
    if len(same) == 2:  # zweizeilig: zwei Events, zweite Zeile tiefer
        ys = [int(re.search(r"\\pos\(\d+,(\d+)\)", e[2]).group(1)) for e in same]
        assert ys[1] > ys[0]
    for _s, _e, text, _ in evs:
        scales = [int(v) for v in re.findall(r"\\fscx(\d+)", text)]
        assert min(scales) >= 50  # nicht winzig geschrumpft


def test_v2_single_word_only_when_isolated(no_glow, tmp_path):
    words = _seq("what? i mean he really did that")
    evs = _events(captions.build_ass(words, TM, [], "shots", no_glow, tmp_path / "c.ass"))
    shown: dict[float, list[str]] = {}
    for s, _e, text, _ in evs:
        shown.setdefault(s, []).extend(_plain(text).split())
    singles = [v for v in shown.values() if len(v) == 1]
    assert singles == [["WHAT?"]] or all(v == ["WHAT?"] for v in singles)


def test_v2_ass_renders_with_libass(settings, tmp_path):
    words = _seq("is PlayStation better than Xbox? GTA 6 plays best on PS5")
    ass = captions.build_ass(words, TM, [], "shots", settings, tmp_path / "c.ass",
                             word_colors=[WordColor(word="playstation", color="blue"),
                                          WordColor(word="xbox", color="green"),
                                          WordColor(word="GTA 6", color="pink")])
    fonts = tmp_path / "fonts"
    fonts.mkdir()
    (fonts / "ArchivoBlack-Regular.ttf").write_bytes(
        (settings.config.paths.fonts_dir / "ArchivoBlack-Regular.ttf").read_bytes())
    png = tmp_path / "f.png"
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                           "color=c=0x202020:s=1080x1920:d=2:r=10", "-vf",
                           f"ass={ass.name}:fontsdir=fonts,trim=start=0.7", "-frames:v", "1", png.name],
                          cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    arr = np.asarray(Image.open(png).convert("RGB")).astype(int)
    band = arr[1100:1450]
    blue = (band[..., 2] > 200) & (band[..., 0] < 100)
    assert blue.sum() > 500  # PLAYSTATION blau eingebrannt


# --------------------------------------------------------------------------- Outro-Karte


def test_outro_png_size_and_alpha(settings, tmp_path):
    p = overlays.render_outro_png("Is PlayStation better than Xbox?", settings, tmp_path / "o.png")
    img = Image.open(p)
    assert img.mode == "RGBA" and img.width == settings.config.render.width
    a = np.asarray(img.getchannel("A"))
    assert a[0, 0] == 0 and a.max() == 255  # transparenter Rand, deckendes Panel
    assert 200 < img.height < settings.config.render.height * 0.45


def test_outro_png_semantic_word_colors(settings, tmp_path):
    p = overlays.render_outro_png("Is PlayStation better than Xbox?", settings, tmp_path / "o.png",
                                  word_colors=[WordColor(word="playstation", color="blue"),
                                               WordColor(word="xbox", color="green")])
    rgb = np.asarray(Image.open(p).convert("RGB")).astype(int)
    blue = (abs(rgb[..., 0] - 0x2D) < 20) & (abs(rgb[..., 1] - 0x9C) < 20) & (abs(rgb[..., 2] - 0xFF) < 20)
    green = (abs(rgb[..., 0] - 0x31) < 20) & (abs(rgb[..., 1] - 0xE3) < 20) & (abs(rgb[..., 2] - 0x6B) < 20)
    assert blue.sum() > 2000 and green.sum() > 1000


def test_outro_png_auto_keywords_in_brand_colours(settings):
    toks = overlays._hook_tokens("WOULD YOU SURVIVE THIS FEAR CHAIN?")
    cols = overlays._outro_colors(toks, settings.config.brand, None)
    brand = settings.config.brand
    assert cols.count(brand.color_primary) == 1 and cols.count(brand.color_secondary) == 1
    assert cols[0] is None and cols[1] is None  # Füllwörter bleiben weiß
    cols = overlays._outro_colors(overlays._hook_tokens("IS GTA 6 WORTH IT?"), brand, None)
    assert cols[1] is not None and cols[2] == cols[1]  # "GTA 6" zusammen


@pytest.mark.parametrize("text", ["Fair?", "Is this the most disgusting resist streak in WoW history or just bad luck?",
                                  "", "   "])
def test_outro_png_auto_fit_up_to_three_lines(settings, tmp_path, text):
    p = overlays.render_outro_png(text, settings, tmp_path / "o.png")
    img = Image.open(p)
    assert img.width == 1080
    a = np.asarray(img.getchannel("A"))
    assert a[:, 0].max() < 16 and a[:, -1].max() < 16  # Panel passt ins Bild, nur Glow-Ausläufer am Rand
    assert a.max() == 255


def test_outro_distinct_from_hook_pill(settings, tmp_path):
    hook = Image.open(overlays.render_hook_png("Is PS5 better?", settings, tmp_path / "h.png")).convert("RGBA")
    outro = Image.open(overlays.render_outro_png("Is PS5 better?", settings, tmp_path / "o.png")).convert("RGBA")
    # Outro: dunkles Glas-Panel (Mitte dunkel), Hook: heller Verlauf
    def panel_mean(img):
        arr = np.asarray(img).astype(int)
        mask = arr[..., 3] == 255
        return arr[..., :3][mask].mean()
    assert panel_mean(outro) < panel_mean(hook)


def test_v2_no_hidden_comma_inside_chunk(no_glow, tmp_path):
    """Kommas werden nicht angezeigt -> "I'M TELLING YOU YOU" sähe wie ein Tippfehler aus; der Chunk endet
    am Komma (Pikaboo-Clip: "I'm telling you, you can't get a single resist.")."""
    words = _seq("I'm telling you, you can't get a single resist.")
    evs = _events(captions.build_ass(words, TM, [], "shots", no_glow, tmp_path / "c.ass"))
    shown: dict[float, list[str]] = {}
    for s, _e, text, _ in evs:
        shown.setdefault(s, []).extend(_plain(text).split())
    chunks = []
    for toks in shown.values():
        if toks not in chunks:
            chunks.append(toks)
    assert chunks[0] == ["I'M", "TELLING", "YOU"]
    assert not any(c[i] == c[i + 1] for c in chunks for i in range(len(c) - 1))


def test_censor_word_masks_common_profanity_only():
    from wdvi.render.captions import censor_word, display_text

    assert censor_word("fucking") == "f*cking"
    assert censor_word("Fuck") == "F*ck"
    assert censor_word("motherfucker") == "motherf*cker"
    assert censor_word("shit") == "sh*t"
    assert censor_word("bitches") == "b*tches"
    assert censor_word("shift") == "shift"
    assert censor_word("fun") == "fun"
    assert display_text("fucking", True, True) == "F*CKING"
    assert display_text("fucking", True, False) == "FUCKING"


def test_emoji_png_falls_back_without_cairo(tmp_path, monkeypatch):
    """Windows ohne libcairo: emoji_png nutzt das Twemoji-PNG statt des SVG-Renderers."""
    import io
    import sys

    from PIL import Image

    from wdvi.render import overlays

    buf = io.BytesIO()
    Image.new("RGBA", (64, 64), (255, 0, 0, 255)).save(buf, "PNG")
    edir = tmp_path / "emoji"
    edir.mkdir()
    (edir / "1f480.svg").write_bytes(b"<svg xmlns='http://www.w3.org/2000/svg'></svg>")
    monkeypatch.setitem(sys.modules, "cairosvg", None)  # import cairosvg -> ImportError
    monkeypatch.setattr(overlays, "_png_fallback", lambda name, size: buf.getvalue())
    monkeypatch.setattr(overlays, "_failed", {})
    out = overlays.emoji_png("💀", 120, tmp_path)
    assert out is not None and out.is_file()
