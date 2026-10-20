"""Edit-Engine v2: echter End-to-End-Render mit Shots, Annotation, Outro, Shake/Flash (ffmpeg, langsam)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
import numpy as np
import pytest
import respx

from wdvi.render import encoder, overlays, renderer, timeline
from wdvi.schemas import (
    Annotation,
    Box,
    EditPlan,
    Emphasis,
    FaceDet,
    FrameSample,
    LayoutHint,
    MediaAnalysis,
    MediaInfo,
    PlatformText,
    Shot,
    WordColor,
)

pytestmark = pytest.mark.slow

FACE_JPG = Path(__file__).parent / "media_fixtures" / "face.jpg"
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


def facecam_video(path: Path, seconds: float = 8.0) -> Path:
    """testsrc2-Gameplay (1280x720) mit Facecam (face.jpg bzw. Farbfläche) links unten bei (0, 450, 280x240)."""
    face_in = (["-loop", "1", "-i", str(FACE_JPG)] if FACE_JPG.is_file()
               else ["-f", "lavfi", "-i", "color=c=0xC89070:s=280x240"])
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
           f"testsrc2=size=1280x720:rate=30:duration={seconds}", *face_in, "-f", "lavfi", "-i",
           f"sine=frequency=330:sample_rate=48000:duration={seconds}",
           "-filter_complex", "[1:v]scale=280:240,setsar=1[f];[0:v][f]overlay=0:450:shortest=1[v]",
           "-map", "[v]", "-map", "2:a", "-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast",
           "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)]
    subprocess.run(cmd, check=True)
    return path


FACECAM_BOX = Box(x=0.0, y=450 / 720, w=280 / 1280, h=240 / 720)
# Gesicht in face.jpg (280x240) etwa bei x 85..195, y 35..175 -> Quellkoordinaten
FACE_BOX = Box(x=85 / 1280, y=(450 + 35) / 720, w=110 / 1280, h=140 / 720)


def v2_plan(**kw) -> EditPlan:
    pt = PlatformText(youtube="t", tiktok="t", instagram="t")
    base = dict(
        keep=True, virality_score=88, reasoning="r", trim_start=0.4, trim_end=7.6, remove_ranges=[(3.0, 3.4)],
        hook_text="", hook_style="none", highlight_words=["insane"],
        word_colors=[WordColor(word="jump", color="green"), WordColor(word="bro", color="blue")],
        shots=[Shot(start=0.4, end=2.0, kind="face"), Shot(start=2.0, end=3.8, kind="split", transition="flash"),
               Shot(start=3.8, end=5.4, kind="gameplay", focus=Box(x=0.5, y=0.3, w=0.2, h=0.2),
                    transition="punch"),
               Shot(start=5.4, end=7.6, kind="fit", zoom=1.1, transition="glitch")],
        annotations=[Annotation(start=4.0, end=5.3, kind="circle", box=Box(x=0.55, y=0.35, w=0.1, h=0.1),
                                color="red", label="LOOK")],
        emphasis=[Emphasis(t=2.5, shake=True, sfx="boom"), Emphasis(t=4.5, flash=True)],
        outro_text="WHO WINS THIS?", titles=pt, descriptions=pt)
    base.update(kw)
    return EditPlan(**base)


def _analysis(path: Path, layout: LayoutHint, transcript=None, faces: bool = True) -> MediaAnalysis:
    frames = [FrameSample(t=t, path="x.jpg", faces=[FaceDet(box=FACE_BOX, score=0.95)]) for t in (1.0, 4.0)]
    return MediaAnalysis(info=MediaInfo(path=str(path), duration=8.0, width=1280, height=720, fps=30, has_audio=True),
                         transcript=transcript, layout=layout, frames=frames if faces else [])


def _probe(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format",
                          str(path)], capture_output=True, check=True).stdout
    return json.loads(out)


def _frame(path: Path, t: float) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(1920, 1080, 3)


def _check(res, tm) -> None:
    video = Path(res.video_path)
    info = _probe(video)
    vs = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (vs["width"], vs["height"]) == (1080, 1920) == (res.width, res.height)
    assert vs["codec_name"] == "h264" and vs["pix_fmt"] == "yuv420p"
    assert any(s["codec_type"] == "audio" for s in info["streams"])
    assert float(info["format"]["duration"]) == pytest.approx(tm.duration, abs=0.15)
    assert Path(res.thumb_path).is_file()


def test_render_v2_shots_annotation_outro_on_sample_video(settings, sample_video, sample_transcript, candidate,
                                                          tmp_path, monkeypatch):
    plan = v2_plan()
    hint = LayoutHint(kind="facecam_split", facecam_box=Box(x=0.0, y=0.6, w=0.3, h=0.4),
                      gameplay_box=Box(x=0.02, y=0.02, w=0.96, h=0.96))
    seen: list[list[tuple[float, str]]] = []
    real_build = renderer.audio.build_audio

    def spy(cut, sfx_events, *a, **kw):
        seen.append(list(sfx_events))
        return real_build(cut, sfx_events, *a, **kw)

    monkeypatch.setattr(renderer.audio, "build_audio", spy)
    res = renderer.render_clip(sample_video, _analysis(sample_video, hint, sample_transcript, faces=False), plan,
                               candidate, settings, tmp_path / "work", tmp_path / "out")
    tm = timeline.build_timemap(plan, 8.0)
    assert tm.duration == pytest.approx(6.8)
    _check(res, tm)
    work = tmp_path / "work" / "render_twitch_clip_AwkwardHelplessSalamanderSwiftRage"
    assert (work / "captions.ass").is_file() and (work / "outro.png").is_file()
    assert not (work / "hook.png").exists()  # hook_style none
    assert any(n == renderer.OUTRO_SFX for _t, n in seen[0])  # Schlussfrage poppt mit Ton auf
    video = Path(res.video_path)
    # Outro steht am Ende im Bild (Bereich um y = 0.42 H unterscheidet sich vom Bild davor)
    end, before = _frame(video, tm.duration - 0.3), _frame(video, tm.duration - 2.6)
    band = slice(round(0.42 * 1920) - 60, round(0.42 * 1920) + 60)
    assert np.abs(end[band].astype(int) - before[band].astype(int)).mean() > 15


def test_render_v2_synthetic_facecam(settings, sample_transcript, candidate, tmp_path):
    src = facecam_video(tmp_path / "fc.mp4")
    hint = LayoutHint(kind="facecam_split", facecam_box=FACECAM_BOX, gameplay_box=Box(x=0.25, y=0.0, w=0.75, h=1.0))
    plan = v2_plan()
    res = renderer.render_clip(src, _analysis(src, hint, sample_transcript), plan, candidate, settings,
                               tmp_path / "work", tmp_path / "out")
    tm = timeline.build_timemap(plan, 8.0)
    _check(res, tm)
    first = _frame(Path(res.video_path), 0.0)
    # Frame 0: Facecam bildfüllend (kein Schwarz, kein Weißblitz)
    assert 40 < first.mean() < 230
