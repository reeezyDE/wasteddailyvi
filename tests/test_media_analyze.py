from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import make_video
from wdvi.media import analyze as analyze_mod
from wdvi.media import audio_tags
from wdvi.media.analyze import analyze
from wdvi.media.faces import FaceDetector
from wdvi.schemas import MediaAnalysis, Transcript


class FakeTranscriber:
    def __init__(self, transcript: Transcript):
        self.transcript = transcript
        self.calls: list[tuple[str, str | None]] = []

    def transcribe(self, path, language=None) -> Transcript:
        self.calls.append((str(path), language))
        return self.transcript


@pytest.fixture
def detector(settings) -> FaceDetector:
    return FaceDetector(settings, allow_download=False)


@pytest.fixture(autouse=True)
def no_music_model(monkeypatch):
    monkeypatch.setattr(audio_tags, "_get_pipeline", lambda: None)


@pytest.mark.slow
def test_analyze_sample_video(sample_video: Path, tmp_path: Path, settings, sample_transcript, detector):
    tr = FakeTranscriber(sample_transcript)
    res = analyze(sample_video, tmp_path / "work", settings, tr, detector, language="en")
    assert isinstance(res, MediaAnalysis)
    assert tr.calls == [(str(sample_video), "en")]
    assert (res.info.width, res.info.height) == (1280, 720)
    assert res.transcript == sample_transcript
    assert res.music_prob is None  # kein Tagger installiert
    assert 0.4 < res.speech_ratio < 0.6
    assert res.loudness_peaks and 4.9 <= res.loudness_peaks[0] <= 5.7
    assert res.layout.kind == "gameplay_fit"
    assert len(res.frames) == settings.config.llm.max_frames
    for f in res.frames:
        assert Path(f.path).is_file() and Path(f.path).parent == tmp_path / "work" / "frames"
        assert 0 < f.t < res.info.duration
    ts = [f.t for f in res.frames]
    assert ts == sorted(ts)
    for want in (res.loudness_peaks[0], res.loudness_peaks[0] + 0.5):  # Einschlag + Payoff
        assert any(abs(t - want) < 1e-3 for t in ts)
    assert len(res.phashes) == 3
    # rundreisefähig (wird als JSON in der DB abgelegt)
    assert MediaAnalysis.model_validate_json(res.model_dump_json()) == res


@pytest.mark.slow
def test_analyze_vertical_video(vertical_video: Path, tmp_path: Path, settings, sample_transcript, detector):
    settings.config.llm.max_frames = 3
    settings.config.llm.frame_width = 360
    res = analyze(vertical_video, tmp_path, settings, FakeTranscriber(sample_transcript), detector)
    assert res.layout.kind == "vertical_native"
    assert len(res.frames) == 3


def test_analyze_without_audio_skips_transcription(tmp_path: Path, settings, sample_transcript, detector):
    silent = make_video(tmp_path / "silent.mp4", seconds=3, size="640x360", audio=False)
    tr = FakeTranscriber(sample_transcript)
    settings.config.llm.max_frames = 2
    res = analyze(silent, tmp_path / "w", settings, tr, detector)
    assert tr.calls == []
    assert res.transcript is None and res.speech_ratio == 0.0
    assert res.music_prob == 0.0 and res.loudness_peaks == []


def test_optional_step_failures_do_not_raise(sample_video: Path, tmp_path: Path, settings, sample_transcript,
                                             detector, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("kaputt")

    monkeypatch.setattr(analyze_mod.audio_tags, "music_probability", boom)
    monkeypatch.setattr(analyze_mod.signals, "loudness_peaks", boom)
    monkeypatch.setattr(analyze_mod.faces_mod, "analyze_layout", boom)
    monkeypatch.setattr(analyze_mod.dedup, "phashes", boom)
    settings.config.llm.max_frames = 1
    res = analyze(sample_video, tmp_path, settings, FakeTranscriber(sample_transcript), detector)
    assert res.music_prob is None and res.loudness_peaks == [] and res.phashes == []
    assert res.layout.kind == "gameplay_fit" and res.layout.confidence == 0.0


def test_transcription_failure_propagates(sample_video: Path, tmp_path: Path, settings, detector):
    class Broken:
        def transcribe(self, path, language=None):
            raise RuntimeError("whisper kaputt")

    with pytest.raises(RuntimeError, match="whisper kaputt"):
        analyze(sample_video, tmp_path, settings, Broken(), detector)


@pytest.mark.slow
def test_analyze_real_facecam_video(tmp_path: Path, settings, sample_transcript, detector):
    from tests.test_media_faces import _compose

    video = _compose(tmp_path / "stream.mp4", "[1:v]scale=320:-2[cam];[0:v][cam]overlay=0:H-h:shortest=1")
    settings.config.llm.max_frames = 4
    res = analyze(video, tmp_path / "work", settings, FakeTranscriber(sample_transcript), detector)
    assert res.info.has_audio is False and res.transcript is None
    assert res.layout.kind == "facecam_split"
    assert sum(1 for f in res.frames if f.faces) >= 3  # Gesichter auch in Claudes Standbildern
    f0 = next(f for f in res.frames if f.faces)
    assert f0.faces[0].box.cx < 0.25 and f0.faces[0].box.cy > 0.6
