"""Edit-Engine v2 auf der Claude-Seite: Prompt, Koordinatengitter, sanitize_plan (Shots, Annotationen,
Farben, Outro, Tags, Hook-Stil) und strict_schema für den größeren EditPlan. Nur Fake-LLM."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from PIL import Image

from wdvi.brain import editor, prompts
from wdvi.brain.claude import _parse_json_model, strict_schema
from wdvi.brain.editor import plan_clip, sanitize_plan
from wdvi.schemas import (
    Annotation,
    Box,
    EditPlan,
    Emphasis,
    FaceDet,
    FrameSample,
    LayoutHint,
    MediaAnalysis,
    MediaInfo,
    PlatformText,
    Segment,
    Shot,
    Transcript,
    Word,
    WordColor,
    Zoom,
)

NOW = datetime(2026, 10, 20, 0, 0, tzinfo=UTC)
FACECAM = LayoutHint(kind="facecam_split", facecam_box=Box(x=0.7, y=0.0, w=0.3, h=0.35),
                     gameplay_box=Box(x=0, y=0, w=1, h=1), confidence=0.8)


class FakeLLM:
    def __init__(self, result) -> None:
        self.result = result
        self.calls: list[dict] = []

    def complete_json(self, system, content, schema_model, *, max_tokens=16000):
        self.calls.append({"system": system, "content": content, "schema_model": schema_model})
        return self.result


def make_plan(**over) -> EditPlan:
    data = dict(
        keep=True, virality_score=85, reasoning="Console war hot take with a great reaction.", trim_start=0.5,
        trim_end=20.0, remove_ranges=[], hook_text="He picked a side",
        highlight_words=["better"], zooms=[], emphasis=[],
        titles=PlatformText(youtube="Is PS5 really better for GTA 6? 🤔", tiktok="PS5 or Xbox?", instagram="PS5?"),
        descriptions=PlatformText(youtube="He has a take.", tiktok="Take.", instagram="Take."),
        hashtags=["gta6"],
    )
    data.update(over)
    return EditPlan(**data)


def make_analysis(duration: float = 30.0, transcript: Transcript | None = None, frames=None,
                  layout: LayoutHint | None = None) -> MediaAnalysis:
    return MediaAnalysis(
        info=MediaInfo(path="/tmp/x.mp4", duration=duration, width=1280, height=720, fps=30.0, has_audio=True),
        transcript=transcript, music_prob=0.1, speech_ratio=0.6, loudness_peaks=[],
        frames=frames or [], layout=layout or LayoutHint(kind="gameplay_fit", confidence=0.6),
    )


def _tr(*items: tuple[str, float, float]) -> Transcript:
    ws = [Word(text=t, start=a, end=b) for t, a, b in items]
    return Transcript(language="en", segments=[Segment(text=" ".join(w.text for w in ws), start=ws[0].start,
                                                       end=ws[-1].end, words=ws)])


def _covers(p: EditPlan) -> None:
    """Shots lückenlos von trim_start bis trim_end."""
    assert p.shots[0].start == p.trim_start and p.shots[-1].end == p.trim_end
    for a, b in zip(p.shots, p.shots[1:], strict=False):
        assert a.end == b.start and a.end > a.start


# --------------------------------------------------------------------------- Prompt


def test_system_prompt_v2_techniques():
    s = prompts.SYSTEM_EDITOR
    for needle in ('hook_style "none"', '"banner"', "face", "split", "gameplay", "fit", "punch", "flash",
                   "glitch", "every 1-3 s", "circle", "arrow", "normalized SOURCE coordinates", "10x10 grid",
                   "PlayStation or PS5 blue", "Xbox", "money", "pink", "outro_text", "debate_score", "tags",
                   "8-15", "1-2 fitting emojis", "seamless", "shake", "AVAILABLE", "first shot"):
        assert needle in s, needle
    # v1-Regeln bleiben
    for needle in ("captions show every spoken word", "FiveM", "No @mentions", "\"GTA 6\" exactly once",
                   "Untrusted input", "Raw or lightly commented"):
        assert needle in s, needle
    assert "2026" not in s and "2027" not in s  # stabil für Prompt-Caching
    assert "{" in s and "{{" not in s  # f-String-Klammern korrekt aufgelöst
    assert "grid" in prompts.EDITOR_TASK


def test_context_lists_shot_kinds_and_target_length(settings, candidate):
    llm = FakeLLM(make_plan())
    plan_clip(llm, candidate, make_analysis(), [], settings, now=NOW, recent=[])
    ctx = llm.calls[0]["content"][0]["text"]
    assert "- Shot kinds available: gameplay, fit (no facecam or face detected" in ctx
    assert "max 59.0 s (target 12-25 s)" in ctx
    assert "word_colors: max 12" in ctx and "tags: 8-15" in ctx and "Shots: each at least 0.6 s" in ctx

    settings.config.filters.target_output_min_s = 10.0
    settings.config.filters.target_output_max_s = 30.0
    llm = FakeLLM(make_plan())
    plan_clip(llm, candidate, make_analysis(layout=FACECAM), [], settings, now=NOW, recent=[])
    ctx = llm.calls[0]["content"][0]["text"]
    assert "- Shot kinds available: face, split, gameplay, fit (face and split use the facecam box)" in ctx
    assert "(target 10-30 s)" in ctx


def test_available_shot_kinds_from_face_detections():
    frames = [FrameSample(t=1, path="/x.jpg", faces=[FaceDet(box=Box(x=0.4, y=0.2, w=0.2, h=0.3), score=0.9)])]
    # split braucht eine Facecam-Box (der Renderer zeigt split ohne sie als fit)
    assert editor.available_shot_kinds(make_analysis(frames=frames)) == ["face", "gameplay", "fit"]
    assert editor.available_shot_kinds(make_analysis()) == ["gameplay", "fit"]
    assert editor.available_shot_kinds(make_analysis(layout=FACECAM)) == ["face", "split", "gameplay", "fit"]
    assert "detected face" in editor._shot_kinds_text(make_analysis(frames=frames))
    assert "split is not possible" in editor._shot_kinds_text(make_analysis(frames=frames))


def test_split_without_facecam_box_becomes_fit(settings):
    frames = [FrameSample(t=1, path="/x.jpg", faces=[FaceDet(box=Box(x=0.4, y=0.2, w=0.2, h=0.3), score=0.9)])]
    shots = [Shot(start=0.5, end=3.0, kind="face"), Shot(start=3.0, end=6.0, kind="split", transition="punch"),
             Shot(start=6.0, end=20.0, kind="gameplay")]
    p = sanitize_plan(make_plan(shots=shots), make_analysis(frames=frames), settings)
    assert [(s.kind, s.transition) for s in p.shots] == [("face", "cut"), ("fit", "punch"), ("gameplay", "cut")]


# --------------------------------------------------------------------------- Koordinatengitter


def test_frames_sent_with_grid_copy(settings, candidate, tmp_path):
    jpg = tmp_path / "frames" / "f_0001.jpg"
    jpg.parent.mkdir()
    Image.new("RGB", (320, 180), (10, 20, 30)).save(jpg, "JPEG")
    before = jpg.read_bytes()
    llm = FakeLLM(make_plan())
    plan_clip(llm, candidate, make_analysis(frames=[FrameSample(t=2.0, path=str(jpg))]), [], settings,
              now=NOW, recent=[])
    blocks = llm.calls[0]["content"]
    img = next(b for b in blocks if b["type"] == "image")
    grid = jpg.parent / "_grid" / "f_0001.jpg"
    assert img["path"] == str(grid.resolve()) and img["t"] == 2.0
    assert jpg.read_bytes() == before  # Original unverändert
    with Image.open(grid) as g:
        assert g.size == (320, 180)
        px = g.convert("RGB")
        # Gitterlinie bei x = 0.5 heller als der Hintergrund, Feldmitte unverändert dunkel
        assert sum(px.getpixel((160, 120))) > sum(px.getpixel((176, 135))) + 30
        assert sum(px.getpixel((176, 135))) < 120
    assert any("10x10 grid" in b.get("text", "") for b in blocks if b["type"] == "text")
    # zweiter Aufruf nutzt die vorhandene Kopie
    mtime = grid.stat().st_mtime_ns
    assert editor.grid_frame(jpg) == grid and grid.stat().st_mtime_ns == mtime


def test_grid_failure_falls_back_to_original(tmp_path):
    bad = tmp_path / "broken.jpg"
    bad.write_bytes(b"not an image")
    assert editor.grid_frame(bad) == bad
    tiny = tmp_path / "tiny.jpg"
    Image.new("RGB", (8, 8)).save(tiny, "JPEG")
    assert editor.grid_frame(tiny) == tiny


# --------------------------------------------------------------------------- Shots


def test_shots_sorted_clipped_and_gap_free(settings):
    shots = [
        Shot(start=6.0, end=9.0, kind="gameplay", transition="punch"),
        Shot(start=0.0, end=2.4, kind="face", transition="flash"),  # vor trim_start, erster -> cut
        Shot(start=2.0, end=5.0, kind="split"),  # überlappt den vorigen -> beginnt bei 2.4
        Shot(start=12.0, end=40.0, kind="fit", zoom=3.0),  # Lücke 9-12 geht an den vorigen, Ende = trim_end
    ]
    p = sanitize_plan(make_plan(shots=shots, trim_start=1.0, trim_end=20.0), make_analysis(layout=FACECAM),
                      settings)
    _covers(p)
    assert [(s.start, s.end, s.kind, s.transition) for s in p.shots] == [
        (1.0, 2.4, "face", "cut"), (2.4, 6.0, "split", "cut"), (6.0, 12.0, "gameplay", "punch"),
        (12.0, 20.0, "fit", "cut")]
    assert p.shots[-1].zoom == 1.6


def test_short_shots_merged_and_min_length(settings):
    shots = [
        Shot(start=0.5, end=0.9, kind="gameplay"),  # erster zu kurz -> wird auf MIN_SHOT_S verlängert
        Shot(start=0.9, end=3.0, kind="fit"),
        Shot(start=3.0, end=3.5, kind="gameplay", transition="punch"),  # zu kurz -> im vorigen
        Shot(start=3.5, end=6.0, kind="gameplay", transition="flash"),
        Shot(start=6.0, end=8.0, kind="gameplay"),  # gleiche Einstellung per cut -> verschmolzen
        Shot(start=8.0, end=10.0, kind="fit"),
        Shot(start=10.0, end=11.0, kind="gameplay"),  # liegt fast ganz im Schnitt -> zu kurz
        Shot(start=11.0, end=20.0, kind="fit", transition="glitch"),
    ]
    p = sanitize_plan(make_plan(shots=shots, remove_ranges=[(10.0, 10.9)]), make_analysis(), settings)
    _covers(p)
    assert [(s.start, s.end, s.kind) for s in p.shots] == [
        (0.5, 1.1, "gameplay"), (1.1, 3.5, "fit"), (3.5, 8.0, "gameplay"), (8.0, 11.0, "fit"),
        (11.0, 20.0, "fit")]
    assert [s.transition for s in p.shots] == ["cut", "cut", "flash", "cut", "glitch"]
    for s in p.shots:
        assert editor._kept_overlap(s.start, s.end, p.remove_ranges) >= editor.MIN_SHOT_S


def test_short_first_shot_extended_or_merged(settings):
    """Erster Shot (stärkstes Bild, meist das Gesicht) behält seine Art: verlängert, solange der Nachfolger
    danach noch MIN_SHOT_S hat; sonst geht er wie bisher im Nachfolger auf. Reaktions-Punch-ins ab 0.6 s."""
    p = sanitize_plan(make_plan(trim_end=8.0, shots=[
        Shot(start=0.5, end=0.9, kind="fit"), Shot(start=0.9, end=4.0, kind="gameplay"),
        Shot(start=4.0, end=4.65, kind="fit", transition="punch"), Shot(start=4.65, end=8.0, kind="gameplay")]),
        make_analysis(), settings)
    assert [(s.start, s.end, s.kind) for s in p.shots] == [
        (0.5, 1.1, "fit"), (1.1, 4.0, "gameplay"), (4.0, 4.65, "fit"), (4.65, 8.0, "gameplay")]
    # Nachfolger zu kurz zum Abgeben -> erster geht im Nachfolger auf
    p = sanitize_plan(make_plan(trim_end=8.0, shots=[
        Shot(start=0.5, end=0.9, kind="fit"), Shot(start=0.9, end=1.6, kind="gameplay"),
        Shot(start=1.6, end=8.0, kind="fit", transition="flash")]), make_analysis(), settings)
    assert [(s.start, s.end, s.kind) for s in p.shots] == [(0.5, 1.6, "gameplay"), (1.6, 8.0, "fit")]


def test_gta_search_word_only_for_gta_content(settings):
    other = dict(titles=PlatformText(youtube="Can Pikaboo Kill the Unkillable Warlock? 💀", tiktok="warlock",
                                     instagram="warlock"),
                 descriptions=PlatformText(youtube="WoW duel.", tiktok="WoW.", instagram="WoW."),
                 hook_text="30 MIN OF LOSING", hashtags=["wow", "pikaboo"], tags=["world of warcraft", "pikaboo"])
    p = sanitize_plan(make_plan(**other), make_analysis(), settings)
    assert p.titles.youtube == "Can Pikaboo Kill the Unkillable Warlock? 💀"  # kein irreführendes "| GTA 6"
    assert p.hashtags == ["wow", "pikaboo"] and p.tags == ["world of warcraft", "pikaboo"]
    # GTA-Inhalt (hier nur über die Hashtags erkennbar) bekommt das Suchwort wie bisher
    p = sanitize_plan(make_plan(**{**other, "hashtags": ["gta6", "wow"]}), make_analysis(), settings)
    assert p.titles.youtube.endswith("| GTA 6") and p.tags[0] == "gta 6"


def test_unavailable_kinds_replaced_and_focus_clamped(settings):
    shots = [
        Shot(start=0.5, end=3.0, kind="face", focus=Box(x=0.9, y=-0.2, w=0.4, h=0.5), zoom=0.5),
        Shot(start=3.0, end=6.0, kind="split", transition="glitch"),
        Shot(start=6.0, end=9.0, kind="gameplay", focus=Box(x=1.5, y=0.2, w=0.2, h=0.2), transition="glitch"),
        Shot(start=9.0, end=12.0, kind="fit", transition="glitch"),
        Shot(start=12.0, end=20.0, kind="gameplay", focus=Box(x=0.5, y=0.5, w=0.0, h=0.01)),
    ]
    p = sanitize_plan(make_plan(shots=shots), make_analysis(), settings)  # kein Gesicht
    assert [s.kind for s in p.shots] == ["fit", "fit", "gameplay", "fit", "gameplay"]
    first = p.shots[0]
    assert first.zoom == 1.0
    assert first.focus == Box(x=0.9, y=0.0, w=0.1, h=0.3)  # Schnittmenge mit dem Bild
    assert p.shots[2].focus is None  # komplett außerhalb
    small = p.shots[4].focus
    assert small.w == pytest.approx(editor.MIN_BOX) and small.h == pytest.approx(editor.MIN_BOX)
    assert small.cx == pytest.approx(0.5) and small.cy == pytest.approx(0.505)
    # glitch höchstens zweimal
    assert [s.transition for s in p.shots].count("glitch") == editor.MAX_GLITCH


def test_face_kinds_kept_with_face_info(settings):
    shots = [Shot(start=0.5, end=3.0, kind="face", zoom=1.2), Shot(start=3.0, end=20.0, kind="split")]
    p = sanitize_plan(make_plan(shots=shots), make_analysis(layout=FACECAM), settings)
    assert [(s.kind, s.zoom) for s in p.shots] == [("face", 1.2), ("split", 1.0)]


def test_shots_follow_trim_after_max_length_cut(settings):
    shots = [Shot(start=float(i), end=float(i + 2), kind="gameplay" if i % 4 else "fit") for i in range(0, 90, 2)]
    p = sanitize_plan(make_plan(shots=shots, trim_start=0.0, trim_end=90.0), make_analysis(duration=90.0),
                      settings)
    assert p.trim_end == pytest.approx(59.0)
    _covers(p)
    assert len(p.shots) <= editor.MAX_SHOTS


def test_shot_cap(settings):
    shots = [Shot(start=i * 1.0, end=i * 1.0 + 1.0, kind="fit" if i % 2 else "gameplay") for i in range(60)]
    p = sanitize_plan(make_plan(shots=shots, trim_start=0.0, trim_end=58.0), make_analysis(duration=60.0),
                      settings)
    assert len(p.shots) == editor.MAX_SHOTS
    _covers(p)


def test_invalid_shots_dropped(settings):
    shots = [Shot(start=float("nan"), end=3.0, kind="fit"), Shot(start=25.0, end=29.0, kind="fit")]
    p = sanitize_plan(make_plan(shots=shots), make_analysis(), settings)
    assert p.shots == []


# --------------------------------------------------------------------------- Annotationen


def test_annotations_clipped_and_boxes_clamped(settings):
    items = [
        Annotation(start=4.0, end=6.0, kind="circle", box=Box(x=-0.1, y=0.8, w=0.3, h=0.4), color="red",
                   label="  Plays best on PS5 right here 💀 "),
        Annotation(start=0.0, end=0.7, box=Box(x=0.2, y=0.2, w=0.1, h=0.1)),  # nach Clip nur 0.2 s -> weg
        Annotation(start=19.0, end=30.0, kind="arrow", box=Box(x=0.5, y=0.5, w=0.001, h=0.001), color="yellow"),
        Annotation(start=8.0, end=9.0, box=Box(x=2.0, y=2.0, w=0.1, h=0.1)),  # außerhalb -> weg
        Annotation(start=10.0, end=11.0, box=Box(x=0.1, y=0.1, w=0.2, h=0.2)),  # im Schnitt -> weg
        Annotation(start=12.0, end=13.0, kind="box", box=Box(x=0.3, y=0.3, w=-0.1, h=0.2), label="@xqc"),
    ]
    p = sanitize_plan(make_plan(annotations=items, remove_ranges=[(9.8, 11.5)]), make_analysis(), settings)
    got = [(a.start, a.end, a.kind, a.color) for a in p.annotations]
    assert got == [(4.0, 6.0, "circle", "red"), (12.0, 13.0, "box", "red"), (19.0, 20.0, "arrow", "yellow")]
    first = p.annotations[0]
    assert first.box == Box(x=0.0, y=0.8, w=0.2, h=0.2)
    assert first.label == "Plays best on"  # max 3 Wörter, ohne Emoji
    assert p.annotations[1].box == Box(x=0.2, y=0.3, w=0.1, h=0.2) and p.annotations[1].label == "xqc"
    tiny = p.annotations[2].box
    assert tiny.w == pytest.approx(editor.MIN_BOX) and tiny.cx == pytest.approx(0.5005, abs=1e-3)
    for a in p.annotations:
        assert 0 <= a.box.x <= a.box.x + a.box.w <= 1 and 0 <= a.box.y <= a.box.y + a.box.h <= 1


def test_annotation_cap_and_slur_label(settings):
    items = [Annotation(start=1.0 + i * 2, end=2.0 + i * 2, box=Box(x=0.1, y=0.1, w=0.2, h=0.2),
                        label="retard" if i == 0 else None) for i in range(6)]
    p = sanitize_plan(make_plan(annotations=items), make_analysis(), settings)
    assert len(p.annotations) == editor.MAX_ANNOTATIONS and p.annotations[0].label is None and p.keep


# --------------------------------------------------------------------------- Farben, Outro, Tags, Score


def test_word_colors_deduped_and_capped(settings):
    items = [WordColor(word="PlayStation", color="blue"), WordColor(word="playstation!", color="green"),
             WordColor(word="Xbox", color="green"), WordColor(word="GTA 6", color="pink"),
             WordColor(word="gta6", color="purple"), WordColor(word="  ", color="red"),
             WordColor(word="faggot", color="red"), WordColor(word="a very long phrase of many words", color="red")]
    items += [WordColor(word=f"w{i}", color="yellow") for i in range(20)]
    p = sanitize_plan(make_plan(word_colors=items), make_analysis(), settings)
    assert [(w.word, w.color) for w in p.word_colors[:3]] == [
        ("PlayStation", "blue"), ("Xbox", "green"), ("GTA 6", "pink")]
    assert len(p.word_colors) == editor.MAX_WORD_COLORS
    keys = [editor._color_key(w.word) for w in p.word_colors]
    assert len(set(keys)) == len(keys)


def test_outro_text_limits(settings):
    p = sanitize_plan(make_plan(outro_text="IS PLAYSTATION BETTER THAN XBOX? 🤔 #gta6"), make_analysis(), settings)
    assert p.outro_text == "IS PLAYSTATION BETTER THAN XBOX?"
    long_q = "Would you really pay one hundred dollars for the GTA 6 special edition?"
    p = sanitize_plan(make_plan(outro_text=long_q), make_analysis(), settings)
    assert len(p.outro_text.split()) <= editor.OUTRO_MAX_WORDS and len(p.outro_text) <= editor.OUTRO_MAX_CHARS
    assert p.outro_text.endswith("?") and p.outro_text.startswith("Would you really pay")
    p = sanitize_plan(make_plan(outro_text="Who wins, @xqc or gta6 cops?"), make_analysis(), settings)
    assert p.outro_text == "Who wins, xqc or GTA 6 cops?"
    for empty in ("", "   ", "🔥💀", None):
        assert sanitize_plan(make_plan(outro_text=empty), make_analysis(), settings).outro_text is None
    p = sanitize_plan(make_plan(outro_text="Is he a retard?"), make_analysis(), settings)
    assert not p.keep and "Schlussfrage" in p.reject_reason


def test_debate_score_and_tags(settings):
    tags = ["#GTA 6", "gta 6", "PS5", "ps5", "xbox, series x", "<script>", "", "x" * 40, "IShowSpeed"]
    tags += [f"tag {i}" for i in range(20)]
    p = sanitize_plan(make_plan(debate_score=250, tags=tags), make_analysis(), settings)
    assert p.debate_score == 100
    assert p.tags[:4] == ["GTA 6", "PS5", "xbox series x", "script"]
    assert len(p.tags) == editor.MAX_TAGS
    assert all("#" not in t and "," not in t and "<" not in t for t in p.tags)
    assert len({t.lower() for t in p.tags}) == len(p.tags)
    p = sanitize_plan(make_plan(debate_score=-3, tags=["ps5", "xbox"]), make_analysis(), settings)
    assert p.debate_score == 0 and p.tags == ["gta 6", "ps5", "xbox"]  # Suchwort ergänzt
    p = sanitize_plan(make_plan(tags=["a" * 29 + str(i) for i in range(15)]), make_analysis(), settings)
    assert sum(len(t) for t in p.tags) <= editor.TAGS_TOTAL_MAX


# --------------------------------------------------------------------------- Hook-Stil


def test_hook_style_none_allows_empty_hook(settings):
    tr = _tr(("Is", 0.6, 0.7), ("PlayStation", 0.75, 1.2), ("better?", 1.25, 1.6))
    p = sanitize_plan(make_plan(hook_style="none", hook_text=""), make_analysis(transcript=tr), settings)
    assert p.hook_style == "none" and p.hook_text == ""


def test_hook_style_none_without_speech_falls_back_to_banner(settings):
    tr = _tr(("later", 8.0, 8.4))
    p = sanitize_plan(make_plan(hook_style="none", hook_text=""), make_analysis(transcript=tr), settings)
    assert p.hook_style == "banner" and p.hook_text  # Fallback aus dem Titel
    p = sanitize_plan(make_plan(hook_style="none", hook_text="Wait for it"), make_analysis(), settings)
    assert p.hook_style == "banner" and p.hook_text == "Wait for it"
    # gesprochenes Wort am Anfang liegt im Schnitt -> zählt nicht
    tr = _tr(("Is", 0.6, 0.9), ("later", 9.0, 9.4))
    p = sanitize_plan(make_plan(hook_style="none", trim_start=0.5, remove_ranges=[(0.55, 3.0)]),
                      make_analysis(transcript=tr), settings)
    assert p.hook_style == "banner"


def test_banner_empty_hook_uses_title(settings):
    p = sanitize_plan(make_plan(hook_text=""), make_analysis(), settings)
    assert p.hook_style == "banner" and p.hook_text.startswith("Is PS5 really better")


# --------------------------------------------------------------------------- Emphasis v2


def test_shake_flash_kept_and_capped(settings):
    items = [Emphasis(t=1.0 + i, shake=True) for i in range(5)] + [Emphasis(t=10.0 + i, flash=True)
                                                                   for i in range(4)]
    p = sanitize_plan(make_plan(emphasis=items), make_analysis(), settings)
    assert sum(e.shake for e in p.emphasis) == editor.MAX_SHAKES
    assert sum(e.flash for e in p.emphasis) == editor.MAX_FLASHES
    assert all(e.emoji is None and e.sfx is None for e in p.emphasis)
    assert len(p.emphasis) == editor.MAX_SHAKES + editor.MAX_FLASHES


def test_own_whoosh_after_punch_transition_dropped(settings):
    shots = [Shot(start=0.5, end=5.0, kind="fit"), Shot(start=5.0, end=20.0, kind="gameplay", transition="punch")]
    items = [Emphasis(t=5.1, sfx="whoosh"), Emphasis(t=5.45, sfx="whoosh", emoji="💀"), Emphasis(t=12.0, sfx="boom")]
    p = sanitize_plan(make_plan(shots=shots, emphasis=items), make_analysis(), settings)
    assert [(e.t, e.emoji, e.sfx) for e in p.emphasis] == [
        (5.1, None, "whoosh"), (5.45, "💀", None), (12.0, None, "boom")]


# --------------------------------------------------------------------------- v1-Kompatibilität


def test_empty_v2_fields_stay_v1(settings, sample_transcript):
    plan = make_plan(zooms=[Zoom(start=2, end=3, scale=1.2)], emphasis=[Emphasis(t=1.02, emoji="😂")])
    p = sanitize_plan(plan, make_analysis(transcript=sample_transcript), settings)
    assert p.shots == [] and p.annotations == [] and p.word_colors == [] and p.tags == []
    assert p.hook_style == "banner" and p.outro_text is None and p.debate_score == 0
    assert p.hook_text == "He picked a side"
    assert [(e.t, e.emoji, e.sfx, e.shake, e.flash) for e in p.emphasis] == [(1.02, "😂", None, False, False)]


# --------------------------------------------------------------------------- strict_schema / Parsing


def _walk(node, path="$"):
    yield path, node
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk(v, f"{path}[{i}]")


def test_strict_schema_v2_edit_plan():
    s = strict_schema(EditPlan)
    dumped = json.dumps(s)
    assert "$ref" not in dumped and "$defs" not in dumped
    unions = 0
    for path, node in _walk(s):
        if not isinstance(node, dict):
            continue
        for bad in ("default", "title", "minimum", "maximum", "minLength", "maxLength", "prefixItems", "oneOf"):
            assert bad not in node or path.endswith(".properties"), (path, bad)
        if node.get("type") == "object":
            assert node["additionalProperties"] is False, path
            assert node["required"] == list(node["properties"]), path
        if "anyOf" in node:
            unions += 1
    assert unions <= 16  # structured outputs: Unions sparsam halten
    props = s["properties"]
    for field in ("shots", "annotations", "word_colors", "hook_style", "outro_text", "debate_score", "tags"):
        assert field in s["required"]
    shot = props["shots"]["items"]
    assert shot["properties"]["kind"]["enum"] == ["face", "split", "gameplay", "fit"]
    assert shot["properties"]["transition"]["enum"] == ["cut", "flash", "punch", "glitch"]
    box_t = [t for t in shot["properties"]["focus"]["anyOf"] if t.get("type") == "object"][0]
    assert box_t["required"] == ["x", "y", "w", "h"]
    ann = props["annotations"]["items"]
    assert ann["properties"]["box"]["required"] == ["x", "y", "w", "h"]
    assert ann["properties"]["color"]["enum"] == ["white", "pink", "orange", "yellow", "green", "blue", "purple",
                                                  "red"]
    assert props["word_colors"]["items"]["required"] == ["word", "color"]
    assert props["hook_style"]["enum"] == ["banner", "none"]
    assert {"type": "null"} in props["outro_text"]["anyOf"]
    assert props["debate_score"]["type"] == "integer"
    assert props["tags"] == {"type": "array", "items": {"type": "string"}}


def test_full_v2_plan_roundtrip_through_parser_and_plan_clip(settings, candidate):
    raw = make_plan(
        hook_style="none", hook_text="",
        shots=[Shot(start=0.5, end=2.0, kind="face", zoom=1.2),
               Shot(start=2.0, end=4.0, kind="split", transition="punch"),
               Shot(start=4.0, end=6.5, kind="gameplay", focus=Box(x=0.3, y=0.4, w=0.3, h=0.2),
                    transition="flash"),
               Shot(start=6.5, end=20.0, kind="face")],
        annotations=[Annotation(start=4.2, end=6.0, kind="circle", box=Box(x=0.35, y=0.45, w=0.2, h=0.1),
                                color="red", label="PS5")],
        word_colors=[WordColor(word="PlayStation", color="blue"), WordColor(word="Xbox", color="green")],
        outro_text="IS PLAYSTATION BETTER THAN XBOX?", debate_score=88, tags=["gta 6", "ps5", "xbox"],
        emphasis=[Emphasis(t=4.0, flash=True), Emphasis(t=5.0, shake=True, emoji="🤯")],
    )
    parsed = _parse_json_model(json.dumps(raw.model_dump()), EditPlan)
    assert parsed == raw
    tr = _tr(("Is", 0.6, 0.7), ("PlayStation", 0.75, 1.2), ("better", 1.25, 1.5), ("than", 1.55, 1.7),
             ("Xbox?", 1.75, 2.1))
    llm = FakeLLM(parsed)
    plan = plan_clip(llm, candidate, make_analysis(transcript=tr, layout=FACECAM), [], settings, now=NOW,
                     recent=[])
    assert plan.keep and plan.hook_style == "none" and plan.hook_text == ""
    _covers(plan)
    assert [s.kind for s in plan.shots] == ["face", "split", "gameplay", "face"]
    assert plan.annotations[0].label == "PS5" and plan.debate_score == 88
    assert plan.outro_text == "IS PLAYSTATION BETTER THAN XBOX?"
    assert [(e.t, e.flash, e.shake, e.emoji) for e in plan.emphasis] == [(4.0, True, False, None),
                                                                        (5.0, False, True, "🤯")]


def test_prompted_hashtag_count_matches_publish_limit(settings, candidate):
    """Claude soll nur so viele Hashtags liefern, wie die Beschreibung auch zeigt (publish.text.MAX_HASHTAGS)."""
    from wdvi.publish import text as ptext

    assert editor.PROMPT_HASHTAGS == ptext.MAX_HASHTAGS
    assert f"hashtags: 3-{ptext.MAX_HASHTAGS} relevant tags" in prompts.SYSTEM_EDITOR
    llm = FakeLLM(make_plan())
    plan_clip(llm, candidate, make_analysis(), [], settings, now=NOW, recent=[])
    assert f"- Hashtags: 3-{ptext.MAX_HASHTAGS}, without '#'" in llm.calls[0]["content"][0]["text"]
