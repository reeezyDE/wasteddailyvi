"""Titel/Beschreibungen je Plattform: Credit, Fan-Hinweis, Hashtags, Limits."""

from __future__ import annotations

from wdvi.publish import text
from wdvi.publish.text import (
    FAN_DISCLAIMER,
    build_caption,
    compose_description,
    compose_title,
    credit_line,
    normalize_hashtags,
    utf8_len,
    utf16_len,
)


def test_credit_line_with_url(candidate):
    assert credit_line(candidate) == "🎥 Clip: xQc on Twitch – twitch.tv/xqc"


def test_credit_line_without_url_and_www(candidate):
    c = candidate.model_copy(update={"creator_url": None})
    assert credit_line(c) == "🎥 Clip: xQc on Twitch"
    c = candidate.model_copy(
        update={
            "creator_url": "http://www.youtube.com/@Someone/",
            "platform_label": "YouTube",
            "creator_name": "Someone",
        }
    )
    assert credit_line(c) == "🎥 Clip: Someone on YouTube – youtube.com/@Someone"


def test_normalize_hashtags_dedupe_and_gta6_first():
    tags = normalize_hashtags(["#gta6", "GTAVI", "gtavi", "#Funny Moments", "!!", "123", "fyp"])
    assert tags[0] == "GTA6"
    assert [t.lower() for t in tags] == ["gta6", "gtavi", "funnymoments", "fyp"]
    many = normalize_hashtags([f"tag{i}" for i in range(20)])
    assert len(many) == text.MAX_HASHTAGS == 5 and many[0] == "GTA6"


def test_compose_description_structure(candidate, settings):
    d = compose_description(
        "youtube", "He really did that 💀", ["gtavi", "GTA6", "gta6", "Shorts"], candidate, settings
    )
    lines = d.split("\n")
    assert lines[0] == "He really did that 💀"
    assert lines[1] == ""
    assert lines[2] == credit_line(candidate)
    assert lines[3] == FAN_DISCLAIMER
    assert "Take-Two" in FAN_DISCLAIMER
    assert lines[-1] == "#GTA6 #gtavi #Shorts"
    assert d.lower().count("#gta6") == 1


def test_compose_description_skips_tags_already_in_body(candidate, settings):
    body = "insane jump #GTA6 #fyp"
    d = compose_description("tiktok", body, ["fyp", "gta6", "viral"], candidate, settings)
    assert d.lower().count("#gta6") == 1
    assert d.lower().count("#fyp") == 1
    assert d.rstrip().endswith("#viral")


def test_compose_description_always_has_gta6_even_with_many_body_tags(candidate, settings):
    # GTA-Inhalt (Hashtag erwähnt GTA): #GTA6 kommt trotz voller Hashtag-Liste im Text dazu
    body = " ".join(f"#t{i}" for i in range(12))
    d = compose_description("instagram", body, ["x", "gtavi"], candidate, settings)
    assert "#GTA6" in d
    assert "#x" not in d.split()


def test_compose_description_limits(candidate, settings):
    long_body = "💀 crazy moment " * 600
    for platform, limit, measure in [
        ("youtube", 5000, utf8_len),
        ("tiktok", 2200, utf16_len),
        ("instagram", 2200, utf16_len),
    ]:
        d = compose_description(platform, long_body, ["gtavi"], candidate, settings)
        assert measure(d) <= limit, platform
        # Credit, Fan-Hinweis und Hashtags bleiben trotz Kürzung erhalten
        assert credit_line(candidate) in d
        assert FAN_DISCLAIMER in d
        assert d.endswith("#GTA6 #gtavi")
        assert "…" in d


def test_compose_description_youtube_strips_angle_brackets(candidate, settings):
    d = compose_description("youtube", "a <b> c", [], candidate, settings)
    assert "<" not in d and ">" not in d


def test_compose_description_empty_body(candidate, settings):
    d = compose_description("tiktok", "  ", [], candidate, settings)
    assert d.startswith("🎥 Clip:")
    assert d.endswith("#GTA6")


def test_compose_title_youtube_appends_shorts():
    assert compose_title("youtube", "No way he did that 💀") == "No way he did that 💀 #Shorts"
    assert compose_title("youtube", "Crazy jump #shorts") == "Crazy jump #shorts"
    t92 = "x" * 92
    assert compose_title("youtube", t92) == t92 + " #Shorts"
    t93 = "x" * 93
    assert compose_title("youtube", t93) == t93  # passt nicht mehr
    assert compose_title("youtube", "a <b>") == "a ‹b› #Shorts"
    assert compose_title("youtube", "   ") == "GTA 6 Clip #Shorts"
    assert compose_title("youtube", "multi\nline   title") == "multi line title #Shorts"


def test_compose_title_youtube_truncates_and_is_idempotent():
    long = "word " * 40
    t = compose_title("youtube", long)
    assert len(t) <= 100 and t.endswith("…")
    assert compose_title("youtube", t) == t
    nospace = "y" * 150
    t2 = compose_title("youtube", nospace)
    assert len(t2) == 100
    assert compose_title("youtube", t2) == t2
    for n in range(80, 120):
        once = compose_title("youtube", "ab " * (n // 3))
        assert len(once) <= 100
        assert compose_title("youtube", once) == once


def test_compose_title_social():
    assert compose_title("tiktok", "  Hello\nworld ") == "Hello world"
    assert len(compose_title("instagram", "z " * 200)) <= text.SOCIAL_TITLE_MAX


def test_build_caption_prepends_title_and_dedupes():
    assert build_caption("tiktok", "Big jump", "desc #GTA6") == "Big jump\n\ndesc #GTA6"
    assert build_caption("tiktok", "Big jump", "Big jump 💀\n\nmore") == "Big jump 💀\n\nmore"
    assert build_caption("instagram", "", "only desc") == "only desc"
    huge = "d" * 2190
    cap = build_caption("tiktok", "Some title here", huge)
    assert cap == huge  # Titel fällt weg, damit das Limit hält
    assert utf16_len(build_caption("tiktok", "t", "💀" * 3000)) <= 2200
