"""Edit-Engine v2, Render-Kern: Multi-Shot, Übergänge, Effekte, Annotationen, Farbkorrektur, Outro."""

from __future__ import annotations

import numpy as np
import pytest

from wdvi.render import compositor as C
from wdvi.render import renderer, timeline
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
    TrackPoint,
)

FACECAM = Box(x=0.0, y=0.6, w=0.25, h=0.4)
SPLIT_HINT = LayoutHint(kind="facecam_split", facecam_box=FACECAM, gameplay_box=Box(x=0.3, y=0.0, w=0.7, h=1.0))


def _frame(w: int = 1920, h: int = 1080) -> np.ndarray:
    """Gradient-Testbild: x kodiert in Blau, y in Grün, Rot = 128."""
    xs = np.linspace(0, 255, w, dtype=np.float32)[None, :]
    ys = np.linspace(0, 255, h, dtype=np.float32)[:, None]
    img = np.empty((h, w, 3), np.uint8)
    img[:, :, 0] = np.broadcast_to(xs, (h, w))
    img[:, :, 1] = np.broadcast_to(ys, (h, w))
    img[:, :, 2] = 128
    return img


def _composer(settings, hint=SPLIT_HINT, src=(1920, 1080), **kw):
    spec = C.CompositionSpec(layout=hint, **kw)
    return C.Composer(spec, settings, src[0], src[1], 30)


FOUR = [C.ShotOut(0.0, 1.0, "face"), C.ShotOut(1.0, 2.0, "split", transition="flash"),
        C.ShotOut(2.0, 3.0, "gameplay", transition="punch"), C.ShotOut(3.0, 4.0, "fit", transition="glitch")]


# --------------------------------------------------------------------------- Shots: Zeitachse


def test_normalize_shots_fills_gaps_and_overlaps():
    shots = [C.ShotOut(3.0, 4.0, "fit"), C.ShotOut(0.5, 1.0, "face"), C.ShotOut(1.6, 3.5, "gameplay")]
    n = C.normalize_shots(shots, duration=5.0)
    assert [s.kind for s in n] == ["face", "gameplay", "fit"]
    assert n[0].start == 0.0 and n[-1].end == 5.0
    assert n[0].end == pytest.approx(1.3) and n[1].start == pytest.approx(1.3)  # Lücke halbiert
    assert n[1].end == 3.0  # Überlappung: späterer Shot gewinnt
    assert all(a.end == b.start for a, b in zip(n, n[1:], strict=False))
    assert shots[1].start == 0.5  # Eingabe unverändert
    assert C.normalize_shots([]) == []


def test_map_shots_through_timemap():
    pt = PlatformText(youtube="t", tiktok="t", instagram="t")
    plan = EditPlan(keep=True, virality_score=80, reasoning="r", trim_start=1.0, trim_end=9.0,
                    remove_ranges=[(4.0, 6.0)], hook_text="", titles=pt, descriptions=pt,
                    shots=[Shot(start=1.0, end=3.0, kind="face"), Shot(start=4.2, end=5.8, kind="split"),
                           Shot(start=3.0, end=7.0, kind="gameplay", zoom=3.0, transition="punch"),
                           Shot(start=7.0, end=9.0, kind="fit")])
    tm = timeline.build_timemap(plan, 10.0)
    shots = renderer._map_shots(plan, tm, tm.duration)
    assert [s.kind for s in shots] == ["face", "gameplay", "fit"]  # split lag komplett im Schnitt
    assert shots[0].start == 0.0 and shots[1].start == pytest.approx(2.0)
    assert shots[2].start == pytest.approx(4.0) and shots[2].end == pytest.approx(6.0)
    assert shots[1].zoom == 1.6 and shots[1].transition == "punch"


# --------------------------------------------------------------------------- Shots: Geometrie


def test_shot_switching_picks_geometry(settings):
    comp = _composer(settings, shots=FOUR, face_box=Box(x=0.08, y=0.7, w=0.06, h=0.1))
    assert comp.kind == "shots"
    kinds = [comp.run_at(t).geom.kind for t in (0.0, 0.5, 1.2, 2.5, 3.5, 99.0)]
    assert kinds == ["face", "face", "facecam_split", "gameplay", "gameplay_fit", "gameplay_fit"]
    img = _frame()
    face = comp.frame(img, 0.5)
    # face: ganzes Bild aus der Facecam (links unten: wenig Blau, viel Grün), 9:16
    assert face[:, :, 0].mean() < 40 and face[:, :, 1].mean() > 160
    fp = comp.run_at(0.5).geom.panels[0]
    assert fp.src[2] / fp.src[3] == pytest.approx(1080 / 1920, rel=1e-3)
    fx, fy, fw, fh = C._box_px(FACECAM, 1920, 1080)
    assert fx <= fp.src[0] and fp.src[0] + fp.src[2] <= fx + fw + 1e-6
    split = comp.frame(img, 1.5)
    seam = round(0.40 * 1920)
    accent = np.array(C._hex_bgr(settings.config.brand.color_accent), np.uint8)
    assert (split[seam, 10:1070] == accent).all()
    game = comp.frame(img, 2.5)
    # gameplay: 9:16 aus gameplay_box (rechts), Mitte = Mitte der Box -> Blau ~ 0.65 * 255
    assert abs(int(game[960, 540, 0]) - round(0.65 * 255)) <= 4
    fit = comp.frame(img, 3.5)
    assert fit[50, 540, 2] < 80 and abs(int(fit[960, 540, 2]) - 128) <= 2  # dunkler Rand, Bild in der Mitte
    for out in (face, split, game, fit):
        assert out.shape == (1920, 1080, 3) and out.dtype == np.uint8


def test_face_shot_fallbacks(settings):
    face = [C.ShotOut(0.0, 2.0, "face")]
    none = _composer(settings, LayoutHint(kind="gameplay_fit"), shots=face)
    assert none.run_at(0.5).geom.kind == "gameplay_fit"  # keine Gesichtsinfo -> fit
    split = _composer(settings, LayoutHint(kind="gameplay_fit"), shots=[C.ShotOut(0.0, 1.0, "split")])
    assert split.run_at(0.5).geom.kind == "gameplay_fit"  # split ohne Facecam -> fit
    vert = _composer(settings, LayoutHint(kind="vertical_native"), src=(720, 1280),
                     shots=[C.ShotOut(0.0, 1.0, "fit")])
    assert vert.run_at(0.5).geom.kind == "vertical_native"
    # nur Gesichtsbox (IRL ohne Track): Ausschnitt um das Gesicht, Kopf + Schultern
    irl = _composer(settings, LayoutHint(kind="face_track"), shots=face, face_box=Box(x=0.6, y=0.2, w=0.1, h=0.2))
    g = irl.run_at(0.5).geom
    sx, sy, sw, sh = g.panels[0].src
    assert g.kind == "face" and sh == pytest.approx(0.2 * 1080 * C.FACE_HEAD_SHOULDERS)
    assert sx < 0.65 * 1920 < sx + sw and sy < 0.3 * 1080 < sy + sh * 0.5  # Gesicht in der oberen Hälfte
    # mit Track: Fenster folgt dem Gesicht
    hint = LayoutHint(kind="face_track", track=[TrackPoint(t=0.0, cx=0.2, cy=0.5), TrackPoint(t=9.0, cx=0.2, cy=0.5)])
    tr = _composer(settings, hint, shots=face)
    assert tr.run_at(0.5).geom.tracked
    out = tr.frame(_frame(), 0.5)
    assert out.shape == (1920, 1080, 3) and out[:, :, 0].mean() < 0.35 * 255


def test_face_crop_math():
    bounds = (0.0, 0.0, 400.0, 300.0)
    r = C.face_crop(bounds, (180.0, 40.0, 40.0, 50.0), 9 / 16)
    assert r[2] / r[3] == pytest.approx(9 / 16)
    assert r[3] == pytest.approx(50 * C.FACE_HEAD_SHOULDERS)
    assert r[1] >= 0 and r[1] + r[3] <= 300 + 1e-6
    assert r[0] <= 200 <= r[0] + r[2]
    # Gesicht außerhalb -> zentrierter Cover-Ausschnitt
    assert C.face_crop(bounds, (900.0, 40.0, 40.0, 50.0), 9 / 16) == pytest.approx(C.cover_rect(*bounds, 9 / 16))


def test_shot_zoom_and_gameplay_focus(settings):
    focus = Box(x=0.8, y=0.1, w=0.1, h=0.1)
    comp = _composer(settings, shots=[C.ShotOut(0.0, 1.0, "gameplay", focus=focus),
                                      C.ShotOut(1.0, 2.0, "gameplay", focus=focus, zoom=1.4)])
    p = comp.run_at(0.5).geom.panels[0]
    assert p.src[0] <= 0.85 * 1920 <= p.src[0] + p.src[2]
    img = _frame()
    a, b = comp.frame(img, 0.5), comp.frame(img, 1.5)
    span_a = int(a[960, -1, 0]) - int(a[960, 0, 0])
    span_b = int(b[960, -1, 0]) - int(b[960, 0, 0])
    assert span_b < span_a / 1.3  # zusätzlicher Shot-Zoom


# --------------------------------------------------------------------------- Annotationen


def test_annotation_mapping_across_shot_kinds(settings):
    box = Box(x=0.6, y=0.45, w=0.1, h=0.1)  # im Gameplay, außerhalb der Facecam
    comp = _composer(settings, shots=FOUR, face_box=Box(x=0.08, y=0.7, w=0.06, h=0.1))
    assert comp.annotation_rect(box, 0.5) is None  # face-Shot: nicht sichtbar
    x0, y0, x1, y1 = comp.annotation_rect(box, 1.5)  # split: im Gameplay-Panel unter der Naht
    assert y0 > 0.40 * 1920 and 0 <= x0 < x1 <= 1080
    # gameplay: Quelle (0.3..1.0 breit, 9:16 zentriert) -> Box-Mitte rechts/links passend
    rx0, ry0, rx1, ry1 = comp.annotation_rect(box, 2.5)
    p = comp.run_at(2.5).geom.panels[0]
    k = 1080 / p.src[2]
    assert rx0 == pytest.approx((0.6 * 1920 - p.src[0]) * k) and ry0 == pytest.approx(0.45 * 1080 * k)
    assert (rx1 - rx0) == pytest.approx(0.1 * 1920 * k)
    # fit: ganzes Bild auf volle Breite, 1.15x
    fx0, fy0, fx1, fy1 = comp.annotation_rect(box, 3.5)
    fg = comp.run_at(3.5).geom.panels[0]
    assert fy0 > fg.dst[1] and fy1 < fg.dst[1] + fg.dst[3]
    assert (fx1 - fx0) == pytest.approx(0.1 * 1920 * 1080 / fg.src[2])
    # Box in der Facecam: im split im oberen Panel
    face_ann = comp.annotation_rect(Box(x=0.05, y=0.7, w=0.1, h=0.1), 1.5)
    assert face_ann is not None and face_ann[3] < 0.40 * 1920


def test_annotation_drawing_circle_box_arrow(settings):
    box = Box(x=0.45, y=0.45, w=0.1, h=0.08)
    anns = [C.AnnotationOut(0.0, 1.0, "circle", box, "#FF3B3B", "LOOK"),
            C.AnnotationOut(1.0, 2.0, "box", box, "#31E36B"),
            C.AnnotationOut(2.0, 3.0, "arrow", box, "#2D9CFF", "HERE")]
    comp = _composer(settings, LayoutHint(kind="gameplay_fit"), annotations=anns)
    ref = _composer(settings, LayoutHint(kind="gameplay_fit"))
    img = _frame()
    for t in (0.05, 0.5, 1.3, 2.5):
        a, b = comp.frame(img, t), ref.frame(img, t)
        assert a.shape == (1920, 1080, 3)
        rect = comp.annotation_rect(box, t)
        diff = np.abs(a.astype(int) - b.astype(int)).sum(axis=2) > 60
        assert diff.sum() > 500, t
        ys, xs = np.nonzero(diff)
        cy = (rect[1] + rect[3]) / 2
        assert abs(np.median(ys) - cy) < 400  # Markierung in der Nähe der Box
    # Kreis zeichnet sich selbst: früh weniger Pixel als voll gezeichnet
    early = (np.abs(comp.frame(img, 0.05).astype(int) - ref.frame(img, 0.05).astype(int)).sum(axis=2) > 60).sum()
    full = (np.abs(comp.frame(img, 0.5).astype(int) - ref.frame(img, 0.5).astype(int)).sum(axis=2) > 60).sum()
    assert early < full
    assert np.array_equal(comp.frame(img, 3.5), ref.frame(img, 3.5))  # danach weg


def test_annotation_follows_split_layout_seam(settings):
    comp = _composer(settings, shots=[C.ShotOut(0, 5, "split")],
                     annotations=[C.AnnotationOut(0.0, 5.0, "circle", Box(x=0.62, y=0.4, w=0.1, h=0.1))])
    out = comp.frame(_frame(), 1.0)
    ref = _composer(settings, shots=[C.ShotOut(0, 5, "split")]).frame(_frame(), 1.0)
    ys, _ = np.nonzero(np.abs(out.astype(int) - ref.astype(int)).sum(axis=2) > 60)
    assert ys.min() > 0.40 * 1920 - 60  # im Gameplay-Panel


# --------------------------------------------------------------------------- Übergänge, Effekte, Grade


def test_transitions_keep_size_and_change_frames(settings):
    comp = _composer(settings, shots=FOUR, face_box=Box(x=0.08, y=0.7, w=0.06, h=0.1))
    ref = _composer(settings, shots=[C.ShotOut(s.start, s.end, s.kind) for s in FOUR],
                    face_box=Box(x=0.08, y=0.7, w=0.06, h=0.1))
    img = _frame()
    for t in (1.0, 2.0, 3.0):  # flash, punch, glitch am Shot-Anfang
        a, b = comp.frame(img, t), ref.frame(img, t)
        assert a.shape == (1920, 1080, 3) and a.dtype == np.uint8
        assert np.abs(a.astype(int) - b.astype(int)).mean() > 1.5, t  # Gradient: Punch ändert wenig
    assert comp.frame(img, 1.0).mean() > ref.frame(img, 1.0).mean() + 40  # Weißblitz
    after = 1.0 + 1.6 * settings.config.render.transition_ms / 1000
    assert np.array_equal(comp.frame(img, after), ref.frame(img, after))  # abgeklungen
    # Frame 0 ist sauber, auch wenn der erste Shot einen Übergang hat
    first = _composer(settings, shots=[C.ShotOut(0.0, 1.0, "fit", transition="flash")])
    plain = _composer(settings, shots=[C.ShotOut(0.0, 1.0, "fit")])
    assert np.array_equal(first.frame(img, 0.0), plain.frame(img, 0.0))


def test_glitch_and_flash_helpers():
    img = _frame(400, 300)
    g = img.copy()
    C.glitch_frame(g, 1.0, seed=3)
    assert g.shape == img.shape and not np.array_equal(g, img)
    g2 = img.copy()
    C.glitch_frame(g2, 1.0, seed=3)
    assert np.array_equal(g, g2)  # deterministisch
    f = img.copy()
    C.flash_frame(f, 1.0)
    assert (f == 255).all()


def test_shake_and_flash_effects(settings):
    dx, dy, amp = C.shake_offset(0.05, [0.0])
    assert 0 < amp <= C.SHAKE_PX and abs(dx) <= C.SHAKE_PX and abs(dy) <= C.SHAKE_PX
    assert C.shake_offset(0.4, [0.0]) == (0.0, 0.0, 0.0)
    amps = [C.shake_offset(i / 30, [0.0])[2] for i in range(11)]
    assert all(b <= a for a, b in zip(amps, amps[1:], strict=False))  # klingt ab
    fx = [C.FxEvent(1.0, "shake"), C.FxEvent(2.0, "flash"), C.FxEvent(0.0, "flash")]
    comp = _composer(settings, LayoutHint(kind="gameplay_fit"), effects=fx)
    ref = _composer(settings, LayoutHint(kind="gameplay_fit"))
    img = _frame()
    assert not np.array_equal(comp.frame(img, 1.05), ref.frame(img, 1.05))
    assert comp.frame(img, 2.02).mean() > ref.frame(img, 2.02).mean() + 30
    assert np.array_equal(comp.frame(img, 0.0), ref.frame(img, 0.0))  # kein Blitz auf Frame 0
    assert np.array_equal(comp.frame(img, 1.5), ref.frame(img, 1.5))


def test_color_grade(settings):
    g = C.Grader(C.GradeSpec.from_render(settings.config.render))
    img = _frame(640, 360)
    out = g(img)
    assert out.shape == img.shape and out.dtype == np.uint8
    sat = lambda a: a.astype(int).max(axis=2) - a.astype(int).min(axis=2)  # noqa: E731
    assert sat(out).mean() > sat(img).mean()  # kräftiger
    gray = np.full((10, 10, 3), 128, np.uint8)
    o = g(gray)[0, 0].astype(int)
    assert o[2] > o[0]  # wärmer (Rot > Blau)
    assert tuple(g(np.zeros((2, 2, 3), np.uint8))[0, 0]) == (0, 0, 0)
    assert tuple(g(np.full((2, 2, 3), 255, np.uint8))[0, 0]) == (255, 255, 255)
    neutral = C.Grader(C.GradeSpec(saturation=1.0, contrast=1.0, warmth=0.0))
    assert np.array_equal(neutral(img), img)
    # im Composer: vor der Trennlinie (Akzentfarbe bleibt exakt)
    comp = _composer(settings, grade=C.GradeSpec.from_render(settings.config.render))
    out = comp.frame(_frame(), 0.0)
    accent = np.array(C._hex_bgr(settings.config.brand.color_accent), np.uint8)
    assert (out[round(0.40 * 1920), 10:1070] == accent).all()


def test_v1_spec_unchanged_without_v2_fields(settings):
    img = _frame()
    a = _composer(settings).frame(img, 0.5)
    b = _composer(settings, shots=[], annotations=[], effects=[], grade=None).frame(img, 0.5)
    assert np.array_equal(a, b)


def test_compositor_speed_multi_shot(settings):
    import time

    anns = [C.AnnotationOut(0.0, 9.0, "circle", Box(x=0.6, y=0.45, w=0.1, h=0.1), "#FF3B3B", "WOW")]
    comp = _composer(settings, shots=FOUR, annotations=anns, grade=C.GradeSpec(),
                     face_box=Box(x=0.08, y=0.7, w=0.06, h=0.1))
    img = _frame()
    comp.frame(img, 0.0)
    t = time.perf_counter()
    for i in range(40):
        comp.frame(img, i / 10)
    assert (time.perf_counter() - t) / 40 < 0.12


# --------------------------------------------------------------------------- Renderer-Helfer


def _plan(**kw) -> EditPlan:
    pt = PlatformText(youtube="t", tiktok="t", instagram="t")
    base = dict(keep=True, virality_score=85, reasoning="r", trim_start=0.0, trim_end=8.0, hook_text="",
                titles=pt, descriptions=pt)
    base.update(kw)
    return EditPlan(**base)


def test_map_annotations_and_effects():
    plan = _plan(remove_ranges=[(2.0, 3.0)],
                 annotations=[Annotation(start=1.0, end=4.0, box=Box(x=0.1, y=0.1, w=0.1, h=0.1), color="blue",
                                         label="  PS5 "),
                              Annotation(start=2.1, end=2.9, box=Box(x=0.1, y=0.1, w=0.1, h=0.1))],
                 emphasis=[Emphasis(t=1.5, shake=True, flash=True), Emphasis(t=2.5, shake=True),
                           Emphasis(t=5.0, emoji="💀")])
    tm = timeline.build_timemap(plan, 8.0)
    anns = renderer._map_annotations(plan, tm, tm.duration)
    assert len(anns) == 1
    a = anns[0]
    assert (a.start, a.end) == (pytest.approx(1.0), pytest.approx(3.0))
    assert a.color.upper() == "#2D9CFF" and a.label == "PS5"
    fx = renderer._map_effects(plan, tm)
    assert sorted((e.t, e.kind) for e in fx) == [(1.5, "flash"), (1.5, "shake")]


def test_face_box_median_inside_facecam():
    fc = Box(x=0.0, y=0.6, w=0.3, h=0.4)
    frames = [FrameSample(t=t, path="x.jpg", faces=[FaceDet(box=Box(x=0.1, y=0.7, w=0.05 + i * 0.01, h=0.1),
                                                              score=0.9),
                                                      FaceDet(box=Box(x=0.6, y=0.2, w=0.2, h=0.3), score=0.99)])
              for i, t in enumerate((1.0, 2.0, 3.0))]
    analysis = MediaAnalysis(info=MediaInfo(path="x", duration=8, width=1280, height=720, fps=30, has_audio=True),
                             frames=frames, layout=LayoutHint(kind="facecam_split", facecam_box=fc))
    b = renderer._face_box(analysis, _plan())
    assert b is not None and b.x == pytest.approx(0.1) and b.w == pytest.approx(0.06)
    analysis.layout = LayoutHint(kind="face_track")
    assert renderer._face_box(analysis, _plan()).x == pytest.approx(0.6)  # ohne Facecam: größtes Gesicht
    analysis.frames = []
    assert renderer._face_box(analysis, _plan()) is None


def test_build_overlays_hook_none_and_outro(settings, candidate, tmp_path):
    settings.config.brand.show_credit = False
    settings.config.brand.show_watermark = False
    tm = timeline.build_timemap(_plan(), 8.0)
    plan = _plan(hook_text="IS PS5 BETTER?", hook_style="none", outro_text="IS PLAYSTATION BETTER THAN XBOX?")
    ovs = renderer._build_overlays(plan, candidate, settings, tm, "shots", tmp_path, 8.0, first_shot="face")
    assert len(ovs) == 1  # kein Hook, nur Outro
    o = ovs[0]
    r = settings.config.render
    assert o.start == pytest.approx(8.0 - r.outro_seconds) and o.end > 8.0 and o.pop
    assert o.pop_from < C.POP_FROM
    asset = C._load_overlay(o)
    h = asset.alpha.shape[0]
    assert abs(asset.cy - renderer.OUTRO_Y_RATIO * r.height) < 2 or asset.cy + h / 2 < r.captions.y_ratio * r.height
    # Banner bleibt Standard
    ovs2 = renderer._build_overlays(_plan(hook_text="NO WAY"), candidate, settings, tm, "shots", tmp_path, 8.0)
    assert len(ovs2) == 1 and ovs2[0].start == 0.0
    # Outro weicht der Caption-Naht im Split-Layout aus
    ovs3 = renderer._build_overlays(_plan(outro_text="WHO WINS?"), candidate, settings, tm, "facecam_split",
                                    tmp_path, 8.0)
    a3 = C._load_overlay(ovs3[0])
    seam = r.split_facecam_ratio * r.height
    assert a3.cy + a3.alpha.shape[0] / 2 < seam - r.captions.font_size * 0.45


def test_outro_waits_for_last_word_and_sits_low_when_silent(settings, candidate, tmp_path):
    """Pikaboo-Fall: Schlussfrage verdeckte die Pointe ("He's dead!"). Jetzt startet sie erst nach dem letzten
    Wort (+ Caption-Nachlauf), und ist danach niemand mehr zu hören, sitzt sie auf der Caption-Zeile."""
    settings.config.brand.show_credit = False
    settings.config.brand.show_watermark = False
    r = settings.config.render
    tm = timeline.build_timemap(_plan(), 8.0)
    plan = _plan(outro_text="IS WARLOCK THE MOST BROKEN CLASS?")
    ovs = renderer._build_overlays(plan, candidate, settings, tm, "shots", tmp_path, 8.0, speech=[(5.0, 6.4)])
    o = ovs[-1]
    assert o.start == pytest.approx(6.4 + renderer.OUTRO_AFTER_SPEECH_S)
    a = C._load_overlay(o)
    assert a.cy == pytest.approx(r.captions.y_ratio * r.height, abs=2)  # stilles Ende -> Caption-Zeile
    # bis zum Ende gesprochen: mindestens OUTRO_MIN_S, Karte weicht den Captions aus (wie bisher)
    ovs = renderer._build_overlays(plan, candidate, settings, tm, "shots", tmp_path, 8.0, speech=[(5.0, 7.9)])
    o = ovs[-1]
    assert o.start == pytest.approx(8.0 - renderer.OUTRO_MIN_S)
    a = C._load_overlay(o)
    assert a.cy + a.alpha.shape[0] / 2 < r.captions.y_ratio * r.height
    # ohne Sprachinfo: altes Verhalten
    ovs = renderer._build_overlays(plan, candidate, settings, tm, "shots", tmp_path, 8.0)
    assert ovs[-1].start == pytest.approx(8.0 - r.outro_seconds)


def test_emoji_ends_at_next_shot_change(settings, candidate, tmp_path):
    settings.config.brand.show_credit = False
    settings.config.brand.show_watermark = False
    tm = timeline.build_timemap(_plan(), 8.0)
    plan = _plan(emphasis=[Emphasis(t=2.0, emoji="💀"), Emphasis(t=5.0, emoji="🔥")])
    ovs = renderer._build_overlays(plan, candidate, settings, tm, "shots", tmp_path, 8.0, cuts=[2.4, 5.1])
    emo = sorted((o for o in ovs if o.png.parent != tmp_path), key=lambda o: o.start)
    if len(emo) < 2:
        pytest.skip("Emoji-Font/-Cache nicht verfügbar")
    assert emo[0].end == pytest.approx(2.38)  # nicht in den nächsten Shot hinein
    assert emo[1].end == pytest.approx(5.0 + renderer.EMOJI_SECONDS)  # Schnitt zu nah -> volle Dauer


def test_thumbnail_prefers_loudest_peak_with_visible_face():
    shots = [C.ShotOut(0.0, 2.0, "face"), C.ShotOut(2.0, 4.0, "gameplay"), C.ShotOut(4.0, 6.0, "split"),
             C.ShotOut(6.0, 8.0, "face")]
    t = renderer._thumb_time([], 8.0, shots, peaks=[3.0, 4.5, 7.0])  # lautester liegt im Gameplay
    assert t == pytest.approx(4.5 + renderer.THUMB_AFTER_PEAK_S)
    assert renderer._thumb_time([], 8.0, shots, peaks=[3.0]) == pytest.approx(0.6)  # Fallback: erster face-Shot
    assert renderer._thumb_time([], 8.0, shots) == pytest.approx(0.6)
