"""Edit-Engine v2 – Media: freigestellte Facecams, Gesichts-Track immer gefüllt, Download-Qualität."""

from __future__ import annotations

import logging
import random
from pathlib import Path

import pytest

from tests.test_media_download import FakeYDL, fake_ydl  # noqa: F401  (autouse-Fixture für yt-dlp)
from wdvi.media import download as dl
from wdvi.media.faces import cam_style, layout_decision, layout_from_detections
from wdvi.media.frames import frame_times
from wdvi.schemas import Box, CandidateIn, FaceDet, MediaInfo

LANDSCAPE = MediaInfo(path="x.mp4", duration=30.0, width=1920, height=1080, fps=30.0, has_audio=True)


def face(cx: float, cy: float, w: float, h: float, score: float = 0.9) -> FaceDet:
    return FaceDet(box=Box(x=cx - w / 2, y=cy - h / 2, w=w, h=h), score=score)


def samples(n: int, fn) -> list[tuple[float, list[FaceDet]]]:
    return [(round((i + 0.5) * 30.0 / n, 3), fn(i)) for i in range(n)]


# --------------------------------------------------------------------------- freigestellte Facecam


def _cutout(i: int, rng: random.Random) -> list[FaceDet]:
    # Greenscreen-Streamer unten links, Gesicht ~14 % der Bildbreite, bewegt sich etwas (lehnt sich vor/zurück)
    if i % 5 == 2:
        return []  # schaut weg
    return [face(0.17 + rng.uniform(-0.04, 0.04), 0.62 + rng.uniform(-0.03, 0.03), 0.14, 0.30)]


def test_cutout_cam_bottom_left_is_facecam_split_with_head_and_shoulders():
    rng = random.Random(3)
    per = samples(16, lambda i: _cutout(i, rng))
    hint, style = layout_decision(per, LANDSCAPE)
    assert style == "cutout"
    assert hint.kind == "facecam_split"
    assert hint.confidence >= 0.7
    cam = hint.facecam_box
    assert cam is not None
    # ~2,6x Gesichtsbreite, an den linken Rand gezogen
    assert cam.x == 0.0
    assert cam.w == pytest.approx(2.6 * 0.14, abs=0.06)
    # Kopf drin (etwas Platz über der Stirn), nach unten bis zum Bildrand verlängert (Schultern/Oberkörper)
    assert cam.y < 0.62 - 0.15 - 0.03
    assert cam.y > 0.2
    assert cam.y + cam.h == pytest.approx(1.0, abs=1e-3)
    assert 0.0 <= cam.x and cam.x + cam.w <= 1.0 and 0.0 <= cam.y and cam.y + cam.h <= 1.0
    # Track trotz Split: geglättete Mitten des Facecam-Gesichts für den 'face'-Shot
    assert len(hint.track) >= 10
    assert all(abs(p.cx - 0.17) < 0.06 and abs(p.cy - 0.62) < 0.05 for p in hint.track)
    assert [p.t for p in hint.track] == sorted(p.t for p in hint.track)
    assert layout_from_detections(per, LANDSCAPE) == hint


def test_cutout_cam_right_side_snaps_right_and_keeps_face_inside():
    per = samples(10, lambda i: [face(0.86, 0.55, 0.16, 0.32)])
    hint = layout_from_detections(per, LANDSCAPE)
    cam = hint.facecam_box
    assert hint.kind == "facecam_split"
    assert cam.x + cam.w == pytest.approx(1.0, abs=1e-3)
    assert cam.x <= 0.86 - 0.08  # Gesicht horizontal ganz drin
    assert cam.y <= 0.55 - 0.16 and cam.y + cam.h == pytest.approx(1.0, abs=1e-3)


def test_cutout_cam_high_in_frame_is_not_stretched_to_bottom():
    # Streamer sitzt oben (Gesicht oben links): Box wird oben angeklemmt, unten nur Schultern
    hint = layout_from_detections(samples(8, lambda i: [face(0.2, 0.2, 0.12, 0.24)]), LANDSCAPE)
    cam = hint.facecam_box
    assert hint.kind == "facecam_split"
    assert cam.y == 0.0
    assert cam.y + cam.h == pytest.approx(0.2 - 0.12 - 0.35 * 0.24 + 2.2 * 0.24, abs=0.09)
    assert cam.y + cam.h < 0.7


def test_small_webcam_face_keeps_frame_style():
    members = [face(0.1, 0.1, 0.06, 0.10)] * 4
    assert cam_style(members) == "frame"
    assert cam_style([face(0.2, 0.6, 0.14, 0.30)]) == "cutout"
    assert cam_style([]) == "none"
    hint, style = layout_decision(samples(8, lambda i: [face(0.1, 0.12, 0.06, 0.10)]), LANDSCAPE)
    assert style == "frame" and hint.kind == "facecam_split"
    assert hint.facecam_box.w == pytest.approx(0.18, abs=0.01)  # unverändert: 3x Gesichtsbreite, 16:9
    assert len(hint.track) == 8


# --------------------------------------------------------------------------- Track immer gefüllt


def test_face_track_layout_still_has_track_and_no_box():
    hint = layout_from_detections(samples(12, lambda i: [face(0.3 + i * 0.02, 0.4, 0.3, 0.5)]), LANDSCAPE)
    assert hint.kind == "face_track"
    assert hint.facecam_box is None and len(hint.track) == 12


def test_minority_faces_still_fill_track_in_gameplay_fit():
    hint = layout_from_detections(samples(16, lambda i: [face(0.5, 0.45, 0.3, 0.5)] if i < 5 else []),
                                  LANDSCAPE)
    assert hint.kind == "gameplay_fit"
    assert len(hint.track) == 5
    assert all(p.cx == pytest.approx(0.5) for p in hint.track)
    # kleine stabile Gesichter in der Minderheit ebenso
    hint = layout_from_detections(samples(16, lambda i: [face(0.8, 0.8, 0.05, 0.08)] if i < 4 else []),
                                  LANDSCAPE)
    assert hint.kind == "gameplay_fit" and len(hint.track) == 4


def test_single_stray_detection_gives_no_track():
    hint = layout_from_detections(samples(16, lambda i: [face(0.4, 0.4, 0.05, 0.08)] if i == 3 else []),
                                  LANDSCAPE)
    assert hint.kind == "gameplay_fit" and hint.track == []
    assert layout_from_detections(samples(16, lambda i: []), LANDSCAPE).track == []


def test_vertical_source_unchanged():
    info = MediaInfo(path="v.mp4", duration=10, width=1080, height=1920, fps=30, has_audio=True)
    hint, style = layout_decision(samples(8, lambda i: [face(0.5, 0.4, 0.3, 0.2)]), info)
    assert hint.kind == "vertical_native" and style == "none" and hint.track == []


# --------------------------------------------------------------------------- Standbilder für Claude


def test_frames_for_claude_include_loudness_peaks_within_budget():
    ts = frame_times(0.0, 20.0, 8, focus=[14.0, 4.0, 9.0])
    assert len(ts) == 8
    assert {14.0, 14.5, 4.0, 4.5} <= set(ts)
    assert all(0.0 < t < 20.0 for t in ts)


# --------------------------------------------------------------------------- Download-Qualität


@pytest.mark.parametrize("info, expected", [
    ({"width": 640, "height": 360}, "Quelle nur 360p – Ergebnis wird unscharf"),
    ({"requested_formats": [{"vcodec": "avc1", "width": 854, "height": 480}, {"vcodec": "none"}]},
     "Quelle nur 480p – Ergebnis wird unscharf"),
    ({"width": 720, "height": 1280}, None),  # Hochkant-720p: kurze Kante zählt
    ({"width": 480, "height": 854}, "Quelle nur 480p – Ergebnis wird unscharf"),
    ({"width": 1920, "height": 1080}, None),
    ({"height": 720}, None),
    ({"format_note": "360p"}, "Quelle nur 360p – Ergebnis wird unscharf"),
    ({"resolution": "640x360"}, "Quelle nur 360p – Ergebnis wird unscharf"),
    ({}, None),
    (None, None),
    ({"height": "n/a"}, None),
])
def test_quality_note(info, expected):
    assert dl.quality_note(info) == expected


def test_download_prefers_best_quality_up_to_1080p(tmp_path: Path, settings, candidate: CandidateIn):
    dl.download(candidate, tmp_path / "dl", settings)
    o = FakeYDL.instances[0].opts
    assert o["format_sort"] == ["res:1080", "vcodec:h264", "acodec:aac"]
    assert "[height<=1080]" in o["format"] and "ext=mp4" not in o["format"]
    assert o["format"].endswith("/b")  # letzter Fallback: irgendein Format


def test_low_resolution_download_logs_german_warning(tmp_path: Path, settings, candidate: CandidateIn,
                                                     monkeypatch, caplog):
    orig = FakeYDL.extract_info

    def low_res(self, url: str, download: bool = True):
        info = orig(self, url, download)
        info.update(width=640, height=360)
        return info

    monkeypatch.setattr(FakeYDL, "extract_info", low_res)
    with caplog.at_level(logging.WARNING, logger="wdvi.media.download"):
        dl.download(candidate, tmp_path / "dl", settings)
    assert any("Quelle nur 360p – Ergebnis wird unscharf" in r.getMessage() for r in caplog.records)


def test_hd_download_has_no_quality_warning(tmp_path: Path, settings, candidate: CandidateIn, monkeypatch, caplog):
    orig = FakeYDL.extract_info

    def hd(self, url: str, download: bool = True):
        info = orig(self, url, download)
        info.update(width=1920, height=1080)
        return info

    monkeypatch.setattr(FakeYDL, "extract_info", hd)
    with caplog.at_level(logging.WARNING, logger="wdvi.media.download"):
        dl.download(candidate, tmp_path / "dl", settings)
    assert not any("unscharf" in r.getMessage() for r in caplog.records)
