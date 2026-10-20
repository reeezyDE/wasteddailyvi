"""SFX-Synthese, Mischung und Loudness-Normalisierung."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import numpy as np
import pytest

from tests.conftest import make_video
from wdvi.render import audio

LENGTHS = {"pop": 0.09, "whoosh": 0.4, "boom": 0.8, "ding": 0.6, "riser": 1.2, "record_scratch": 0.35}


def _probe(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format",
                          str(path)], capture_output=True, check=True).stdout
    return json.loads(out)


def _loudness(path: Path) -> tuple[float, float]:
    err = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-map", "0:a",
                          "-af", "ebur128=peak=true", "-f", "null", "-"], capture_output=True, text=True).stderr
    i = float(re.findall(r"I:\s+(-?[\d.]+|-inf) LUFS", err)[-1])
    peak = float(re.findall(r"Peak:\s+(-?[\d.]+|-inf) dBFS", err)[-1])
    return i, peak


@pytest.mark.parametrize("name", list(LENGTHS))
def test_synth_sfx_shape_and_peak(name):
    sig = audio.synth_sfx(name)
    assert sig.dtype == np.float32 and sig.ndim == 1
    assert len(sig) == pytest.approx(LENGTHS[name] * 48000, abs=2)
    peak = float(np.max(np.abs(sig)))
    assert 0.85 <= peak <= 0.9 + 1e-6
    assert np.isfinite(sig).all()
    assert abs(sig[-1]) < 0.05  # kein Knacken am Ende


def test_synth_sfx_other_rate_and_unknown():
    assert len(audio.synth_sfx("pop", sr=44100)) == pytest.approx(0.09 * 44100, abs=2)
    with pytest.raises(ValueError):
        audio.synth_sfx("airhorn")


def test_synth_sfx_is_deterministic_and_copy():
    a = audio.synth_sfx("whoosh")
    a[:] = 0
    assert np.abs(audio.synth_sfx("whoosh")).max() > 0.5


def test_mix_sfx_anchors():
    mix = np.zeros((48000 * 3, 2), np.float32)
    audio.mix_sfx(mix, [(2.0, "riser")], gain_db=0.0)
    energy = np.abs(mix[:, 0])
    assert energy[: int(0.7 * 48000)].max() == 0  # Riser beginnt 1.2 s vor dem Moment
    assert energy[int(1.9 * 48000): int(2.0 * 48000)].max() > 0.3  # ... und gipfelt direkt davor
    assert energy[int(2.05 * 48000):].max() == 0
    mix2 = np.zeros((48000, 2), np.float32)
    audio.mix_sfx(mix2, [(0.95, "boom"), (-1.0, "pop")], gain_db=-6.0)  # am Ende abgeschnitten, kein Fehler
    assert np.abs(mix2).max() <= 0.9 * 10 ** (-6 / 20) + 1e-6


def test_build_audio_loudness_and_format(settings, sample_video, tmp_path):
    out = audio.build_audio(sample_video, [(1.0, "pop"), (2.0, "whoosh"), (4.0, "boom"), (6.0, "ding")], settings,
                            tmp_path / "a.wav")
    st = _probe(out)["streams"][0]
    assert st["codec_name"] == "pcm_s16le" and st["sample_rate"] == "48000" and st["channels"] == 2
    assert float(st["duration"]) == pytest.approx(8.0, abs=0.1)
    i, peak = _loudness(out)
    assert i == pytest.approx(settings.config.render.target_lufs, abs=1.5)
    assert peak <= -0.5


def test_build_audio_without_audio_stream(settings, tmp_path):
    silent = make_video(tmp_path / "noaudio.mp4", seconds=3, size="320x240", audio=False)
    out = audio.build_audio(silent, [(1.0, "boom")], settings, tmp_path / "a.wav")
    st = _probe(out)["streams"][0]
    assert float(st["duration"]) == pytest.approx(3.0, abs=0.1)
    assert st["channels"] == 2


def test_build_audio_with_music_bed(settings, sample_video, tmp_path):
    music = tmp_path / "music.wav"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    "sine=frequency=220:sample_rate=44100:duration=2", "-ac", "2", str(music)], check=True)
    out = audio.build_audio(sample_video, [], settings, tmp_path / "a.wav", music_track=music)
    assert float(_probe(out)["streams"][0]["duration"]) == pytest.approx(8.0, abs=0.1)


def test_duck_envelope_ducks_loud_parts():
    prog = np.zeros((48000 * 2, 2), np.float32)
    prog[48000:] = 0.5  # zweite Sekunde laut
    env = audio._duck_envelope(prog)
    assert env[: 24000].min() > 0.95
    assert env[-1000:].max() < 0.5
