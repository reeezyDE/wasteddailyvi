"""Edit-Engine v2, Review-Fixes: Schnittstellen Renderer <-> Captions/Overlays, sauberer erster Frame,
Vorschaubild außerhalb von Übergängen."""

from __future__ import annotations

import numpy as np
import pytest

from wdvi.render import compositor as C
from wdvi.render import overlays, renderer, timeline
from wdvi.schemas import Annotation, Box, EditPlan, LayoutHint, PlatformText, WordColor


def _plan(**kw) -> EditPlan:
    pt = PlatformText(youtube="t", tiktok="t", instagram="t")
    base = dict(keep=True, virality_score=85, reasoning="r", trim_start=0.0, trim_end=8.0, hook_text="",
                titles=pt, descriptions=pt)
    base.update(kw)
    return EditPlan(**base)


def _frame(w: int = 1280, h: int = 720) -> np.ndarray:
    xs = np.linspace(0, 255, w, dtype=np.float32)[None, :]
    ys = np.linspace(0, 255, h, dtype=np.float32)[:, None]
    img = np.empty((h, w, 3), np.uint8)
    img[:, :, 0] = np.broadcast_to(xs, (h, w))
    img[:, :, 1] = np.broadcast_to(ys, (h, w))
    img[:, :, 2] = 128
    return img


def test_outro_card_gets_plan_word_colors(settings, candidate, tmp_path, monkeypatch):
    """Die Schlussfrage nutzt dieselben Bedeutungsfarben wie die Captions (PLAYSTATION blau, XBOX grün)."""
    settings.config.brand.show_credit = False
    settings.config.brand.show_watermark = False
    seen: dict = {}
    real = overlays.render_outro_png

    def spy(text, settings_, out, *, word_colors=None):
        seen["word_colors"] = word_colors
        return real(text, settings_, out, word_colors=word_colors)

    monkeypatch.setattr(renderer.overlays, "render_outro_png", spy)
    wc = [WordColor(word="PlayStation", color="blue"), WordColor(word="Xbox", color="green")]
    plan = _plan(outro_text="IS PLAYSTATION BETTER THAN XBOX?", word_colors=wc)
    tm = timeline.build_timemap(plan, 8.0)
    ovs = renderer._build_overlays(plan, candidate, settings, tm, "shots", tmp_path, 8.0)
    assert ovs and ovs[-1].png.name == renderer.OUTRO_PNG
    assert seen["word_colors"] == wc
    # die Karte enthält tatsächlich das Blau aus COLOR_HEX
    from PIL import Image

    with Image.open(ovs[-1].png) as im:
        px = np.asarray(im.convert("RGBA"))
    blue = np.array([0x2D, 0x9C, 0xFF])
    hit = (np.abs(px[:, :, :3].astype(int) - blue).sum(axis=2) < 40) & (px[:, :, 3] > 200)
    assert hit.sum() > 200


def test_annotation_colors_follow_brand(settings):
    """pink/orange sind Markenfarben (wie bei den Captions), nicht feste Hex-Werte."""
    brand = settings.config.brand
    brand.color_primary = "#00FF00"
    brand.color_secondary = "#0000FF"
    box = Box(x=0.1, y=0.1, w=0.2, h=0.2)
    plan = _plan(annotations=[Annotation(start=1.0, end=3.0, box=box, color="pink"),
                              Annotation(start=3.0, end=5.0, box=box, color="orange"),
                              Annotation(start=5.0, end=7.0, box=box, color="red")])
    tm = timeline.build_timemap(plan, 8.0)
    anns = renderer._map_annotations(plan, tm, tm.duration, brand)
    assert [a.color.upper() for a in anns] == ["#00FF00", "#0000FF", "#FF3B3B"]


def test_shake_never_moves_first_frame(settings):
    """Frame 0 ist das Cover (TikTok/Shorts) und muss sauber sein – auch bei einem Shake ab Sekunde 0."""
    hint = LayoutHint(kind="gameplay_fit")
    img = _frame()
    comp = C.Composer(C.CompositionSpec(layout=hint, effects=[C.FxEvent(0.0, "shake"), C.FxEvent(1.0, "shake")]),
                      settings, 1280, 720, 30)
    ref = C.Composer(C.CompositionSpec(layout=hint), settings, 1280, 720, 30)
    assert np.array_equal(comp.frame(img, 0.0), ref.frame(img, 0.0))
    assert not np.array_equal(comp.frame(img, 1.05), ref.frame(img, 1.05))  # spätere Shakes bleiben


def test_thumbnail_not_inside_shot_transition():
    """Peak direkt am Anfang eines Shots mit flash/punch/glitch: Vorschaubild erst nach dem Übergang."""
    shots = [C.ShotOut(0.0, 2.0, "gameplay"), C.ShotOut(2.0, 4.0, "split", transition="flash"),
             C.ShotOut(4.0, 8.0, "face")]
    t = renderer._thumb_time([], 8.0, shots, peaks=[2.02])
    assert t == pytest.approx(2.0 + renderer.THUMB_AFTER_TRANSITION_S)
    # harter Schnitt: wie bisher kurz nach dem Peak
    shots[1].transition = "cut"
    assert renderer._thumb_time([], 8.0, shots, peaks=[2.02]) == pytest.approx(2.02 + renderer.THUMB_AFTER_PEAK_S)
