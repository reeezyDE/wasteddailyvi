"""Encoder-Erkennung und ffmpeg-Argumente."""

from __future__ import annotations

import subprocess

import pytest

from wdvi.config import RenderConfig
from wdvi.render import encoder


@pytest.fixture(autouse=True)
def _reset():
    encoder.reset_cache()
    yield
    encoder.reset_cache()


def test_codec_args_x264():
    cfg = RenderConfig(x264_crf=18, fps=30)
    assert encoder.video_codec_args("libx264", cfg) == [
        "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-profile:v", "high",
        "-pix_fmt", "yuv420p", "-g", "60", "-movflags", "+faststart"]


def test_codec_args_nvenc():
    cfg = RenderConfig(nvenc_cq=19, fps=60)
    assert encoder.video_codec_args("h264_nvenc", cfg) == [
        "-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", "19", "-b:v", "0",
        "-profile:v", "high", "-pix_fmt", "yuv420p", "-g", "120", "-movflags", "+faststart"]


def test_forced_preferences():
    assert encoder.detect_encoder("x264") == "libx264"
    assert encoder.detect_encoder("nvenc") == "h264_nvenc"


@pytest.mark.parametrize(("rc", "expected"), [(0, "h264_nvenc"), (1, "libx264")])
def test_auto_runs_test_encode_once(monkeypatch, rc, expected):
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, rc, b"", b"Cannot load libcuda.so.1")

    monkeypatch.setattr(encoder.subprocess, "run", fake_run)
    assert encoder.detect_encoder("auto") == expected
    assert encoder.detect_encoder("auto") == expected  # gecacht
    assert len(calls) == 1
    assert "h264_nvenc" in calls[0] and "color=c=black:s=256x256:d=0.1" in calls[0]


def test_auto_without_ffmpeg(monkeypatch):
    def boom(cmd, **kw):
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr(encoder.subprocess, "run", boom)
    assert encoder.detect_encoder("auto") == "libx264"


def test_auto_real_environment():
    assert encoder.detect_encoder("auto") in ("h264_nvenc", "libx264")
