"""Bildkomposition (Layouts, Zooms, Overlays) auf synthetischen Frames."""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from wdvi.render import compositor as C
from wdvi.schemas import Box, LayoutHint, TrackPoint


def _frame(w: int = 1920, h: int = 1080) -> np.ndarray:
    """Gradient-Testbild: x kodiert in Blau, y in Grün, Rot = 128."""
    xs = np.linspace(0, 255, w, dtype=np.float32)[None, :]
    ys = np.linspace(0, 255, h, dtype=np.float32)[:, None]
    img = np.empty((h, w, 3), np.uint8)
    img[:, :, 0] = np.broadcast_to(xs, (h, w))
    img[:, :, 1] = np.broadcast_to(ys, (h, w))
    img[:, :, 2] = 128
    return img


def _composer(settings, hint, zooms=None, overlays=None, src=(1920, 1080), fps=30):
    spec = C.CompositionSpec(layout=hint, zooms=zooms or [], overlays=overlays or [])
    return C.Composer(spec, settings, src[0], src[1], fps)


# --------------------------------------------------------------------------- Mathe


def test_zoom_value_ramps():
    z = C.ZoomOut(start=1.0, end=3.0, scale=1.3, focus="center")
    assert C.zoom_value(z, 0.5) == 1.0 and C.zoom_value(z, 3.5) == 1.0
    assert 1.0 < C.zoom_value(z, 1.1) < 1.15  # mitten in der Rampe
    assert C.zoom_value(z, 1.25) == pytest.approx(1.3 * (1 + C.ZOOM_DRIFT * 0.125), abs=1e-3)
    assert C.zoom_value(z, 2.0) == pytest.approx(1.3 * (1 + C.ZOOM_DRIFT * 0.5), abs=1e-3)
    vals = [C.zoom_value(z, 1.0 + i / 100) for i in range(26)]
    assert all(b >= a for a, b in zip(vals, vals[1:], strict=False))  # monoton steigend
    short = C.ZoomOut(start=0.0, end=0.2, scale=1.5, focus="center")  # kürzer als 2 Rampen
    assert C.zoom_value(short, 0.1) > 1.4


def test_cover_rect():
    assert C.cover_rect(0, 0, 1920, 1080, 9 / 16) == pytest.approx((656.25, 0, 607.5, 1080))
    x, y, w, h = C.cover_rect(0, 0, 1920, 1080, 9 / 16, cx=50)  # an den Rand geklemmt
    assert x == 0 and w == pytest.approx(607.5)
    assert C.cover_rect(100, 100, 400, 400, 2.0) == pytest.approx((100, 200, 400, 200))


# --------------------------------------------------------------------------- Layouts


def test_gameplay_fit_layout(settings):
    comp = _composer(settings, LayoutHint(kind="gameplay_fit"))
    out = comp.frame(_frame(), 0.0)
    assert out.shape == (1920, 1080, 3) and out.dtype == np.uint8
    fg_x, fg_y, fg_w, fg_h = comp.panels[0].dst
    assert fg_w == 1080 and fg_h == pytest.approx(1080 * 1.15 * 9 / 16, abs=2)
    assert fg_y == (1920 - fg_h) // 2
    # Hintergrund abgedunkelt, Vordergrund volle Helligkeit (Rot-Kanal = 128)
    assert out[50, 540, 2] < 80
    assert abs(int(out[960, 540, 2]) - 128) <= 2
    # Vordergrund ist 1.15x gezoomt: Bildmitte bleibt Bildmitte
    assert abs(int(out[960, 540, 0]) - 128) <= 3


def test_facecam_split_layout_and_divider(settings):
    hint = LayoutHint(kind="facecam_split", facecam_box=Box(x=0.0, y=0.6, w=0.25, h=0.4),
                      gameplay_box=Box(x=0.02, y=0.02, w=0.96, h=0.96))
    comp = _composer(settings, hint)
    out = comp.frame(_frame(), 0.0)
    seam = round(0.40 * 1920)
    accent = np.array(C._hex_bgr(settings.config.brand.color_accent), np.uint8)
    assert (out[seam, 10:1070] == accent).all()
    # Facecam-Panel zeigt die linke untere Ecke der Quelle (Blau klein, Grün groß)
    top = out[: seam - 4]
    assert top[:, :, 0].mean() < 50 and top[:, :, 1].mean() > 150
    # Gameplay-Panel zeigt die Bildmitte
    assert abs(int(out[seam + 576, 540, 0]) - 128) <= 3


def test_facecam_split_without_box_falls_back(settings):
    comp = _composer(settings, LayoutHint(kind="facecam_split"))
    assert comp.kind == "gameplay_fit"


def test_vertical_native_cover_and_square_fit(settings):
    comp = _composer(settings, LayoutHint(kind="vertical_native"), src=(720, 1280))
    assert comp.kind == "vertical_native"
    assert comp.frame(_frame(720, 1280), 0.0).shape == (1920, 1080, 3)
    sq = _composer(settings, LayoutHint(kind="vertical_native"), src=(1080, 1080))
    assert sq.kind == "gameplay_fit" and sq.panels[0].dst[3] == 1080  # nichts abgeschnitten


def test_face_track_is_smooth_and_converges(settings):
    hint = LayoutHint(kind="face_track", track=[TrackPoint(t=0.0, cx=0.2, cy=0.5), TrackPoint(t=1.0, cx=0.8, cy=0.5),
                                                TrackPoint(t=6.0, cx=0.8, cy=0.5)])
    comp = _composer(settings, hint)
    xs = []
    for i in range(150):
        win, _ = comp._window(i / 30)
        xs.append(win[0])
    steps = np.abs(np.diff(xs))
    assert steps.max() < 40  # keine Sprünge (px pro Frame in der Quelle)
    assert xs[0] == pytest.approx(0.2 * 1920 - 607.5 / 2, abs=1)
    assert xs[-1] == pytest.approx(0.8 * 1920 - 607.5 / 2, abs=15)  # eingeschwungen
    assert max(xs) <= 1920 - 607.5 + 1e-6  # nie über den Rand
    out = comp.frame(_frame(), 0.0)
    assert out.shape == (1920, 1080, 3)


def test_face_track_deadzone_ignores_jitter(settings):
    pts = [TrackPoint(t=i * 0.2, cx=0.5 + (0.004 if i % 2 else -0.004), cy=0.5) for i in range(30)]
    comp = _composer(settings, LayoutHint(kind="face_track", track=pts))
    xs = [comp._window(i / 30)[0][0] for i in range(150)]
    assert max(xs) - min(xs) < 1.0


# --------------------------------------------------------------------------- Zooms


def test_zoom_on_gameplay_panel_only(settings):
    hint = LayoutHint(kind="facecam_split", facecam_box=Box(x=0.0, y=0.6, w=0.25, h=0.4),
                      gameplay_box=Box(x=0.0, y=0.0, w=1.0, h=1.0))
    zoom = C.ZoomOut(start=1.0, end=3.0, scale=1.5, focus="gameplay")
    comp = _composer(settings, hint, zooms=[zoom])
    a = comp.frame(_frame(), 0.0)
    b = comp.frame(_frame(), 2.0)
    seam = round(0.40 * 1920)
    assert np.array_equal(a[: seam - 4], b[: seam - 4])  # Facecam unverändert
    # Gameplay: Gradient flacher -> Spannweite von Blau über die Breite kleiner
    span_a = int(a[seam + 300, -1, 0]) - int(a[seam + 300, 0, 0])
    span_b = int(b[seam + 300, -1, 0]) - int(b[seam + 300, 0, 0])
    assert span_b < span_a / 1.4


def test_center_zoom_whole_canvas_gameplay_fit(settings):
    comp = _composer(settings, LayoutHint(kind="gameplay_fit"),
                     zooms=[C.ZoomOut(start=0.0, end=2.0, scale=1.3, focus="center")])
    a = comp.frame(_frame(), 0.0)
    b = comp.frame(_frame(), 1.0)
    assert not np.array_equal(a, b)
    assert abs(int(b[960, 540, 0]) - int(a[960, 540, 0])) <= 3  # Zoom um die Bildmitte


def test_zoom_start_has_no_jump(settings):
    """Erster Zoom-Frame (z ~ 1.0001) darf sich kaum vom statischen Frame unterscheiden."""
    comp = _composer(settings, LayoutHint(kind="vertical_native"), src=(1080, 1920),
                     zooms=[C.ZoomOut(start=1.0, end=3.0, scale=1.2, focus="center")])
    img = _frame(1080, 1920)
    a = comp.frame(img, 0.9).astype(int)
    b = comp.frame(img, 1.0005).astype(int)
    assert np.abs(a - b).mean() < 1.0


# --------------------------------------------------------------------------- Overlays


def _png(path, w, h, rgba):
    Image.new("RGBA", (w, h), rgba).save(path)
    return path


def test_overlay_blend_timing_and_pop(settings, tmp_path):
    red = _png(tmp_path / "red.png", 100, 60, (255, 0, 0, 255))
    half = _png(tmp_path / "half.png", 40, 40, (0, 0, 255, 128))
    ovs = [C.TimedOverlay(png=red, start=1.0, end=2.0, x=100, y=200, pop=True, fade_out=0.2),
           C.TimedOverlay(png=half, start=0.0, end=9.0, x=500, y=500, pop=False, fade_out=0.0)]
    comp = _composer(settings, LayoutHint(kind="gameplay_fit"), overlays=ovs)
    img = _frame()
    before = comp.frame(img, 0.5)
    assert tuple(before[230, 150]) != (0, 0, 255)
    popping = comp.frame(img, 1.0)  # Pop: kleiner + teiltransparent
    full = comp.frame(img, 1.5)
    assert tuple(full[230, 150]) == (0, 0, 255)  # BGR rot, deckend
    assert tuple(full[201, 101]) == (0, 0, 255)  # volle Größe nach dem Pop
    assert tuple(popping[201, 101]) != (0, 0, 255)  # während des Pops noch kleiner
    fading = comp.frame(img, 1.9)
    assert 0 < fading[230, 150, 2] < 255
    after = comp.frame(img, 2.0)
    assert tuple(after[230, 150]) != (0, 0, 255)
    # 50 % Alpha blau (RGBA 0,0,255,128) über dem Hintergrund: B-Kanal halb Richtung 255, R halbiert
    bg = _composer(settings, LayoutHint(kind="gameplay_fit")).frame(img, 1.5)[520, 520].astype(float)
    a = 128 / 255
    assert abs(int(full[520, 520, 0]) - (bg[0] * (1 - a) + 255 * a)) <= 1.5
    assert abs(int(full[520, 520, 2]) - bg[2] * (1 - a)) <= 1.5


def test_overlay_at_frame_zero_is_opaque(settings, tmp_path):
    red = _png(tmp_path / "red.png", 200, 100, (255, 0, 0, 255))
    comp = _composer(settings, LayoutHint(kind="gameplay_fit"),
                     overlays=[C.TimedOverlay(png=red, start=0.0, end=3.0, x=100, y=100, pop=True)])
    out = comp.frame(_frame(), 0.0)
    assert tuple(out[150, 200]) == (0, 0, 255)


def test_overlay_partly_offscreen_and_missing_file(settings, tmp_path):
    red = _png(tmp_path / "red.png", 300, 300, (255, 0, 0, 255))
    ovs = [C.TimedOverlay(png=red, start=0.0, end=3.0, x=-150, y=1800, pop=False),
           C.TimedOverlay(png=tmp_path / "missing.png", start=0.0, end=3.0, x=0, y=0)]
    comp = _composer(settings, LayoutHint(kind="gameplay_fit"), overlays=ovs)
    out = comp.frame(_frame(), 0.5)
    assert tuple(out[1900, 10]) == (0, 0, 255)
    assert len(comp.overlays) == 1


def test_compositor_speed(settings, tmp_path):
    """Grobe Leistungsgrenze (Ziel < 30 ms/Frame bei 1080x1920 auf CPU; hier großzügig)."""
    import time

    hook = _png(tmp_path / "hook.png", 1000, 280, (255, 80, 0, 230))
    comp = _composer(settings, LayoutHint(kind="gameplay_fit"),
                     zooms=[C.ZoomOut(start=0.0, end=5.0, scale=1.2, focus="center")],
                     overlays=[C.TimedOverlay(png=hook, start=0.0, end=9.0, x=40, y=120)])
    img = _frame()
    comp.frame(img, 0.0)
    t = time.perf_counter()
    for i in range(20):
        comp.frame(img, 1.0 + i / 30)
    assert (time.perf_counter() - t) / 20 < 0.08


# --------------------------------------------------------------------------- compose_video (ffmpeg-Pipes)


def _small_inputs(tmp_path, seconds=1.0):
    import subprocess

    cut = tmp_path / "cut.mkv"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    f"testsrc2=size=640x360:rate=30:duration={seconds}", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", str(cut)], check=True)
    wav = tmp_path / "a.wav"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    f"sine=frequency=300:sample_rate=48000:duration={seconds}", "-ac", "2", str(wav)], check=True)
    return cut, wav


def test_compose_video_small(settings, tmp_path):
    import json
    import subprocess

    settings.config.render.width, settings.config.render.height = 270, 480
    cut, wav = _small_inputs(tmp_path)
    out = C.compose_video(cut, C.CompositionSpec(layout=LayoutHint(kind="gameplay_fit")), settings, None, wav,
                          tmp_path / "out" / "o.mp4", "libx264")
    info = json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(out)],
                                     capture_output=True, check=True).stdout)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (v["width"], v["height"]) == (270, 480) and int(v["nb_frames"]) == 30
    assert v.get("color_space") == "bt709"
    assert any(s["codec_type"] == "audio" for s in info["streams"])
    assert not (tmp_path / "out" / "o.part.mp4").exists()


def test_compose_video_encoder_failure_has_german_hint(settings, tmp_path, monkeypatch):
    from wdvi.render import encoder

    settings.config.render.width, settings.config.render.height = 270, 480
    cut, wav = _small_inputs(tmp_path)
    monkeypatch.setattr(encoder, "video_codec_args", lambda enc, cfg: ["-c:v", "does_not_exist_264"])
    with pytest.raises(C.RenderError, match="Video-Encoding fehlgeschlagen.*Encoder fehlt"):
        C.compose_video(cut, C.CompositionSpec(layout=LayoutHint(kind="gameplay_fit")), settings, None, wav,
                        tmp_path / "o.mp4", "libx264")
    assert not (tmp_path / "o.mp4").exists()


def test_compose_video_error_mid_render_cleans_up(settings, tmp_path, monkeypatch):
    import threading
    import time

    settings.config.render.width, settings.config.render.height = 270, 480
    cut, wav = _small_inputs(tmp_path, seconds=2.0)
    real = C.Composer.frame
    calls = {"n": 0}

    def flaky(self, img, t, out=None):
        calls["n"] += 1
        if calls["n"] == 5:
            raise ValueError("kaputt")
        return real(self, img, t, out)

    monkeypatch.setattr(C.Composer, "frame", flaky)
    before = threading.active_count()
    with pytest.raises(C.RenderError, match="Komposition fehlgeschlagen bei Frame 4"):
        C.compose_video(cut, C.CompositionSpec(layout=LayoutHint(kind="gameplay_fit")), settings, None, wav,
                        tmp_path / "o.mp4", "libx264")
    for _ in range(50):
        if threading.active_count() <= before:
            break
        time.sleep(0.1)
    assert threading.active_count() <= before
    assert not (tmp_path / "o.mp4").exists() and not (tmp_path / "o.part.mp4").exists()


def _wait_threads(before: int) -> bool:
    import threading
    import time

    for _ in range(80):
        if threading.active_count() <= before:
            return True
        time.sleep(0.1)
    return False


def test_compose_video_hung_encoder_is_aborted(settings, tmp_path, monkeypatch):
    """Encoder lebt, nimmt aber nichts an (z. B. NVENC-Treiber hängt) -> RenderError statt ewig blockieren."""
    import threading
    import time

    settings.config.render.width, settings.config.render.height = 270, 480
    cut, wav = _small_inputs(tmp_path, seconds=2.0)
    monkeypatch.setattr(C, "STALL_TIMEOUT_S", 1.0)
    procs = []

    class HungWriter(C._Writer):
        def __init__(self, cmd, cwd):
            super().__init__(["sleep", "60"], cwd)  # liest stdin nie
            procs.append(self.proc)

    monkeypatch.setattr(C, "_Writer", HungWriter)
    before = threading.active_count()
    t0 = time.monotonic()
    with pytest.raises(C.RenderError, match="Video-Encoding seit 1 s ohne Fortschritt"):
        C.compose_video(cut, C.CompositionSpec(layout=LayoutHint(kind="gameplay_fit")), settings, None, wav,
                        tmp_path / "o.mp4", "h264_nvenc")
    assert time.monotonic() - t0 < 20
    assert procs and procs[0].poll() is not None  # hängender Prozess wurde beendet
    assert _wait_threads(before)
    assert not (tmp_path / "o.mp4").exists() and not (tmp_path / "o.part.mp4").exists()


def test_compose_video_hung_decoder_is_aborted(settings, tmp_path, monkeypatch):
    import threading

    settings.config.render.width, settings.config.render.height = 270, 480
    cut, wav = _small_inputs(tmp_path)
    monkeypatch.setattr(C, "STALL_TIMEOUT_S", 1.0)
    monkeypatch.setattr(C._Reader, "_run", lambda self: self.stop.wait())  # liefert nie einen Frame
    before = threading.active_count()
    with pytest.raises(C.RenderError, match="Dekodieren von cut.mkv seit 1 s ohne Fortschritt"):
        C.compose_video(cut, C.CompositionSpec(layout=LayoutHint(kind="gameplay_fit")), settings, None, wav,
                        tmp_path / "o.mp4", "libx264")
    assert _wait_threads(before)
    assert not (tmp_path / "o.mp4").exists() and not (tmp_path / "o.part.mp4").exists()


def test_compose_video_overall_deadline(settings, tmp_path, monkeypatch):
    """Langsamer, aber nicht hängender Ablauf: die Gesamtfrist greift trotzdem."""
    import time

    settings.config.render.width, settings.config.render.height = 270, 480
    cut, wav = _small_inputs(tmp_path, seconds=2.0)
    monkeypatch.setattr(C, "MIN_DEADLINE_S", 0.5)
    monkeypatch.setattr(C, "DEADLINE_PER_S", 0.0)
    real = C.Composer.frame

    def slow(self, img, t, out=None):
        time.sleep(0.05)
        return real(self, img, t, out)

    monkeypatch.setattr(C.Composer, "frame", slow)
    with pytest.raises(C.RenderError, match="dauert länger als 0 s"):
        C.compose_video(cut, C.CompositionSpec(layout=LayoutHint(kind="gameplay_fit")), settings, None, wav,
                        tmp_path / "o.mp4", "libx264")
    assert not (tmp_path / "o.mp4").exists() and not (tmp_path / "o.part.mp4").exists()
