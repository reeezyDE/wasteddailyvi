from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.conftest import make_video
from wdvi.media import probe as probe_mod
from wdvi.media.probe import ProbeError, probe


def test_probe_sample_video(sample_video: Path):
    info = probe(sample_video)
    assert (info.width, info.height) == (1280, 720)
    assert info.fps == pytest.approx(30.0, abs=0.01)
    assert info.duration == pytest.approx(8.0, abs=0.2)
    assert info.has_audio is True
    assert info.path == str(sample_video)


def test_probe_vertical_video(vertical_video: Path):
    info = probe(vertical_video)
    assert (info.width, info.height) == (720, 1280)
    assert info.duration == pytest.approx(4.0, abs=0.2)


def test_probe_without_audio(tmp_path: Path):
    info = probe(make_video(tmp_path / "silent.mp4", seconds=2, size="320x240", audio=False))
    assert info.has_audio is False
    assert (info.width, info.height) == (320, 240)


def test_probe_respects_rotation(tmp_path: Path):
    src = make_video(tmp_path / "landscape.mp4", seconds=2, size="640x360", audio=False)
    rotated = tmp_path / "rotated.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-display_rotation", "90", "-i", str(src),
                    "-c", "copy", str(rotated)], check=True)
    info = probe(rotated)
    assert (info.width, info.height) == (360, 640)


def test_probe_ntsc_frame_rate(tmp_path: Path):
    p = tmp_path / "ntsc.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc2=size=320x180:rate=30000/1001:duration=1", "-c:v", "libx264", "-preset",
                    "ultrafast", "-pix_fmt", "yuv420p", str(p)], check=True)
    assert probe(p).fps == pytest.approx(29.97, abs=0.01)


def test_probe_missing_file(tmp_path: Path):
    with pytest.raises(ProbeError, match="nicht gefunden"):
        probe(tmp_path / "nope.mp4")


def test_probe_garbage_file(tmp_path: Path):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not a video at all" * 10)
    with pytest.raises(ProbeError):
        probe(bad)


def test_parse_rate_and_rotation_helpers():
    assert probe_mod._parse_rate("30000/1001") == pytest.approx(29.97, abs=0.01)
    assert probe_mod._parse_rate("0/0") == 0.0
    assert probe_mod._parse_rate(None) == 0.0
    assert probe_mod._rotation({"side_data_list": [{"rotation": -90}]}) == 270
    assert probe_mod._rotation({"tags": {"rotate": "180"}}) == 180
    assert probe_mod._rotation({}) == 0
