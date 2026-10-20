from __future__ import annotations

import builtins
from pathlib import Path

import numpy as np
import pytest

from wdvi.media import audio_tags
from wdvi.media.audio_tags import music_probability, speech_ratio
from wdvi.schemas import Segment, Transcript, Word


@pytest.fixture(autouse=True)
def reset_tagger(monkeypatch):
    monkeypatch.setattr(audio_tags, "_pipe", None)
    monkeypatch.setattr(audio_tags, "_unavailable", False)
    monkeypatch.setattr(audio_tags, "_failed_at", None)


def test_speech_ratio(sample_transcript: Transcript):
    # 12 Wörter à 0.32 s, ohne Überlappung
    assert speech_ratio(sample_transcript, 8.0) == pytest.approx(12 * 0.32 / 8.0, abs=1e-3)
    assert speech_ratio(None, 8.0) == 0.0
    assert speech_ratio(sample_transcript, 0.0) == 0.0


def test_speech_ratio_merges_overlaps_and_clamps():
    words = [Word(text="a", start=0.0, end=2.0), Word(text="b", start=1.0, end=3.0),
             Word(text="c", start=9.0, end=12.0), Word(text="d", start=5.0, end=5.0)]
    t = Transcript(language="en", segments=[Segment(text="a b c", start=0, end=12, words=words)])
    assert speech_ratio(t, 10.0) == pytest.approx((3.0 + 1.0) / 10.0)


def test_music_probability_none_without_transformers(settings, sample_video: Path, monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name in ("torch", "transformers"):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert music_probability(sample_video, settings) is None
    assert audio_tags._unavailable is True
    assert music_probability(sample_video, settings) is None  # zweiter Aufruf: kein neuer Importversuch


def test_music_probability_max_over_windows(settings, monkeypatch):
    sr = 16000
    audio = np.full(sr * 35, 0.1, dtype=np.float32)  # 35 s -> 4 Fenster
    monkeypatch.setattr(audio_tags._ff, "decode_audio", lambda path, sr=16000: audio)
    monkeypatch.setattr(audio_tags, "_get_pipeline", lambda: object())
    seen: list[int] = []
    scores = [
        {"Speech": 0.9, "Music": 0.10, "Gunshot, gunfire": 0.8},
        {"Speech": 0.4, "Pop music": 0.62, "Music": 0.3},
        {"Speech": 0.8, "Musical instrument": 0.2},
        {"Hip hop music": 0.5, "Explosion": 0.95},
    ]

    def fake_score(pipe, window, sr_):
        seen.append(window.size)
        return scores[len(seen) - 1]

    monkeypatch.setattr(audio_tags, "_score_window", fake_score)
    assert music_probability("x.mp4", settings) == pytest.approx(0.62)
    assert seen == [sr * 10] * 4


def test_music_probability_runtime_failure_is_none(settings, monkeypatch):
    monkeypatch.setattr(audio_tags._ff, "decode_audio", lambda path, sr=16000: np.ones(16000 * 3, np.float32))
    monkeypatch.setattr(audio_tags, "_get_pipeline", lambda: object())

    def boom(*a):
        raise RuntimeError("CUDA error")

    monkeypatch.setattr(audio_tags, "_score_window", boom)
    assert music_probability("x.mp4", settings) is None


def test_music_probability_no_audio_is_zero(settings, monkeypatch):
    monkeypatch.setattr(audio_tags._ff, "decode_audio", lambda path, sr=16000: np.zeros(0, np.float32))
    monkeypatch.setattr(audio_tags, "_get_pipeline", lambda: object())
    assert music_probability("x.mp4", settings) == 0.0


def test_windows_spread_and_short_clip():
    sr = 16000
    assert len(audio_tags._windows(np.zeros(sr * 5), sr)) == 1
    w = audio_tags._windows(np.arange(sr * 120, dtype=np.float32), sr)
    assert len(w) == 6
    assert w[0][0] == 0 and w[-1][-1] == sr * 120 - 1  # Anfang und Ende abgedeckt
    assert all(x.size == sr * 10 for x in w)


def test_music_labels():
    for lbl in ("Music", "Pop music", "Background music", "Video game music", "Rapping", "Singing", "Song"):
        assert audio_tags.is_music_label(lbl)
    for lbl in ("Speech", "Gunshot, gunfire", "Explosion", "Laughter", "Vehicle"):
        assert not audio_tags.is_music_label(lbl)


def test_pipeline_load_failure_is_remembered(monkeypatch):
    import sys
    import types

    calls = []
    fake_tf = types.ModuleType("transformers")

    def pipeline(*a, **kw):
        calls.append(kw)
        raise OSError("huggingface.co blocked")

    fake_tf.pipeline = pipeline
    fake_torch = types.ModuleType("torch")
    fake_torch.cuda = types.SimpleNamespace(is_available=lambda: False)
    monkeypatch.setitem(sys.modules, "transformers", fake_tf)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    assert audio_tags._get_pipeline() is None
    assert audio_tags._get_pipeline() is None
    assert len(calls) == 1
    assert calls[0]["device"] == -1 and calls[0]["model"] == audio_tags.MODEL_ID and calls[0]["top_k"] is None
