"""Wort-für-Wort-Captions (Chunking + ASS)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from wdvi.render import captions
from wdvi.schemas import TimeMap, Word

DIALOGUE = re.compile(r"^Dialogue: 0,(\d+):(\d\d):(\d\d)\.(\d\d),(\d+):(\d\d):(\d\d)\.(\d\d),Main,,0,0,0,,(.*)$")


def _w(text: str, start: float, end: float) -> Word:
    return Word(text=text, start=start, end=end)


def _seq(text: str, t0: float = 0.5, step: float = 0.4, dur: float = 0.3) -> list[Word]:
    out, t = [], t0
    for tok in text.split():
        out.append(_w(tok, round(t, 2), round(t + dur, 2)))
        t += step
    return out


def _events(ass: Path) -> list[tuple[float, float, str]]:
    evs = []
    for line in ass.read_text(encoding="utf-8").splitlines():
        m = DIALOGUE.match(line)
        if m:
            g = m.groups()
            s = int(g[0]) * 3600 + int(g[1]) * 60 + int(g[2]) + int(g[3]) / 100
            e = int(g[4]) * 3600 + int(g[5]) * 60 + int(g[6]) + int(g[7]) / 100
            evs.append((s, e, g[8]))
    return evs


def _plain(text: str) -> str:
    return re.sub(r"\{[^}]*\}", "", text)


def _active(text: str) -> str | None:
    """Das aktive (pinke) Wort eines Events."""
    m = re.search(r"\{\\c&H872DFF&[^}]*\}(\S+)", text)
    return m.group(1) if m else None


# --------------------------------------------------------------------------- chunk_words


def test_chunk_max_words():
    chunks = captions.chunk_words(_seq("a b c d e f g"), max_words=3, max_chars=99)
    assert [len(c) for c in chunks] == [3, 3, 1]


def test_chunk_max_chars():
    chunks = captions.chunk_words(_seq("unbelievable absolutely insane"), max_words=3, max_chars=18)
    assert [[w.text for w in c] for c in chunks] == [["unbelievable"], ["absolutely", "insane"]]


def test_chunk_breaks_on_gap_and_punctuation():
    words = [_w("no", 0.0, 0.2), _w("way.", 0.3, 0.5), _w("he", 0.6, 0.8), _w("did", 0.85, 1.0),
             _w("it", 2.0, 2.2)]
    chunks = captions.chunk_words(words, max_words=3, max_chars=18)
    assert [[w.text for w in c] for c in chunks] == [["no", "way."], ["he", "did"], ["it"]]


def test_chunk_skips_empty_words():
    chunks = captions.chunk_words([_w(" ", 0, 0.1), _w("yo", 0.2, 0.3)], max_words=3, max_chars=18)
    assert [[w.text for w in c] for c in chunks] == [["yo"]]


# --------------------------------------------------------------------------- build_ass


def test_ass_header_and_style(settings, sample_transcript, tmp_path):
    tm = TimeMap(segments=[(0.0, 8.0)])
    out = captions.build_ass(sample_transcript.words(), tm, ["insane"], "gameplay_fit", settings,
                             tmp_path / "c.ass")
    text = out.read_text(encoding="utf-8")
    assert "PlayResX: 1080" in text and "PlayResY: 1920" in text
    assert "ScaledBorderAndShadow: yes" in text and "WrapStyle: 2" in text
    style = next(ln for ln in text.splitlines() if ln.startswith("Style: Main,"))
    fields = style.split(",")
    assert fields[1] == "Archivo Black"  # echter Familienname aus der TTF
    assert fields[3] == "&H00FFFFFF"  # weiß
    assert fields[5] == "&H00000000"  # schwarze Kontur
    assert fields[6] == "&H80000000"  # halbtransparenter Schatten
    assert fields[7] == "0"  # nicht fett
    assert fields[16] == "7" and fields[17] == "3"  # Outline / Shadow
    assert fields[18] == "5"  # Alignment Mitte


def test_ass_events_content_and_order(settings, sample_transcript, tmp_path):
    tm = TimeMap(segments=[(0.0, 8.0)])
    out = captions.build_ass(sample_transcript.words(), tm, ["insane"], "gameplay_fit", settings,
                             tmp_path / "c.ass")
    evs = _events(out)
    assert len(evs) == len(sample_transcript.words())  # ein Event je Wort
    for (s1, e1, _), (s2, _e2, _) in zip(evs, evs[1:], strict=False):
        assert e1 <= s2 + 1e-9  # keine Überlappung
        assert s1 < e1
    first = evs[0][2]
    assert "\\pos(540,1267)" in first  # y_ratio 0.66 * 1920
    assert _plain(first) == "NO WAY HE"  # Großbuchstaben, ganze Zeile
    assert "\\c&H872DFF&" in first  # aktives Wort in color_primary (#FF2D87 -> BGR)
    assert "\\t(" in first and "\\fscx" in first  # Pop-Animation
    insane = [e for e in evs if "INSANE" in e[2]]
    assert any("\\c&H008AFF&" in e[2] for e in insane)  # highlight in color_secondary (#FF8A00)


def test_ass_split_layout_sits_on_seam(settings, sample_transcript, tmp_path):
    out = captions.build_ass(sample_transcript.words(), TimeMap(segments=[(0.0, 8.0)]), [], "facecam_split",
                             settings, tmp_path / "c.ass")
    assert "\\pos(540,768)" in out.read_text(encoding="utf-8")  # 0.40 * 1920


def test_ass_words_in_cuts_are_dropped_and_times_shifted(settings, tmp_path):
    words = [_w("first", 0.5, 0.8), _w("cutaway", 2.2, 2.6), _w("partial", 2.9, 3.4), _w("after", 4.0, 4.3)]
    tm = TimeMap(segments=[(0.0, 2.0), (3.0, 6.0)])  # 2.0-3.0 herausgeschnitten
    evs = _events(captions.build_ass(words, tm, [], "gameplay_fit", settings, tmp_path / "c.ass"))
    texts = " ".join(_plain(e[2]) for e in evs)
    assert "CUTAWAY" not in texts
    assert "PARTIAL" in texts and "AFTER" in texts
    partial = next(e for e in evs if _active(e[2]) == "PARTIAL")
    assert partial[0] == pytest.approx(2.0, abs=0.011)  # 3.0 (Quelle) -> 2.0 (Ausgabe), Anfang geklemmt
    after = next(e for e in evs if _active(e[2]) == "AFTER")
    assert after[0] == pytest.approx(3.0, abs=0.011)  # 4.0 -> 3.0


def test_ass_small_gaps_bridged_large_gaps_kept(settings, tmp_path):
    words = [_w("one", 0.0, 0.3), _w("two.", 0.4, 0.6),  # Zeile 1 endet (Satzzeichen)
             _w("three", 0.7, 1.0),  # Zeile 2 beginnt 0.1 s später -> überbrücken
             _w("four", 3.0, 3.3)]  # große Pause -> Lücke bleibt
    evs = _events(captions.build_ass(words, TimeMap(segments=[(0.0, 5.0)]), [], "gameplay_fit", settings,
                                     tmp_path / "c.ass"))
    two = next(e for e in evs if _active(e[2]) == "TWO")
    three = next(e for e in evs if _plain(e[2]) == "THREE")
    assert two[1] == pytest.approx(three[0])  # überbrückt
    four = next(e for e in evs if _plain(e[2]) == "FOUR")
    assert four[0] - three[1] > 0.25  # echte Pause bleibt sichtbar leer
    assert "\\fad(" in four[2]  # nach Pause sanft einblenden


def test_ass_escapes_braces_and_backslashes(settings, tmp_path):
    words = [_w("{evil}", 0.1, 0.3), _w("back\\slash", 0.4, 0.6)]
    out = captions.build_ass(words, TimeMap(segments=[(0.0, 2.0)]), [], "gameplay_fit", settings,
                             tmp_path / "c.ass")
    for _, _, text in _events(out):
        plain = _plain(text)
        assert "{" not in plain and "}" not in plain and "\\" not in plain


def test_ass_empty_words_is_valid(settings, tmp_path):
    out = captions.build_ass([], TimeMap(segments=[(0.0, 3.0)]), [], "gameplay_fit", settings, tmp_path / "e.ass")
    text = out.read_text(encoding="utf-8")
    assert "[Events]" in text and "Dialogue:" not in text


def test_ass_long_line_is_scaled_down(settings, tmp_path):
    words = [_w("unbelievably", 0.1, 0.5), _w("ridiculous", 0.6, 1.0)]
    settings.config.brand.caption_max_chars = 40
    evs = _events(captions.build_ass(words, TimeMap(segments=[(0.0, 2.0)]), [], "gameplay_fit", settings,
                                     tmp_path / "c.ass"))
    scales = [int(s) for s in re.findall(r"\\fscx(\d+)", evs[-1][2])]
    assert max(scales) < 100  # passt nicht in 76 % Breite -> verkleinert


def test_ass_lowercase_option(settings, tmp_path):
    settings.config.brand.caption_uppercase = False
    out = captions.build_ass([_w("Hello", 0.1, 0.4)], TimeMap(segments=[(0.0, 1.0)]), [], "gameplay_fit",
                             settings, tmp_path / "c.ass")
    assert _plain(_events(out)[0][2]) == "Hello"


def _slur(enc: str) -> str:
    """Testwörter rot13-kodiert wie in captions.py (nicht im Klartext im Repo)."""
    import codecs

    return codecs.decode(enc, "rot13")


def test_severe_slurs_are_masked_in_burned_in_captions(settings, tmp_path):
    words = _seq(f"bro {_slur('avttn')}, no way {_slur('snttbgf')} fucking shit")
    settings.config.brand.caption_max_words = 1  # ein Wort pro Event -> leicht prüfbar
    evs = _events(captions.build_ass(words, TimeMap(segments=[(0.0, 10.0)]), [], "gameplay_fit", settings,
                                     tmp_path / "c.ass"))
    shown = [_plain(t) for _, _, t in evs]
    assert shown == ["BRO", "N****", "NO", "WAY", "F******", "FUCKING", "SHIT"]  # normale Flüche bleiben
    raw = (tmp_path / "c.ass").read_text(encoding="utf-8").lower()
    assert _slur("avttn") not in raw and _slur("snttbg") not in raw


@pytest.mark.parametrize("word, masked", [
    ("avttre", "n*****"), ("Avttnm", "N*****"), ("ergneqrq", "r*******"), ("genaavrf", "t*******"),
    ("snt", "f**"), ("XVXR", "K***"),
])
def test_mask_word_variants(word, masked):
    assert captions.mask_word(_slur(word)) == masked


@pytest.mark.parametrize("word", [
    "spice", "spiced", "fagan", "kicker", "niger", "retardant", "fuck", "shit", "damn", "bitch", "nigeria",
])
def test_mask_word_keeps_normal_words(word):
    assert captions.mask_word(word) == word


def test_mask_slurs_in_text_keeps_punctuation_and_spacing():
    text = f"WHAT A {_slur('ergneq').upper()}!\nno {_slur('avttnf')}, ok"
    assert captions.mask_slurs(text) == "WHAT A R*****!\nno n*****, ok"
    assert captions.display_text(f"“{_slur('avttnf')}'”", True) == "N******"  # Apostroph zählt mit


def test_ass_renders_with_libass(settings, sample_transcript, tmp_path):
    """Die erzeugte Datei muss von ffmpeg/libass geparst und gerendert werden können."""
    ass = captions.build_ass(sample_transcript.words(), TimeMap(segments=[(0.0, 8.0)]), ["jump"], "gameplay_fit",
                             settings, tmp_path / "c.ass")
    fonts = tmp_path / "fonts"
    fonts.mkdir()
    (fonts / "ArchivoBlack-Regular.ttf").write_bytes(
        (settings.config.paths.fonts_dir / "ArchivoBlack-Regular.ttf").read_bytes())
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                           "color=c=gray:s=1080x1920:d=1.5:r=10", "-vf", f"ass={ass.name}:fontsdir=fonts",
                           "-f", "null", "-"], cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
