"""Edit-Engine v2: Transkript in der YouTube-Beschreibung, #GTA6 nur bei GTA-Inhalten, YouTube-Tags."""

from __future__ import annotations

from wdvi.publish import text
from wdvi.publish.text import (
    FAN_DISCLAIMER,
    TRANSCRIPT_LABEL,
    clean_transcript,
    compose_description,
    credit_line,
    is_gta_content,
    transcript_in,
    transcript_text,
    utf8_len,
)
from wdvi.publish.youtube import TAGS_MAX_CHARS, youtube_tags
from wdvi.schemas import TimeMap, Word

SPOKEN = "Is PlayStation better than Xbox? Bro it plays best on PS5!"


def _w(t: str, a: float, b: float) -> Word:
    return Word(text=t, start=a, end=b)


# --------------------------------------------------------------------------- Transkript


def test_transcript_text_only_kept_ranges_and_cleaned():
    words = [
        _w(" Is", 0.0, 0.3), _w(" PlayStation", 0.3, 0.8), _w(" better", 0.8, 1.0),
        _w(" um", 2.1, 2.3), _w(" uh", 2.3, 2.6),  # herausgeschnitten (2.0–3.0)
        _w(" than", 3.1, 3.3), _w(" Xbox", 3.3, 3.6), _w(" ?", 3.6, 3.7),
        _w(" never", 9.0, 9.5),  # nach trim_end
    ]
    tm = TimeMap(segments=[(0.0, 2.0), (3.0, 5.0)])
    assert transcript_text(words, tm) == "Is PlayStation better than Xbox?"
    # ohne TimeMap: alles, in Zeitreihenfolge
    assert transcript_text(list(reversed(words)), None).startswith("Is PlayStation better um uh than")


def test_transcript_text_word_midpoint_decides():
    tm = TimeMap(segments=[(1.0, 2.0)])
    words = [_w("half", 0.5, 1.4), _w("in", 1.2, 1.8), _w("out", 1.9, 2.4)]
    # "half": Mitte 0.95 -> draußen; "out": Mitte 2.15 -> draußen
    assert transcript_text(words, tm) == "in"


def test_clean_transcript_rules():
    assert clean_transcript("  no no no no no   way  ") == "no no way"
    assert clean_transcript("wait , what !") == "wait, what!"
    assert clean_transcript("#ad <b>bold</b>") == "ad bbold/b"
    assert clean_transcript(" ... ") == ""
    long = " ".join(f"word{i}" for i in range(300))
    out = clean_transcript(long)
    assert len(out) <= text.TRANSCRIPT_MAX_CHARS and out.endswith("…")


def test_youtube_description_has_transcript_block(candidate, settings):
    d = compose_description(
        "youtube", "Speed asks the real question 🎮", ["gtavi", "playstation", "xbox"], candidate, settings,
        transcript=SPOKEN, title="Is PS5 better than Xbox? 🤔", tags=["gta 6", "ishowspeed"],
    )
    lines = d.split("\n")
    assert lines[0] == "Speed asks the real question 🎮" and lines[1] == ""
    assert lines[2] == credit_line(candidate) and lines[3] == FAN_DISCLAIMER
    assert lines[4] == "" and lines[5] == TRANSCRIPT_LABEL and lines[6] == SPOKEN
    assert lines[7] == "" and lines[8] == "#GTA6 #gtavi #playstation #xbox"
    assert transcript_in(d) == SPOKEN
    # Hashtags im Anhang bleiben über hashtags_in auffindbar, das Transkript liefert keine
    assert text.hashtags_in(d[d.find(credit_line(candidate)):]) == ["GTA6", "gtavi", "playstation", "xbox"]


def test_social_captions_stay_short_without_transcript(candidate, settings):
    for platform in ("tiktok", "instagram"):
        d = compose_description(platform, "PS5 or Xbox?", ["gta6", "ps5"], candidate, settings, transcript=SPOKEN)
        assert TRANSCRIPT_LABEL not in d and SPOKEN not in d
        assert d.endswith("#GTA6 #ps5")


def test_transcript_respects_youtube_limit(candidate, settings):
    long_body = "💀 crazy moment " * 600
    d = compose_description("youtube", long_body, ["gtavi"], candidate, settings, transcript=SPOKEN * 20)
    assert utf8_len(d) <= 5000
    assert TRANSCRIPT_LABEL in d and credit_line(candidate) in d and d.endswith("#GTA6 #gtavi")
    block = transcript_in(d)
    assert block is not None and len(block) <= text.TRANSCRIPT_MAX_CHARS


def test_empty_transcript_means_no_block(candidate, settings):
    d = compose_description("youtube", "body", ["gta6"], candidate, settings, transcript="   ")
    assert TRANSCRIPT_LABEL not in d and transcript_in(d) is None


def test_youtube_transcript_is_angle_bracket_safe(candidate, settings):
    d = compose_description("youtube", "x", ["gta6"], candidate, settings, transcript="a <3 b > c")
    assert "<" not in d and ">" not in d


# --------------------------------------------------------------------------- #GTA6 nur bei GTA


def test_is_gta_content():
    assert is_gta_content("GTA 6 is here")
    assert is_gta_content(["fails", "gtavi"])
    assert is_gta_content(None, ["x", ["Grand Theft Auto VI"]])
    assert is_gta_content("welcome to vice city")
    assert not is_gta_content("Minecraft speedrun", ["minecraft", "speedrun"], None)


def test_no_gta6_for_non_gta_content(candidate, settings):
    d = compose_description(
        "youtube", "He beat the ender dragon", ["minecraft", "speedrun", "gaming"], candidate, settings,
        title="Fastest Minecraft run ever? 😱", tags=["minecraft"],
    )
    assert "#GTA6" not in d and d.endswith("#minecraft #speedrun #gaming")
    tt = compose_description("tiktok", "", ["minecraft"], candidate, settings, title="crazy run")
    assert "#GTA6" not in tt and tt.endswith("#minecraft")


def test_gta6_when_title_or_tags_mention_gta(candidate, settings):
    d = compose_description("tiktok", "wow", ["funny"], candidate, settings, title="GTA 6 police chase 🚓")
    assert d.endswith("#GTA6 #funny")
    d = compose_description("tiktok", "wow", ["funny"], candidate, settings, title="x", tags=["gta 6 trailer"])
    assert d.endswith("#GTA6 #funny #gta6trailer")  # zu wenige Hashtags -> Tag aufgefüllt


def test_gta_flag_overrides_detection(candidate, settings):
    d = compose_description("tiktok", "wow", ["gta6", "funny"], candidate, settings, gta=False)
    assert d.endswith("#gta6 #funny")  # nicht hart vorangestellt, nur übernommen
    d = compose_description("tiktok", "wow", ["funny"], candidate, settings, gta=True)
    assert d.endswith("#GTA6 #funny")


def test_legacy_call_without_plan_context_keeps_gta6(candidate, settings):
    # alte Aufrufer (keine Hashtags/Titel/Tags): Kanal-Standard bleibt #GTA6
    assert compose_description("tiktok", "x", [], candidate, settings).endswith("#GTA6")


def test_hashtags_capped_at_five_and_filled_from_tags(candidate, settings):
    many = [f"tag{i}" for i in range(10)]
    d = compose_description("instagram", "x", ["gta6", *many], candidate, settings)
    assert d.split("\n")[-1] == "#GTA6 #tag0 #tag1 #tag2 #tag3"
    # nur 1 Hashtag -> mit Plan-Tags auf 3–5 auffüllen (lange Keywords nicht)
    d = compose_description(
        "tiktok", "x", ["gta6"], candidate, settings,
        tags=["vice city", "ishowspeed", "a very very long keyword phrase here"],
    )
    assert d.split("\n")[-1] == "#GTA6 #vicecity #ishowspeed"


# --------------------------------------------------------------------------- YouTube-Tags


def test_youtube_tags_keep_keywords_and_limit():
    tags = youtube_tags(["gta 6", "GTA6", "IShowSpeed", "#ps5", "ps 5", "<b>", "123", "a,b"], gta=True)
    assert tags == ["GTA6", "IShowSpeed", "ps5", "b", "a b"]
    assert youtube_tags(["minecraft", "speedrun"], gta=False) == ["minecraft", "speedrun"]
    many = [f"keyword number {i}" for i in range(100)]
    out = youtube_tags(many, gta=True)
    cost = sum(len(t) + (2 if " " in t else 0) for t in out) + len(out) - 1
    assert cost <= TAGS_MAX_CHARS <= 500 and len(out) > 10
