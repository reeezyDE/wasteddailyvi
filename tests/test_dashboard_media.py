"""Dashboard: /media/clips liefert nur Dateien aus clips_dir (Range-Requests, kein Path-Traversal)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from tests.test_dashboard_support import add_clip, dash_fixture  # noqa: F401  (Fixture)
from wdvi.dashboard import _data


def test_media_serves_file_with_content_type(dash):
    f = dash.clips_dir / "sub" / "clip.mp4"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"0123456789" * 100)
    r = dash.client.get("/media/clips/sub/clip.mp4")
    assert r.status_code == 200
    assert r.content == f.read_bytes()
    assert r.headers["content-type"] == "video/mp4"
    assert r.headers["accept-ranges"] == "bytes"


def test_media_supports_range_requests(dash):
    f = dash.clips_dir / "clip.mp4"
    f.write_bytes(bytes(range(256)) * 4)
    r = dash.client.get("/media/clips/clip.mp4", headers={"Range": "bytes=0-1"})
    assert r.status_code == 206
    assert r.content == bytes([0, 1])
    assert r.headers["content-range"] == "bytes 0-1/1024"
    r = dash.client.get("/media/clips/clip.mp4", headers={"Range": "bytes=1000-"})
    assert r.status_code == 206 and len(r.content) == 24


@pytest.mark.slow
def test_media_range_on_real_rendered_video(dash, sample_video):
    dst = dash.clips_dir / "real.mp4"
    shutil.copy(sample_video, dst)
    size = dst.stat().st_size
    r = dash.client.get("/media/clips/real.mp4", headers={"Range": f"bytes={size - 100}-{size - 1}"})
    assert r.status_code == 206
    assert r.content == dst.read_bytes()[-100:]
    assert r.headers["content-range"] == f"bytes {size - 100}-{size - 1}/{size}"


def test_media_blocks_path_traversal(dash):
    data_dir = Path(dash.settings.config.paths.data_dir)
    (data_dir / "secret.txt").write_text("TOPSECRET", encoding="utf-8")
    tokens_dir = Path(dash.settings.config.paths.tokens_dir)
    tokens_dir.mkdir(parents=True, exist_ok=True)
    (tokens_dir / "youtube.json").write_text('{"refresh_token": "TOPSECRET"}', encoding="utf-8")
    for path in (
        "/media/clips/../secret.txt",
        "/media/clips/%2e%2e/secret.txt",
        "/media/clips/..%2fsecret.txt",
        "/media/clips/%2e%2e%2ftokens%2fyoutube.json",
        "/media/clips/..%5csecret.txt",
        "/media/clips/%2fetc%2fpasswd",
        "/media/clips//etc/passwd",
        f"/media/clips/{data_dir.resolve()}/secret.txt",
        "/static/../media/clips/../secret.txt",
        "/static/%2e%2e/%2e%2e/%2e%2e/data/secret.txt",
    ):
        r = dash.client.get(path)
        assert r.status_code in (400, 401, 403, 404), (path, r.status_code)
        assert "TOPSECRET" not in r.text, path
        assert "root:" not in r.text, path


def test_media_blocks_symlinks_out_of_clips_dir(dash):
    data_dir = Path(dash.settings.config.paths.data_dir)
    (data_dir / "secret.txt").write_text("TOPSECRET", encoding="utf-8")
    try:
        os.symlink(data_dir / "secret.txt", dash.clips_dir / "link.mp4")
    except OSError:
        pytest.skip("Symlinks nicht möglich")
    r = dash.client.get("/media/clips/link.mp4")
    assert r.status_code == 404
    assert "TOPSECRET" not in r.text


def test_media_no_directory_listing_and_get_only(dash):
    (dash.clips_dir / "a.mp4").write_bytes(b"x")
    assert dash.client.get("/media/clips/").status_code == 404
    assert dash.client.get("/media/clips").status_code == 404
    assert dash.client.post("/media/clips/a.mp4").status_code == 405
    assert dash.client.get("/media/clips/fehlt.mp4").status_code == 404


def test_media_url_helper(dash, tmp_path):
    clips = dash.clips_dir
    (clips / "x y.mp4").write_bytes(b"x")
    assert _data.media_url(str(clips / "x y.mp4"), clips) == "/media/clips/x%20y.mp4"
    assert _data.media_url(str(clips / "missing.mp4"), clips) == ""
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"x")
    assert _data.media_url(str(outside), clips) == ""
    assert _data.media_url(str(clips / ".." / "outside.mp4"), clips) == ""
    assert _data.media_url(None, clips) == ""


def test_media_url_with_relative_data_dir(dash, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    clips = Path("data/clips")
    clips.mkdir(parents=True, exist_ok=True)
    (clips / "r.mp4").write_bytes(b"x")
    assert _data.media_url("data/clips/r.mp4", clips) == "/media/clips/r.mp4"
