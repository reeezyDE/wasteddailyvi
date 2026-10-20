"""Echte End-to-End-Renders mit ffmpeg (langsam, laufen aber standardmäßig mit)."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import httpx
import numpy as np
import pytest
import respx

from tests.conftest import make_video
from wdvi.render import encoder, overlays, renderer, timeline
from wdvi.schemas import (
    Box,
    EditPlan,
    Emphasis,
    LayoutHint,
    MediaAnalysis,
    MediaInfo,
    PlatformText,
    TimeMap,
    Zoom,
)

pytestmark = pytest.mark.slow

SVG = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 36 36">'
       b'<circle cx="18" cy="18" r="16" fill="#FFCC4D"/></svg>')


@pytest.fixture(autouse=True)
def _no_network():
    overlays._failed.clear()
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r"https://raw\.githubusercontent\.com/.*").mock(
            return_value=httpx.Response(200, content=SVG))
        yield
    encoder.reset_cache()


def _plan(**kw) -> EditPlan:
    pt = PlatformText(youtube="t", tiktok="t", instagram="t")
    base = dict(keep=True, virality_score=85, reasoning="r", trim_start=0.0, trim_end=8.0, hook_text="NO WAY 💀",
                titles=pt, descriptions=pt)
    base.update(kw)
    return EditPlan(**base)


def _analysis(path: Path, layout: LayoutHint, transcript=None, w=1280, h=720, dur=8.0, audio=True) -> MediaAnalysis:
    return MediaAnalysis(info=MediaInfo(path=str(path), duration=dur, width=w, height=h, fps=30, has_audio=audio),
                         transcript=transcript, layout=layout)


def _probe(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format",
                          str(path)], capture_output=True, check=True).stdout
    return json.loads(out)


def _lufs(path: Path) -> float:
    err = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-map", "0:a", "-af", "ebur128",
                          "-f", "null", "-"], capture_output=True, text=True).stderr
    return float(re.findall(r"I:\s+(-?[\d.]+) LUFS", err)[-1])


def _frame(path: Path, t: float) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(1920, 1080, 3)


def _check_output(res, tm: TimeMap, expect_lufs: float | None = -14.0) -> dict:
    video = Path(res.video_path)
    assert video.is_file() and Path(res.thumb_path).is_file()
    info = _probe(video)
    vs = next(s for s in info["streams"] if s["codec_type"] == "video")
    aus = [s for s in info["streams"] if s["codec_type"] == "audio"]
    assert (vs["width"], vs["height"]) == (1080, 1920) == (res.width, res.height)
    assert vs["codec_name"] == "h264" and vs["pix_fmt"] == "yuv420p"
    assert aus and aus[0]["codec_name"] == "aac" and aus[0]["sample_rate"] == "48000"
    assert float(info["format"]["duration"]) == pytest.approx(tm.duration, abs=0.15)
    assert res.duration == pytest.approx(tm.duration, abs=0.15)
    if expect_lufs is not None:
        assert _lufs(video) == pytest.approx(expect_lufs, abs=2.0)
    thumb = _probe(Path(res.thumb_path))["streams"][0]
    assert thumb["codec_name"] == "mjpeg" and (thumb["width"], thumb["height"]) == (1080, 1920)
    return info


def test_render_gameplay_fit_with_zooms_emphasis_hook(settings, sample_video, sample_transcript, candidate,
                                                      tmp_path):
    plan = _plan(trim_start=0.5, trim_end=7.5, remove_ranges=[(3.0, 3.5)],
                 zooms=[Zoom(start=4.0, end=6.0, scale=1.3, focus="gameplay"),
                        Zoom(start=3.1, end=3.4, scale=1.2, focus="center")],  # liegt im Schnitt -> entfällt
                 emphasis=[Emphasis(t=5.0, emoji="💀", sfx="boom"), Emphasis(t=1.2, sfx="pop"),
                           Emphasis(t=3.2, emoji="🔥")],  # im Schnitt
                 highlight_words=["insane"], layout="gameplay_fit")
    analysis = _analysis(sample_video, LayoutHint(kind="gameplay_fit"), sample_transcript)
    res = renderer.render_clip(sample_video, analysis, plan, candidate, settings, tmp_path / "work",
                               tmp_path / "out")
    tm = timeline.build_timemap(plan, 8.0)
    assert tm.duration == pytest.approx(6.5)
    _check_output(res, tm)
    assert re.fullmatch(r"twitch_clip_AwkwardHelplessSalamanderSwiftRage-\d{8}-\d{6}\.mp4", Path(res.video_path).name)
    assert Path(res.thumb_path) == Path(res.video_path).with_suffix(".jpg")
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == sorted(
        [Path(res.video_path).name, Path(res.thumb_path).name])  # keine .part-/Platzhalter-Reste
    assert res.encoder == "libx264"
    work = tmp_path / "work" / "render_twitch_clip_AwkwardHelplessSalamanderSwiftRage"
    assert not (work / "cut.mkv").exists()  # große Zwischendateien aufgeräumt
    assert (work / "captions.ass").is_file()
    # Hook ist am Anfang sichtbar und nach hook_seconds weg
    early, late = _frame(Path(res.video_path), 1.0), _frame(Path(res.video_path), 4.5)
    hook_rows = slice(140, 330)
    diff = np.abs(early[hook_rows].astype(int) - late[hook_rows].astype(int)).mean()
    assert diff > 20


def test_render_vertical_native(settings, vertical_video, candidate, tmp_path):
    plan = _plan(trim_start=0.0, trim_end=4.0, hook_text="", zooms=[Zoom(start=1.0, end=2.5, scale=1.2)])
    analysis = _analysis(vertical_video, LayoutHint(kind="vertical_native"), None, w=720, h=1280, dur=4.0)
    res = renderer.render_clip(vertical_video, analysis, plan, candidate, settings, tmp_path / "work",
                               tmp_path / "out")
    _check_output(res, timeline.build_timemap(plan, 4.0))


def test_render_facecam_split(settings, sample_video, sample_transcript, candidate, tmp_path):
    hint = LayoutHint(kind="facecam_split", facecam_box=Box(x=0.0, y=0.0, w=0.3, h=0.35),
                      gameplay_box=Box(x=0.02, y=0.02, w=0.96, h=0.96), confidence=0.8)
    plan = _plan(trim_start=1.0, trim_end=6.0, zooms=[Zoom(start=2.0, end=4.0, scale=1.25, focus="face")],
                 emphasis=[Emphasis(t=2.5, emoji="😂", sfx="ding")], layout=None)
    res = renderer.render_clip(sample_video, _analysis(sample_video, hint, sample_transcript), plan, candidate,
                               settings, tmp_path / "work", tmp_path / "out")
    _check_output(res, timeline.build_timemap(plan, 8.0))
    # lila Trennlinie auf der Naht (color_accent #7B2FF7), Zeile 0.40 * 1920
    row = _frame(Path(res.video_path), 4.5)[768].astype(int)
    edges = np.concatenate([row[4:40], row[1042:1076]])  # Mitte ist von Captions verdeckt
    accent = np.array([0x7B, 0x2F, 0xF7])
    assert (np.abs(edges - accent).max(axis=1) < 40).mean() > 0.9


def test_render_source_without_audio(settings, candidate, tmp_path):
    src = make_video(tmp_path / "silent.mp4", seconds=3, size="640x360", audio=False)
    plan = _plan(trim_end=3.0, emphasis=[Emphasis(t=1.0, sfx="boom")])
    analysis = _analysis(src, LayoutHint(kind="gameplay_fit"), None, w=640, h=360, dur=3.0, audio=False)
    res = renderer.render_clip(src, analysis, plan, candidate, settings, tmp_path / "work", tmp_path / "out")
    _check_output(res, timeline.build_timemap(plan, 3.0), expect_lufs=None)


def test_render_nvenc_failure_falls_back_to_x264(settings, candidate, tmp_path, monkeypatch):
    src = make_video(tmp_path / "short.mp4", seconds=2, size="640x360")
    monkeypatch.setattr(encoder, "detect_encoder", lambda pref="auto": "h264_nvenc")
    calls: list[str] = []
    from wdvi.render import compositor

    real = compositor.compose_video

    def spy(*a, **kw):
        calls.append(a[-1])
        if a[-1] == "h264_nvenc":  # hier gibt es keine GPU -> simulieren, dass NVENC scheitert
            raise renderer.RenderError("Video-Encoding fehlgeschlagen (h264_nvenc): No NVENC capable devices")
        return real(*a, **kw)

    monkeypatch.setattr(compositor, "compose_video", spy)
    plan = _plan(trim_end=2.0, hook_text="")
    res = renderer.render_clip(src, _analysis(src, LayoutHint(kind="gameplay_fit"), None, 640, 360, 2.0), plan,
                               candidate, settings, tmp_path / "work", tmp_path / "out")
    assert calls == ["h264_nvenc", "libx264"]
    assert res.encoder == "libx264"


def test_render_missing_source_raises(settings, candidate, tmp_path):
    with pytest.raises(renderer.RenderError, match="Quellvideo fehlt"):
        renderer.render_clip(tmp_path / "nope.mp4", _analysis(tmp_path / "nope.mp4", LayoutHint(kind="gameplay_fit")),
                             _plan(), candidate, settings, tmp_path / "w", tmp_path / "o")


def test_render_broken_source_raises_render_error(settings, candidate, tmp_path):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not a video at all")
    with pytest.raises(renderer.RenderError):
        renderer.render_clip(bad, _analysis(bad, LayoutHint(kind="gameplay_fit")), _plan(), candidate, settings,
                             tmp_path / "w", tmp_path / "o")


def test_rerender_same_candidate_never_overwrites_previous_output(settings, candidate, tmp_path):
    """process-url auf einen schon gerenderten Kandidaten: das MP4/JPG des bestehenden (evtl. schon
    eingeplanten) Clips muss unverändert bleiben, der neue Render bekommt eigene Dateien."""
    src = make_video(tmp_path / "short.mp4", seconds=2, size="640x360")
    plan = _plan(trim_end=2.0, hook_text="")
    analysis = _analysis(src, LayoutHint(kind="gameplay_fit"), None, 640, 360, 2.0)
    first = renderer.render_clip(src, analysis, plan, candidate, settings, tmp_path / "work", tmp_path / "out")
    stamp = {p: Path(p).stat().st_mtime_ns for p in (first.video_path, first.thumb_path)}
    second = renderer.render_clip(src, analysis, plan, candidate, settings, tmp_path / "work", tmp_path / "out")
    assert second.video_path != first.video_path and second.thumb_path != first.thumb_path
    assert {p: Path(p).stat().st_mtime_ns for p in stamp} == stamp
    assert Path(second.video_path).stat().st_size > 0 and Path(second.thumb_path).is_file()


def test_failed_render_leaves_no_files_in_out_dir(settings, candidate, tmp_path, monkeypatch):
    """Scheitert ein Render nach dem Encoding (hier: Thumbnail), darf kein verwaistes MP4 in clips_dir
    zurückbleiben (eindeutige Namen würden sonst nie überschrieben/aufgeräumt)."""
    src = make_video(tmp_path / "short.mp4", seconds=2, size="640x360")

    def no_thumb(video, t, out):
        assert video.is_file() and video.stat().st_size > 0  # Video war schon fertig
        out.write_bytes(b"halb")
        out.with_name(out.stem + ".part.jpg").write_bytes(b"")
        raise renderer.RenderError("Thumbnail kaputt")

    monkeypatch.setattr(renderer, "_thumbnail", no_thumb)
    with pytest.raises(renderer.RenderError, match="Thumbnail kaputt"):
        renderer.render_clip(src, _analysis(src, LayoutHint(kind="gameplay_fit"), None, 640, 360, 2.0),
                             _plan(trim_end=2.0, hook_text=""), candidate, settings, tmp_path / "work",
                             tmp_path / "out")
    assert list((tmp_path / "out").iterdir()) == []


def test_unexpected_error_cleans_reserved_output(settings, candidate, tmp_path, monkeypatch):
    src = make_video(tmp_path / "short.mp4", seconds=2, size="640x360")
    from wdvi.render import compositor

    def boom(*a, **kw):
        raise ValueError("kaputt")

    monkeypatch.setattr(compositor, "compose_video", boom)
    with pytest.raises(renderer.RenderError, match="ValueError: kaputt"):
        renderer.render_clip(src, _analysis(src, LayoutHint(kind="gameplay_fit"), None, 640, 360, 2.0),
                             _plan(trim_end=2.0, hook_text=""), candidate, settings, tmp_path / "work",
                             tmp_path / "out")
    assert list((tmp_path / "out").iterdir()) == []


def test_reserve_output_is_unique(tmp_path, monkeypatch):
    from datetime import UTC, datetime

    class _Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 20, 12, 0, 5, tzinfo=UTC)

    monkeypatch.setattr(renderer, "datetime", _Fixed)
    a = renderer._reserve_output(tmp_path, "k")
    b = renderer._reserve_output(tmp_path, "k")
    assert (a.name, b.name) == ("k-20261020-120005.mp4", "k-20261020-120005-2.mp4")
    assert a.is_file() and b.is_file()  # Platzhalter reserviert den Namen atomar
    # ein übrig gebliebenes Thumbnail blockiert seinen Namen ebenfalls
    (tmp_path / "x-20261020-120005.jpg").write_bytes(b"x")
    assert renderer._reserve_output(tmp_path, "x").name == "x-20261020-120005-2.mp4"
