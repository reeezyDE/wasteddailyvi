"""mine_moments: Prompt aus Transkript/Heatmap/Kapiteln, Kappung langer Videos, Bereinigung der Picks."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from wdvi.brain import prompts
from wdvi.brain.editor import MOMENT_WORD_BUDGET, mine_moments
from wdvi.schemas import CandidateIn, MomentPick, MomentPicks, Segment, SourceKind, Transcript
from wdvi.sources.youtube import HeatPoint, VideoContext

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


class FakeLLM:
    def __init__(self, result) -> None:
        self.result = result
        self.calls: list[dict] = []

    def complete_json(self, system, content, schema_model, *, max_tokens=16000):
        self.calls.append({"system": system, "content": content, "schema_model": schema_model,
                           "max_tokens": max_tokens})
        return self.result


def _video(duration: float = 600.0) -> CandidateIn:
    return CandidateIn(source=SourceKind.youtube_video, source_id="dQw4w9WgXcQ",
                       url="https://www.youtube.com/watch?v=dQw4w9WgXcQ", title="GTA 6 chaos stream",
                       creator_name="SomeStreamer", platform_label="YouTube",
                       created_at=datetime(2026, 10, 20, 2, 0, tzinfo=UTC), duration_s=duration, view_count=50_000)


def _ctx(duration: float = 600.0, seg_len: float = 5.0, words_per_seg: int = 10, heat=True,
         transcript=True) -> VideoContext:
    segs = []
    t = 0.0
    i = 0
    while t < duration:
        text = " ".join(f"s{i}w{j}" for j in range(words_per_seg))
        segs.append(Segment(text=text, start=round(t, 2), end=round(min(t + seg_len - 0.3, duration), 2)))
        t += seg_len
        i += 1
    heatmap = []
    if heat:
        n = 100
        for k in range(n):
            st = duration * k / n
            val = 1.0 if k == 42 else (0.7 if k == 80 else 0.2)
            heatmap.append(HeatPoint(start=st, end=st + duration / n, value=val))
    return VideoContext(
        video_id="dQw4w9WgXcQ", title="GTA 6 chaos stream", channel="SomeStreamer", duration=duration,
        description="Watch me break GTA 6. Sponsored by nobody.",
        chapters=[{"start_time": 0, "end_time": 120, "title": "Intro"},
                  {"start_time": 120, "end_time": duration, "title": "Police chase"}],
        heatmap=heatmap, transcript=Transcript(language="en", segments=segs) if transcript else None,
        view_count=50_000,
    )


def test_prompt_contents(settings):
    llm = FakeLLM(MomentPicks(moments=[]))
    assert mine_moments(llm, _video(), _ctx(), settings, now=NOW) == []
    call = llm.calls[0]
    assert call["system"] is prompts.SYSTEM_MOMENTS and call["schema_model"] is MomentPicks
    text = "\n".join(b["text"] for b in call["content"])
    assert 'Title: "GTA 6 chaos stream"' in text
    assert "Views: 50,000 | Age: 10.0 h | Velocity: 5,000 views/h" in text
    assert "Current date (UTC): 2026-10-20" in text
    assert "GTA VI is planned to release in 30 days, on 2026-11-19" in text  # Termin nur in der User-Nachricht
    assert call["max_tokens"] == 64000
    assert "At most 3 moments" in text
    assert "2:00 (120 s) Police chase" in text
    peaks = text[text.index("MOST REPLAYED"):]
    assert peaks.splitlines()[1].startswith("- 252-258 s")  # stärkster Peak zuerst
    assert "[0.0-4.7] s0w0" in text
    assert call["content"][-1]["text"] == prompts.MOMENTS_TASK.format(max_moments=3)


def test_picks_sanitized(settings):
    picks = MomentPicks(moments=[
        MomentPick(start=100.0, end=170.0, score=90, why="Too long chase."),  # -> <= 55 s
        MomentPick(start=301.0, end=309.0, score=70, why="Short crash."),  # -> auf 15 s gepolstert
        MomentPick(start=120.0, end=140.0, score=60, why="Overlaps the chase."),  # Überlappung -> weg
        MomentPick(start=590.0, end=640.0, score=75, why="Ends after video."),  # geclampt
        MomentPick(start=460.0, end=430.0, score=80, why="Reversed."),  # vertauscht
        MomentPick(start=200.0, end=230.0, score=101, why="  Big   reaction. "),
    ])
    settings.config.sources.youtube.moments_per_video = 4
    out = mine_moments(FakeLLM(picks), _video(), _ctx(), settings, now=NOW)
    assert len(out) == 4
    assert [m.score for m in out] == sorted([m.score for m in out], reverse=True)
    assert out[0].score == 100 and out[0].why == "Big reaction."
    for m in out:
        assert 15.0 - 1e-6 <= m.end - m.start <= 55.0 + 1e-6
        assert 0.0 <= m.start < m.end <= 600.0
    for a in out:
        for b in out:
            if a is not b:
                assert a.end <= b.start or b.end <= a.start
    by_why = {m.why: m for m in out}
    chase = by_why["Too long chase."]
    assert chase.start == 100.0 and chase.end <= 155.0
    assert "Overlaps the chase." not in by_why
    assert by_why["Reversed."].start == 430.0 and by_why["Reversed."].end == 459.7  # an Segmentende
    assert "Short crash." not in by_why  # Score 70 ist der schwächste -> durch max 4 gekappt


def test_short_pick_padded_and_snapped(settings):
    picks = MomentPicks(moments=[MomentPick(start=301.2, end=309.0, score=70, why="Short crash.")])
    out = mine_moments(FakeLLM(picks), _video(), _ctx(), settings, now=NOW)
    m = out[0]
    assert m.end - m.start >= 15.0 - 1e-6
    assert m.start <= 300.0 and m.end >= 309.0


def test_snap_to_segment_edges(settings):
    picks = MomentPicks(moments=[MomentPick(start=101.3, end=129.0, score=80, why="x")])
    out = mine_moments(FakeLLM(picks), _video(), _ctx(), settings, now=NOW)
    assert out[0].start == 100.0  # Segmentstart innerhalb 2 s
    assert out[0].end == 129.7  # Segmentende innerhalb 2 s


def test_moments_per_video_cap(settings):
    picks = MomentPicks(moments=[MomentPick(start=i * 60.0, end=i * 60.0 + 30, score=50 + i, why=str(i))
                                 for i in range(8)])
    settings.config.sources.youtube.moments_per_video = 2
    out = mine_moments(FakeLLM(picks), _video(), _ctx(), settings, now=NOW)
    assert [m.why for m in out] == ["7", "6"]


def test_no_transcript_no_heatmap_skips_llm(settings):
    llm = FakeLLM(MomentPicks(moments=[]))
    assert mine_moments(llm, _video(), _ctx(heat=False, transcript=False), settings, now=NOW) == []
    assert llm.calls == []


def test_heatmap_only_still_works(settings):
    picks = MomentPicks(moments=[MomentPick(start=250.0, end=280.0, score=85, why="Peak.")])
    llm = FakeLLM(picks)
    out = mine_moments(llm, _video(), _ctx(transcript=False), settings, now=NOW)
    assert out[0].start == 250.0 and out[0].end == 280.0
    assert "TRANSCRIPT: none" in llm.calls[0]["content"][0]["text"]


def test_long_video_transcript_is_capped_around_peaks(settings):
    duration = 4 * 3600.0  # 4 h, 2880 Segmente x 15 Wörter = 43 200 Wörter
    ctx = _ctx(duration=duration, words_per_seg=15)
    llm = FakeLLM(MomentPicks(moments=[]))
    mine_moments(llm, _video(duration), ctx, settings, now=NOW)
    text = llm.calls[0]["content"][0]["text"]
    transcript = text[text.index("TRANSCRIPT"):]
    words = len(re.findall(r"\bs\d+w\d+\b", transcript))
    assert 0 < words <= MOMENT_WORD_BUDGET
    assert "omitted" in transcript
    # stärkster Peak (k=42 -> 6048 s) und Kapitelstart (120 s) sind enthalten
    peak_seg = int(6048 // 5)
    assert f"s{peak_seg}w0" in transcript
    assert "s24w0" in transcript  # 120 s
    assert len(text) < 120_000


def test_unknown_duration_uses_transcript_end(settings):
    ctx = _ctx(duration=300.0)
    ctx.duration = 0
    video = _video(duration=300.0)
    video.duration_s = None
    picks = MomentPicks(moments=[MomentPick(start=280.0, end=330.0, score=80, why="End.")])
    out = mine_moments(FakeLLM(picks), video, ctx, settings, now=NOW)
    assert out[0].end <= 300.0
