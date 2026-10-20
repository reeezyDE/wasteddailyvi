"""plan_clip (Prompt-Aufbau mit Fake-LLM) und sanitize_plan (harte Regeln)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from PIL import Image

from wdvi.brain import editor, prompts
from wdvi.brain.editor import plan_clip, sanitize_plan
from wdvi.schemas import (
    Box,
    CandidateIn,
    EditPlan,
    Emphasis,
    FaceDet,
    FrameSample,
    LayoutHint,
    MediaAnalysis,
    MediaInfo,
    PerformanceExample,
    PlatformText,
    Segment,
    SourceKind,
    Transcript,
    Word,
    Zoom,
)

NOW = datetime(2026, 10, 20, 0, 0, tzinfo=UTC)


class FakeLLM:
    def __init__(self, result) -> None:
        self.result = result
        self.calls: list[dict] = []

    def complete_json(self, system, content, schema_model, *, max_tokens=16000):
        self.calls.append({"system": system, "content": content, "schema_model": schema_model,
                           "max_tokens": max_tokens})
        return self.result


def make_plan(**over) -> EditPlan:
    data = dict(
        keep=True, virality_score=85, reasoning="Huge stunt with a great reaction.", trim_start=0.5,
        trim_end=20.0, remove_ranges=[], hook_text="He did NOT see this coming",
        highlight_words=["insane", "jump"], zooms=[], emphasis=[],
        titles=PlatformText(youtube="xQc lands an insane GTA 6 jump", tiktok="Insane jump", instagram="Insane jump"),
        descriptions=PlatformText(youtube="Wild jump.", tiktok="Wild.", instagram="Wild."),
        hashtags=["gta6", "gaming"],
    )
    data.update(over)
    return EditPlan(**data)


def make_analysis(duration: float = 30.0, transcript: Transcript | None = None, frames=None,
                  layout: LayoutHint | None = None, width: int = 1280, height: int = 720) -> MediaAnalysis:
    return MediaAnalysis(
        info=MediaInfo(path="/tmp/x.mp4", duration=duration, width=width, height=height, fps=30.0, has_audio=True),
        transcript=transcript, music_prob=0.12, speech_ratio=0.45, loudness_peaks=[5.0, 5.4],
        frames=frames or [], layout=layout or LayoutHint(kind="gameplay_fit", confidence=0.6),
    )


def _kept(p: EditPlan) -> float:
    return (p.trim_end - p.trim_start) - sum(b - a for a, b in p.remove_ranges)


# --------------------------------------------------------------------------- plan_clip


def test_plan_clip_builds_prompt_and_sanitizes(settings, candidate, sample_transcript, tmp_path):
    jpg = tmp_path / "f1.jpg"
    Image.new("RGB", (64, 36)).save(jpg, "JPEG")
    frames = [
        FrameSample(t=1.0, path=str(jpg), faces=[FaceDet(box=Box(x=0.7, y=0.05, w=0.2, h=0.3), score=0.9)]),
        FrameSample(t=4.0, path=str(tmp_path / "missing.jpg")),
    ]
    layout = LayoutHint(kind="facecam_split", facecam_box=Box(x=0.7, y=0.0, w=0.3, h=0.35),
                        gameplay_box=Box(x=0, y=0, w=1, h=1), confidence=0.8)
    analysis = make_analysis(duration=8.0, transcript=sample_transcript, frames=frames, layout=layout)
    examples = [
        PerformanceExample(platform="youtube", title="Weak one", hook_text="meh", views=900, hours_after=72),
        PerformanceExample(platform="tiktok", title="Banger", hook_text="No way", views=1_250_000, hours_after=24),
    ]
    raw = make_plan(trim_start=0.4, trim_end=7.9, titles=PlatformText(youtube="Insane jump #shorts #Shorts",
                                                                     tiktok="t", instagram="i"),
                    hashtags=["#GTA6", "gta6", "Gaming"])
    llm = FakeLLM(raw)
    recent = [("He did NOT see this coming", "xQc vs the cops in GTA 6"), ("", "Only a title")]
    candidate = candidate.model_copy(update={"extra": {**candidate.extra, "game_id": "1234"}})
    plan = plan_clip(llm, candidate, analysis, examples, settings, now=NOW, recent=recent)

    call = llm.calls[0]
    assert call["system"] is prompts.SYSTEM_EDITOR
    assert call["schema_model"] is EditPlan and call["max_tokens"] == editor.MAX_OUTPUT_TOKENS == 64000
    blocks = call["content"]
    ctx = blocks[0]["text"]
    assert "Creator: xQc (@xqc)" in ctx
    assert "- Platform category: Grand Theft Auto VI (Twitch game category)" in ctx
    assert 'Original title: "no way bro"' in ctx
    assert "Views: 12,000 | Age: 6.0 h | Velocity: 2,000 views/h" in ctx
    assert "Current date (UTC): 2026-10-20" in ctx and "30 days, on 2026-11-19" in ctx
    assert "max 59.0 s" in ctx and "min 7.0 s" in ctx
    assert "virality_score >=" not in ctx  # Schwelle nicht verraten (sonst Score-Inflation), Code prüft sie
    assert "Zooms: max 3" in ctx and "Emojis: 1-4" in ctx and "sound effects: 0-4" in ctx
    assert "facecam_split (confidence 0.80)" in ctx
    assert "Music probability: 0.12" in ctx
    assert "Loudness peaks (source s; likely action, impact or reaction moments" in ctx
    assert "before them): 5.00, 5.40" in ctx
    assert "[0.60-0.92] no" in ctx
    assert ctx.index("1,250,000 views") < ctx.index("900 views")  # beste zuerst
    assert "- TOP: 1,250,000 views" in ctx and "- FLOP: 900 views" in ctx
    assert "RECENT POSTS" in ctx and '- hook "He did NOT see this coming" | title "xQc vs the cops in GTA 6"' in ctx
    assert "broadcaster_id" not in ctx and "vod_offset" not in ctx
    # Frames: Beschriftung + Bild; fehlende Datei wird weggelassen
    labels = [b["text"] for b in blocks if b["type"] == "text"]
    assert "Frame t=1.00s (faces: 1)" in labels
    assert not any("t=4.00" in t for t in labels)
    imgs = [b for b in blocks if b["type"] == "image"]
    # v2: Claude bekommt eine Kopie mit Koordinatengitter (Original bleibt unverändert)
    assert imgs == [{"type": "image", "path": str((tmp_path / "_grid" / "f1.jpg").resolve()), "t": 1.0}]
    i = blocks.index(imgs[0])
    assert blocks[i - 1]["text"] == "Frame t=1.00s (faces: 1)"
    assert blocks[-1]["text"] == prompts.EDITOR_TASK
    # sanitized
    assert plan.titles.youtube == "Insane jump | GTA 6"
    assert plan.hashtags == ["GTA6", "Gaming"]
    assert plan.trim_start == 0.4 and plan.trim_end == 7.9


def test_plan_clip_without_transcript_and_frames(settings, candidate):
    llm = FakeLLM(make_plan(trim_start=0.0, trim_end=10.0))
    plan = plan_clip(llm, candidate, make_analysis(duration=10.0), [], settings, now=NOW, recent=[])
    ctx = llm.calls[0]["content"][0]["text"]
    assert "TRANSCRIPT: none" in ctx
    assert "OUR PAST POSTS" not in ctx and "RECENT POSTS" not in ctx
    assert not [b for b in llm.calls[0]["content"] if b["type"] == "image"]
    assert plan.keep


def test_plan_clip_long_transcript_uses_segments(settings, candidate):
    segs = []
    for i in range(100):  # 1000 Wörter
        ws = [Word(text=f"w{i}_{j}", start=i * 3 + j * 0.25, end=i * 3 + j * 0.25 + 0.2) for j in range(10)]
        segs.append(Segment(text=" ".join(w.text for w in ws), start=ws[0].start, end=ws[-1].end, words=ws))
    tr = Transcript(language="en", segments=segs)
    llm = FakeLLM(make_plan())
    plan_clip(llm, candidate, make_analysis(duration=300.0, transcript=tr), [], settings, now=NOW)
    ctx = llm.calls[0]["content"][0]["text"]
    assert "segment level" in ctx and "[0.00-2.45] w0_0 w0_1" in ctx


def test_frame_selection_caps_to_max_frames(settings, candidate, tmp_path):
    frames = []
    for i in range(20):
        p = tmp_path / f"f{i}.jpg"
        Image.new("RGB", (8, 8)).save(p, "JPEG")
        frames.append(FrameSample(t=float(i), path=str(p)))
    settings.config.llm.max_frames = 5
    llm = FakeLLM(make_plan())
    plan_clip(llm, candidate, make_analysis(frames=frames), [], settings, now=NOW)
    ts = [b["t"] for b in llm.calls[0]["content"] if b["type"] == "image"]
    assert len(ts) == 5 and ts[0] == 0.0 and ts[-1] == 19.0


# --------------------------------------------------------------------------- sanitize_plan: Zeiten


def test_trim_clamped_to_duration(settings):
    p = sanitize_plan(make_plan(trim_start=-3, trim_end=99), make_analysis(duration=30.0), settings)
    assert (p.trim_start, p.trim_end) == (0.0, 30.0)
    p = sanitize_plan(make_plan(trim_start=25, trim_end=5), make_analysis(duration=30.0), settings)
    assert (p.trim_start, p.trim_end) == (5.0, 25.0)
    p = sanitize_plan(make_plan(trim_start=float("nan"), trim_end=10), make_analysis(duration=30.0), settings)
    assert p.trim_start == 0.0 and p.trim_end == 10.0


def test_over_long_plan_is_cut_to_max_output(settings):
    p = sanitize_plan(make_plan(trim_start=0, trim_end=120), make_analysis(duration=120.0), settings)
    assert p.trim_end == pytest.approx(59.0)
    assert _kept(p) <= settings.config.filters.max_output_duration_s + 1e-6
    assert p.keep


def test_over_long_with_cuts_counts_kept_time(settings):
    p = sanitize_plan(make_plan(trim_start=0, trim_end=80, remove_ranges=[(10, 20), (70, 75)]),
                      make_analysis(duration=90.0), settings)
    assert p.remove_ranges == [(10.0, 20.0)]
    assert p.trim_end == pytest.approx(69.0)
    assert _kept(p) == pytest.approx(59.0)


def test_over_long_cut_does_not_end_mid_word(settings):
    words = [Word(text=f"w{i}", start=i * 0.5, end=i * 0.5 + 0.45) for i in range(200)]
    tr = Transcript(language="en", segments=[Segment(text="x", start=0, end=100, words=words)])
    p = sanitize_plan(make_plan(trim_start=0.2, trim_end=100), make_analysis(duration=100, transcript=tr), settings)
    assert p.trim_end <= 59.2 + 1e-6
    assert not any(w.start < p.trim_end < w.end for w in words)


def test_remove_ranges_normalized(settings):
    plan = make_plan(trim_start=1.0, trim_end=25.0, remove_ranges=[
        (5.0, 6.0), (5.5, 7.0),  # überlappend -> (5, 7)
        (7.1, 8.0),  # nur 0.1 s Rest dazwischen -> mit verschmolzen
        (12.0, 12.1),  # zu kurz
        (16.0, 14.0),  # vertauscht
        (24.5, 40.0),  # über das Trim-Ende hinaus -> wird zum Trim
        (0.0, 1.5),  # am Anfang -> Trim-Start
    ])
    p = sanitize_plan(plan, make_analysis(duration=30.0), settings)
    assert p.trim_start == 1.5
    assert p.trim_end == 24.5
    assert p.remove_ranges == [(5.0, 8.0), (14.0, 16.0)]
    for (_, b1), (a2, _) in zip(p.remove_ranges, p.remove_ranges[1:], strict=False):
        assert a2 > b1
    assert all(b - a >= 0.25 for a, b in p.remove_ranges)


def test_too_short_is_rejected(settings):
    p = sanitize_plan(make_plan(trim_start=1, trim_end=5), make_analysis(duration=30.0), settings)
    assert not p.keep and "Zu kurz" in p.reject_reason
    p = sanitize_plan(make_plan(trim_start=0, trim_end=10, remove_ranges=[(1, 9)]),
                      make_analysis(duration=30.0), settings)
    assert not p.keep


def test_unknown_duration_rejected(settings):
    p = sanitize_plan(make_plan(trim_start=0, trim_end=20), make_analysis(duration=0.0), settings)
    assert not p.keep


# --------------------------------------------------------------------------- sanitize_plan: Policies


def test_leak_rejected_unless_policy_allows(settings):
    p = sanitize_plan(make_plan(is_leak_or_unreleased=True), make_analysis(), settings)
    assert not p.keep and "Geleakt" in p.reject_reason
    settings.config.filters.leak_policy = "allow"
    p = sanitize_plan(make_plan(is_leak_or_unreleased=True), make_analysis(), settings)
    assert p.keep and p.reject_reason is None


def test_unsafe_rejected(settings):
    p = sanitize_plan(make_plan(is_unsafe=True), make_analysis(), settings)
    assert not p.keep and "Unsicher" in p.reject_reason


def test_low_score_rejected_and_score_clamped(settings):
    p = sanitize_plan(make_plan(virality_score=55), make_analysis(), settings)
    assert not p.keep and "55" in p.reject_reason
    p = sanitize_plan(make_plan(virality_score=150), make_analysis(), settings)
    assert p.virality_score == 100 and p.keep


def test_claude_reject_reason_is_kept(settings):
    p = sanitize_plan(make_plan(keep=False, reject_reason="Needs too much context", virality_score=40),
                      make_analysis(), settings)
    assert not p.keep and p.reject_reason == "Needs too much context"
    p = sanitize_plan(make_plan(keep=False, reject_reason=None), make_analysis(), settings)
    assert p.reject_reason
    p = sanitize_plan(make_plan(keep=True, reject_reason="leftover"), make_analysis(), settings)
    assert p.keep and p.reject_reason is None


def test_input_plan_not_mutated(settings):
    plan = make_plan(trim_end=99, hashtags=["#x"])
    sanitize_plan(plan, make_analysis(), settings)
    assert plan.trim_end == 99 and plan.hashtags == ["#x"]


# --------------------------------------------------------------------------- sanitize_plan: Zooms/Emphasis


def test_zooms_limited(settings):
    zooms = [
        Zoom(start=2, end=4, scale=2.0, focus="face"),
        Zoom(start=3, end=5, scale=1.2),  # überlappt -> weg
        Zoom(start=6, end=8, scale=0.5, focus="gameplay"),
        Zoom(start=-5, end=0.6, scale=1.2),  # vor dem Trim (Rest 0.1 s) -> weg
        Zoom(start=19.5, end=40, scale=1.3),  # über Trim-Ende -> gekürzt
        Zoom(start=10, end=11, scale=1.2),  # komplett im Schnitt -> weg
    ]
    p = sanitize_plan(make_plan(zooms=zooms, remove_ranges=[(9.5, 11.5)]), make_analysis(), settings)
    assert [(z.start, z.end, z.scale, z.focus) for z in p.zooms] == [
        (2.0, 4.0, 1.5, "center"),  # face ohne Gesicht -> center
        (6.0, 8.0, 1.0, "gameplay"),
        (19.5, 20.0, 1.3, "center"),
    ]


def test_zoom_face_kept_when_face_present(settings):
    layout = LayoutHint(kind="facecam_split", facecam_box=Box(x=0.7, y=0, w=0.3, h=0.3), confidence=0.9)
    p = sanitize_plan(make_plan(zooms=[Zoom(start=2, end=4, scale=1.3, focus="face")]),
                      make_analysis(layout=layout), settings)
    assert p.zooms[0].focus == "face"


def test_emphasis_rules(settings, sample_transcript):
    # Wörter: no@0.60 way@1.02 he@1.44 just@1.86 did@2.28 that@2.70 ...
    items = [
        Emphasis(t=0.1, emoji="💀"),  # vor trim_start -> weg
        Emphasis(t=1.02, emoji="😂😂"),  # nur das erste Emoji
        Emphasis(t=1.10, emoji="🔥", sfx="pop"),  # gleiches Wort -> Emoji weg, SFX bleibt
        Emphasis(t=1.44, emoji="fire"),  # kein Emoji, kein SFX -> weg
        Emphasis(t=1.86, emoji="🤦‍♂️"),  # ZWJ-Folge bleibt ganz
        Emphasis(t=2.28, emoji=" 👍🏽 ", sfx="ding"),  # Hautton bleibt
        Emphasis(t=3.2, sfx="boom"),  # im Schnitt -> weg
        Emphasis(t=float("nan"), emoji="💀"),
    ]
    plan = make_plan(trim_start=0.5, trim_end=20.0, emphasis=items, remove_ranges=[(3.0, 3.5)])
    p = sanitize_plan(plan, make_analysis(transcript=sample_transcript), settings)
    assert [(e.t, e.emoji, e.sfx) for e in p.emphasis] == [
        (1.02, "😂", None),
        (1.1, None, "pop"),
        (1.86, "🤦‍♂️", None),
        (2.28, "👍🏽", "ding"),
    ]


def test_emoji_cap(settings):
    items = [Emphasis(t=1.0 + i, emoji="💀") for i in range(10)]
    p = sanitize_plan(make_plan(emphasis=items), make_analysis(), settings)
    assert len([e for e in p.emphasis if e.emoji]) == editor.MAX_EMOJIS


# --------------------------------------------------------------------------- sanitize_plan: Texte


def test_titles_hashtags_and_descriptions(settings):
    long_title = "This streamer found the most unbelievable hidden stunt ramp in all of Vice City and " \
                 "nobody expected what happened next #shorts"
    plan = make_plan(
        titles=PlatformText(youtube=long_title, tiktok="", instagram="  #Shorts  insane jump  "),
        descriptions=PlatformText(youtube="He actually made it.\n\n#gta6 #stunts #GTA6",
                                  tiktok="No way #shorts this worked", instagram="#only #tags"),
        hashtags=["#GTA6", "gta6", "Gaming", "gaming", "fun ny!", "#", "a" * 50, "x1", "x2", "x3", "x4", "x5"],
    )
    p = sanitize_plan(plan, make_analysis(), settings)
    assert len(p.titles.youtube) <= 100 and "#shorts" not in p.titles.youtube.lower()
    assert p.titles.youtube.endswith("nobody | GTA 6")  # Suchwort angehängt, Titel dafür gekürzt
    assert p.titles.tiktok == editor._clean_title(long_title, 100)  # leer -> YouTube-Titel (ohne Anhang)
    assert p.titles.instagram == "insane jump"
    assert p.descriptions.youtube == "He actually made it."
    assert p.descriptions.tiktok == "No way this worked"
    assert p.descriptions.instagram == ""
    assert p.hashtags[:4] == ["GTA6", "Gaming", "funny", "x1"]
    assert len(p.hashtags) == 8
    assert len({h.lower() for h in p.hashtags}) == len(p.hashtags)
    assert all("#" not in h for h in p.hashtags)


def test_gta6_hashtag_always_present(settings):
    p = sanitize_plan(make_plan(hashtags=["gaming"]), make_analysis(), settings)
    assert p.hashtags == ["gta6", "gaming"]


def test_all_caps_title_fixed(settings):
    plan = make_plan(titles=PlatformText(youtube="THIS GTA 6 NPC IS ABSOLUTELY INSANE", tiktok="OK", instagram="OK"))
    p = sanitize_plan(plan, make_analysis(), settings)
    assert p.titles.youtube == "This GTA 6 NPC is absolutely insane"


def test_hook_text_limits(settings):
    plan = make_plan(hook_text="  \"This is the craziest thing you will ever see 💀\" ")
    p = sanitize_plan(plan, make_analysis(), settings)
    assert len(p.hook_text.split()) <= 6 and len(p.hook_text) <= 32
    assert "💀" not in p.hook_text and '"' not in p.hook_text
    assert p.hook_text.startswith("This is the craziest")
    p = sanitize_plan(make_plan(hook_text="Supercalifragilisticexpialidocious-ultra-mega-word"),
                      make_analysis(), settings)
    assert len(p.hook_text) <= 32
    p = sanitize_plan(make_plan(hook_text="🔥💀"), make_analysis(), settings)
    assert p.hook_text  # Fallback aus dem Titel


def test_highlight_words_cleaned(settings):
    plan = make_plan(highlight_words=["insane jump!", "INSANE", "", "bro,", "a", "b", "c", "d"])
    p = sanitize_plan(plan, make_analysis(), settings)
    assert p.highlight_words == ["insane", "jump", "bro", "a", "b", "c"]


def test_layout_override_validated(settings):
    p = sanitize_plan(make_plan(layout="facecam_split"), make_analysis(), settings)
    assert p.layout is None  # keine Facecam erkannt
    p = sanitize_plan(make_plan(layout="vertical_native"), make_analysis(), settings)
    assert p.layout is None
    p = sanitize_plan(make_plan(layout="gameplay_fit"), make_analysis(), settings)
    assert p.layout == "gameplay_fit"
    layout = LayoutHint(kind="face_track", confidence=0.7)
    frames = [FrameSample(t=1, path="/x.jpg", faces=[FaceDet(box=Box(x=0.4, y=0.2, w=0.2, h=0.3), score=0.9)])]
    p = sanitize_plan(make_plan(layout="face_track"), make_analysis(layout=layout, frames=frames), settings)
    assert p.layout == "face_track"


def test_content_language_fallback(settings, sample_transcript):
    p = sanitize_plan(make_plan(content_language="English"), make_analysis(transcript=sample_transcript), settings)
    assert p.content_language == "en"
    p = sanitize_plan(make_plan(content_language="DE"), make_analysis(), settings)
    assert p.content_language == "de"


def test_frames_path_object(tmp_path):
    # Hilfsfunktion: ausgewählte Frames bleiben zeitlich sortiert
    frames = [FrameSample(t=t, path=str(Path(tmp_path) / f"{t}.jpg")) for t in (5.0, 1.0, 3.0)]
    assert [f.t for f in editor._select_frames(frames, 8)] == [1.0, 3.0, 5.0]
    assert editor._select_frames(frames, 0) == []


def test_reddit_popularity_and_engagement_format(settings):
    c = CandidateIn(source=SourceKind.reddit_post, source_id="1abc", url="https://reddit.com/r/GTA6/comments/1abc",
                    title="This NPC has no chill", creator_name="u/foo", platform_label="Reddit",
                    created_at=datetime(2026, 10, 19, 12, 0, tzinfo=UTC),
                    engagement={"score": 2400, "comments": 140, "upvote_ratio": 0.97},
                    extra={"subreddit": "GTA6", "post_id": "1abc", "media_url": "https://v.redd.it/x"})
    llm = FakeLLM(make_plan())
    plan_clip(llm, c, make_analysis(), [], settings, now=NOW)
    ctx = llm.calls[0]["content"][0]["text"]
    assert "Reddit score: 2,400 | Age: 12.0 h | Velocity: 200 upvotes/h" in ctx
    assert "comments=140" in ctx and "upvote_ratio=0.97" in ctx
    assert "- Subreddit: r/GTA6" in ctx and "media_url" not in ctx and "post_id" not in ctx


# --------------------------------------------------------------------------- Review-Fixes


def test_sfx_budget_counts_zoom_whooshes(settings):
    zooms = [Zoom(start=2.0, end=3.0, scale=1.2), Zoom(start=8.0, end=9.0, scale=1.2)]
    items = [
        Emphasis(t=2.1, sfx="whoosh"),  # fällt im Renderer mit dem Zoom-whoosh zusammen -> bleibt, zählt nicht
        Emphasis(t=4.0, sfx="boom"),
        Emphasis(t=8.45, emoji="💀", sfx="whoosh"),  # 0.45 s nach Zoom-Start: klänge doppelt -> sfx weg
        Emphasis(t=12.0, sfx="ding"),
        Emphasis(t=15.0, sfx="pop"),  # 2 Zoom-whooshs + boom + ding = 4 -> Budget voll
    ]
    p = sanitize_plan(make_plan(zooms=zooms, emphasis=items), make_analysis(), settings)
    assert [(e.t, e.emoji, e.sfx) for e in p.emphasis] == [
        (2.1, None, "whoosh"), (4.0, None, "boom"), (8.45, "💀", None), (12.0, None, "ding")]


def test_zoom_cap_is_three(settings):
    zooms = [Zoom(start=float(s), end=float(s) + 1.0, scale=1.2) for s in (1, 4, 7, 10, 13)]
    p = sanitize_plan(make_plan(zooms=zooms), make_analysis(), settings)
    assert len(p.zooms) == editor.MAX_ZOOMS == 3


def _words(*items: tuple[str, float, float]) -> Transcript:
    ws = [Word(text=t, start=a, end=b) for t, a, b in items]
    return Transcript(language="en", segments=[Segment(text=" ".join(w.text for w in ws), start=ws[0].start,
                                                       end=ws[-1].end, words=ws)])


def test_spoken_slur_is_cut_out_of_kept_part(settings):
    tr = _words(("bro", 3.0, 3.2), (" NIGGA,", 3.25, 3.45), ("look", 3.5, 3.7), ("faggots", 15.0, 15.4))
    p = sanitize_plan(make_plan(trim_start=0.5, trim_end=20.0), make_analysis(transcript=tr), settings)
    assert p.keep
    for a, b in [(3.25, 3.45), (15.0, 15.4)]:
        assert any(x <= a and b <= y for x, y in p.remove_ranges), p.remove_ranges
    assert all(y - x >= editor.MIN_REMOVE_S for x, y in p.remove_ranges)


def test_slur_on_trim_edge_moves_the_edge(settings):
    tr = _words(("Nigga", 0.4, 0.7), ("nigga", 19.95, 20.3))
    p = sanitize_plan(make_plan(trim_start=0.5, trim_end=20.0), make_analysis(duration=30.0, transcript=tr),
                      settings)
    assert p.keep and p.trim_start == pytest.approx(0.75) and p.trim_end == pytest.approx(19.9)


def test_slur_that_cannot_be_cut_rejects(settings, monkeypatch):
    monkeypatch.setattr(editor, "SLUR_MIN_CUT_S", 0.1)  # Schnitt kürzer als MIN_REMOVE_S -> wird verworfen
    tr = _words(("bro", 5.0, 5.2), ("nigga", 5.3, 5.35), ("look", 5.4, 5.6))
    p = sanitize_plan(make_plan(trim_start=0.5, trim_end=20.0), make_analysis(transcript=tr), settings)
    assert not p.keep and "Slur" in p.reject_reason and "behaltenen Teil" in p.reject_reason


def test_slur_in_texts(settings):
    p = sanitize_plan(make_plan(highlight_words=["insane", "retarded"], hashtags=["gta6", "f4ggot"]),
                      make_analysis(), settings)
    assert p.keep and p.highlight_words == ["insane"] and p.hashtags == ["gta6"]
    p = sanitize_plan(make_plan(titles=PlatformText(youtube="Retard moment in GTA 6", tiktok="t", instagram="i")),
                      make_analysis(), settings)
    assert not p.keep and "Slur" in p.reject_reason
    # harmlose Wörter, die ähnlich aussehen, bleiben
    p = sanitize_plan(make_plan(highlight_words=["spice", "figure", "snigger", "kicked"]), make_analysis(), settings)
    assert p.highlight_words == ["spice", "figure", "snigger", "kicked"]


def test_mentions_stripped_and_gta6_spelling(settings):
    plan = make_plan(
        titles=PlatformText(youtube="@xqc jumps the bridge in gta6", tiktok="@xqc GTAVI moment",
                            instagram="mail me at a@b.com #gta6"),
        descriptions=PlatformText(youtube="Clip by @xqc on Twitch.", tiktok="lol @someone", instagram="ok"),
        hook_text="@xqc did WHAT",
    )
    p = sanitize_plan(plan, make_analysis(), settings)
    assert p.titles.youtube == "xqc jumps the bridge in GTA 6"
    assert p.titles.tiktok == "xqc GTA 6 moment"
    assert p.titles.instagram == "mail me at a@b.com #gta6"  # E-Mail und Hashtag unangetastet
    assert p.descriptions.youtube == "Clip by xqc on Twitch." and p.descriptions.tiktok == "lol someone"
    assert p.hook_text == "xqc did WHAT"
    p = sanitize_plan(make_plan(titles=PlatformText(youtube="Grand Theft Auto VI cops are insane", tiktok="t",
                                                    instagram="i")), make_analysis(), settings)
    assert p.titles.youtube == "Grand Theft Auto VI cops are insane"


def test_twitch_clip_creator_not_shown_and_kick_category(settings, candidate):
    c = candidate.model_copy(update={"extra": {**candidate.extra, "clip_creator_name": "RandomViewer42",
                                                "broadcaster_login": "xqc", "game_id": "123"}})
    llm = FakeLLM(make_plan())
    plan_clip(llm, c, make_analysis(), [], settings, now=NOW, recent=[])
    ctx = llm.calls[0]["content"][0]["text"]
    assert "RandomViewer42" not in ctx and "is_featured=False" in ctx
    kick = c.model_copy(update={"source": SourceKind.kick_clip, "platform_label": "Kick",
                                "extra": {"category_slug": "grand-theft-auto-vi", "clip_creator_name": "v",
                                          "video_url": "https://kick.com/x.mp4"}})
    llm = FakeLLM(make_plan())
    plan_clip(llm, kick, make_analysis(), [], settings, now=NOW, recent=[])
    ctx = llm.calls[0]["content"][0]["text"]
    assert "- Platform category: grand-theft-auto-vi (Kick category slug)" in ctx and "kick.com" not in ctx
    settings.config.sources.twitch.game_names = ["Grand Theft Auto VI", "Grand Theft Auto V"]
    llm = FakeLLM(make_plan())
    plan_clip(llm, c, make_analysis(), [], settings, now=NOW, recent=[])
    assert "one of Grand Theft Auto VI, Grand Theft Auto V" in llm.calls[0]["content"][0]["text"]
    # manuell per URL eingereicht: Kategorie unbekannt -> keine Behauptung
    manual = c.model_copy(update={"extra": {"broadcaster_login": "xqc", "manual": True}})
    llm = FakeLLM(make_plan())
    plan_clip(llm, manual, make_analysis(), [], settings, now=NOW, recent=[])
    assert "Platform category" not in llm.calls[0]["content"][0]["text"]


def test_reddit_repost_poster_is_labeled(settings):
    c = CandidateIn(source=SourceKind.reddit_post, source_id="1x", url="https://reddit.com/r/GTA6/comments/1x",
                    title="look at this", creator_name="shroud", creator_handle="shroud", platform_label="Twitch",
                    created_at=datetime(2026, 10, 19, 12, 0, tzinfo=UTC),
                    extra={"subreddit": "GTA6", "author": "poster9", "via": "u/poster9", "flair": "Clip"})
    llm = FakeLLM(make_plan())
    plan_clip(llm, c, make_analysis(), [], settings, now=NOW, recent=[])
    ctx = llm.calls[0]["content"][0]["text"]
    assert "- Shared on Reddit by: u/poster9 (not the creator)" in ctx
    assert "- Subreddit: r/GTA6 (flair: Clip)" in ctx and "author=" not in ctx


def test_release_date_only_in_user_message_and_configurable(settings, candidate):
    for system in (prompts.SYSTEM_EDITOR, prompts.SYSTEM_MOMENTS):
        assert "2026" not in system  # Verschiebung darf den gecachten System-Prompt nicht ändern
    after = datetime(2026, 12, 1, tzinfo=UTC)
    assert editor._release_note(after) == "GTA VI was released 12 days ago, on 2026-11-19"
    object.__setattr__(settings.config.filters, "gta6_release_date", "2027-05-26")  # Feld kommt ggf. später
    llm = FakeLLM(make_plan())
    plan_clip(llm, candidate, make_analysis(), [], settings, now=after, recent=[])
    assert "planned to release in 176 days, on 2027-05-26" in llm.calls[0]["content"][0]["text"]
    object.__setattr__(settings.config.filters, "gta6_release_date", "bogus")
    assert editor._release_date(settings) == prompts.GTA6_RELEASE_DATE


def test_prompt_rules_present():
    s = prompts.SYSTEM_EDITOR
    for needle in ("captions show every spoken word", "self-harm", "Fan-made concept trailers", "AI-generated",
                   "FiveM", "Raw or lightly commented", "automatically plays a whoosh", "riser builds up",
                   "\"GTA 6\" exactly once", "No @mentions", "RECENT POSTS", "Never cut inside continuous action",
                   "At most 3 cuts"):
        assert needle in s, needle
    assert "He did NOT see this coming" not in s  # keine wörtlichen Hook-Beispiele (werden kopiert)
    assert "self-harm" in prompts.SYSTEM_MOMENTS and "FiveM" in prompts.SYSTEM_MOMENTS


def test_recent_posts_from_db(settings, db, candidate):
    from wdvi import db as dbmod
    from wdvi.models import Clip

    with dbmod.session_scope() as s:
        s.add(Clip(candidate_id=1, status="posted", video_path="/x.mp4", plan={"hook_text": "Old hook"},
                   titles={"youtube": "Old title"}, created_at=datetime(2026, 10, 18, tzinfo=UTC)))
        s.add(Clip(candidate_id=2, status="rejected", video_path="/y.mp4", plan={"hook_text": "Rejected hook"},
                   created_at=datetime(2026, 10, 19, tzinfo=UTC)))
        s.add(Clip(candidate_id=3, status="pending_review", video_path="/z.mp4",
                   plan={"hook_text": "New hook", "titles": {"youtube": "New title"}},
                   created_at=datetime(2026, 10, 19, 12, tzinfo=UTC)))
    assert editor._recent_posts() == [("New hook", "New title"), ("Old hook", "Old title")]
    llm = FakeLLM(make_plan())
    plan_clip(llm, candidate, make_analysis(), [], settings, now=NOW)
    ctx = llm.calls[0]["content"][0]["text"]
    assert '- hook "New hook" | title "New title"' in ctx and "Rejected hook" not in ctx


def test_recent_posts_without_db_is_empty(monkeypatch):
    from wdvi import db as dbmod

    monkeypatch.setattr(dbmod, "_engine", None)
    assert editor._recent_posts() == []
