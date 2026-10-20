"""Studio-Modus: prepare -> plan.json -> render (Claude Code als Editor, ohne API)."""

from __future__ import annotations

import json

import pytest

from wdvi import studio
from wdvi.schemas import Box, Segment, Transcript, Word


class FakeTranscriber:
    def __init__(self, tr: Transcript):
        self.tr = tr

    def transcribe(self, path, language=None):
        return self.tr


def test_transcript_text_compact(sample_transcript):
    txt = studio.transcript_text(sample_transcript)
    assert txt.startswith("Sprache: en")
    assert "no@0.60" in txt and "jump@" in txt
    assert studio.transcript_text(None).startswith("(kein Transkript")


def test_parse_box():
    assert studio._parse_box("0,0.5,0.2,0.3") == Box(x=0, y=0.5, w=0.2, h=0.3)
    assert studio._parse_box(None) is None


@pytest.mark.slow
def test_prepare_frame_render_roundtrip(tmp_path, settings, sample_video):
    words = [Word(text=w, start=1.0 + i * 0.5, end=1.4 + i * 0.5) for i, w in enumerate("no way he did that".split())]
    tr = Transcript(language="en", segments=[Segment(text="no way he did that", start=1.0, end=3.4, words=words)])
    work = tmp_path / "clip"
    p = studio.prepare(sample_video, work, settings, start=1.0, end=7.0, transcriber=FakeTranscriber(tr))
    assert (work / "source.mp4").is_file() and p.sheet.is_file() and p.transcript_txt.is_file()
    assert 5.5 < p.analysis.info.duration < 6.5
    assert "Shot-Arten" in studio.summary(p)
    still = studio.grid_still(work / "source.mp4", 2.0, work / "frame.jpg")
    assert still.is_file()

    plan = {
        "keep": True, "virality_score": 80, "reasoning": "test", "trim_start": 0.5, "trim_end": 5.5,
        "hook_text": "", "hook_style": "none",
        "titles": {"youtube": "t", "tiktok": "t", "instagram": "t"},
        "descriptions": {"youtube": "d", "tiktok": "d", "instagram": "d"},
        "shots": [{"start": 0.5, "end": 3.0, "kind": "fit"}, {"start": 3.0, "end": 5.5, "kind": "gameplay",
                                                               "transition": "punch"}],
        "word_colors": [{"word": "way", "color": "green"}],
        "outro_text": "WOULD YOU DO THAT?",
    }
    settings.config.filters.min_output_duration_s = 3.0
    (work / "plan.json").write_text(json.dumps(plan))
    video, preview, clean = studio.render(work, work / "plan.json", settings, "Tester", "Twitch")
    assert video.is_file() and preview.is_file()
    assert (work / "plan.sanitized.json").is_file()
    assert len(clean.shots) == 2
    assert "LUFS" in studio.loudness(video)
