from __future__ import annotations

import random
import subprocess
from pathlib import Path

import httpx
import numpy as np
import pytest
import respx

from wdvi.media import faces
from wdvi.media.faces import FaceDetector, analyze_layout, layout_from_detections
from wdvi.schemas import Box, FaceDet, MediaInfo

LANDSCAPE = MediaInfo(path="x.mp4", duration=30.0, width=1920, height=1080, fps=30.0, has_audio=True)


def face(cx: float, cy: float, w: float, h: float | None = None, score: float = 0.9) -> FaceDet:
    h = h if h is not None else w * 1.6
    return FaceDet(box=Box(x=cx - w / 2, y=cy - h / 2, w=w, h=h), score=score)


def samples(n: int, fn) -> list[tuple[float, list[FaceDet]]]:
    return [(round((i + 0.5) * 30.0 / n, 3), fn(i)) for i in range(n)]


# --------------------------------------------------------------------------- reine Layout-Logik


def test_vertical_source_is_vertical_native():
    info = MediaInfo(path="v.mp4", duration=10, width=1080, height=1920, fps=30, has_audio=True)
    hint = layout_from_detections(samples(16, lambda i: [face(0.5, 0.4, 0.4)]), info)
    assert hint.kind == "vertical_native"
    assert hint.confidence == 1.0


def test_facecam_top_left_snaps_to_corner():
    rng = random.Random(1)

    def fn(i: int):
        if i % 8 in (0, 3, 5):  # 6 von 16 Samples: Streamer schaut weg / kein Treffer
            return []
        dets = [face(0.10 + rng.uniform(-0.01, 0.01), 0.12 + rng.uniform(-0.01, 0.01), 0.06, 0.10)]
        if i == 4:  # vereinzelter Fehltreffer im Gameplay
            dets.append(face(0.55, 0.6, 0.04))
        return dets

    hint = layout_from_detections(samples(16, fn), LANDSCAPE)
    assert hint.kind == "facecam_split"
    assert hint.confidence == pytest.approx(10 / 16, abs=0.001)
    cam = hint.facecam_box
    assert cam is not None
    assert cam.x == 0.0 and cam.y == 0.0  # an die Ecke geklemmt
    assert cam.w == pytest.approx(0.18, abs=0.01)  # 3 x Gesichtsbreite
    assert cam.h == pytest.approx(cam.w, abs=0.01)  # 16:9 im 16:9-Bild -> gleiche normierte Höhe
    gp = hint.gameplay_box
    assert gp is not None and gp.w > 0.9 and gp.h > 0.9
    assert 0.0 <= gp.x and gp.x + gp.w <= 1.0


def test_facecam_bottom_right_and_clamped_width():
    hint = layout_from_detections(samples(12, lambda i: [face(0.90, 0.90, 0.05, 0.08)] if i % 2 == 0 else []),
                                  LANDSCAPE)
    assert hint.kind == "facecam_split"
    assert hint.confidence == pytest.approx(0.5)
    cam = hint.facecam_box
    assert cam.w == pytest.approx(0.16, abs=0.001)  # Mindestbreite
    assert cam.x + cam.w == pytest.approx(1.0, abs=1e-3)
    assert cam.y + cam.h == pytest.approx(1.0, abs=1e-3)


def test_facecam_box_is_extended_to_edge_when_moving_would_cut_the_face():
    # Gesicht sitzt oben in der Webcam unten links: Verschieben nach unten würde den Kopf abschneiden
    hint = layout_from_detections(samples(8, lambda i: [face(0.11, 0.80, 0.09, 0.16)]), LANDSCAPE)
    cam = hint.facecam_box
    assert hint.kind == "facecam_split"
    assert cam.x == 0.0
    assert cam.y == pytest.approx(0.80 - 0.27 / 2, abs=1e-3)  # oben bleibt, wo er war
    assert cam.y + cam.h == pytest.approx(1.0, abs=1e-3)  # unten bis zum Rand verlängert
    assert cam.y <= 0.80 - 0.08  # Gesicht vollständig drin


def test_facecam_box_aspect_for_4_3_source():
    info = MediaInfo(path="x.mp4", duration=30, width=1440, height=1080, fps=30, has_audio=True)
    hint = layout_from_detections(samples(10, lambda i: [face(0.5, 0.5, 0.1, 0.12)]), info)
    assert hint.kind == "facecam_split"
    cam = hint.facecam_box
    # Pixel-Seitenverhältnis des Rahmens bleibt 16:9
    assert (cam.w * 1440) / (cam.h * 1080) == pytest.approx(16 / 9, rel=0.01)
    assert 0.0 <= cam.x and cam.x + cam.w <= 1.0 and 0.0 <= cam.y and cam.y + cam.h <= 1.0


def test_scattered_small_faces_are_gameplay():
    rng = random.Random(7)
    hint = layout_from_detections(
        samples(16, lambda i: [face(rng.uniform(0.1, 0.9), rng.uniform(0.1, 0.9), 0.04)] if i % 2 else []),
        LANDSCAPE)
    assert hint.kind == "gameplay_fit"
    assert 0.0 < hint.confidence <= 1.0


def test_no_faces_is_gameplay_with_full_confidence():
    hint = layout_from_detections(samples(16, lambda i: []), LANDSCAPE)
    assert hint.kind == "gameplay_fit"
    assert hint.confidence == 1.0
    assert layout_from_detections([], LANDSCAPE).kind == "gameplay_fit"


def test_large_face_is_face_track_with_outlier_removed():
    def fn(i: int):
        if i == 7:
            return [face(0.95, 0.2, 0.25)]  # Ausreißer (Fehltreffer am Rand)
        return [face(0.30 + i * 0.02, 0.40, 0.30)]

    hint = layout_from_detections(samples(16, fn), LANDSCAPE)
    assert hint.kind == "face_track"
    assert hint.confidence == pytest.approx(1.0)
    xs = [p.cx for p in hint.track]
    assert len(hint.track) == 15  # Ausreißer entfernt
    assert max(xs) < 0.7
    assert xs == sorted(xs)  # Bewegung von links nach rechts bleibt erhalten
    ts = [p.t for p in hint.track]
    assert ts == sorted(ts)


def test_large_face_in_minority_is_gameplay():
    hint = layout_from_detections(samples(16, lambda i: [face(0.5, 0.5, 0.3)] if i < 5 else []), LANDSCAPE)
    assert hint.kind == "gameplay_fit"


# --------------------------------------------------------------------------- Detektor


@pytest.fixture
def no_download(monkeypatch):
    monkeypatch.setattr(faces, "_download_failed_at", None)


def test_detector_falls_back_to_haar(settings):
    det = FaceDetector(settings, allow_download=False)
    assert det.backend == "haar"
    img = np.zeros((720, 1280, 3), dtype=np.uint8)
    assert det.detect(img) == []
    assert det.detect(None) == []
    assert det.detect(np.zeros((0, 0, 3), dtype=np.uint8)) == []


def test_lfs_pointer_download_is_rejected(settings, no_download):
    pointer = b"version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 232589\n"
    with respx.mock(assert_all_called=False) as router:
        router.get(faces.YUNET_URLS[0]).mock(return_value=httpx.Response(200, content=pointer))
        router.get(faces.YUNET_URLS[1]).mock(return_value=httpx.Response(404))
        det = FaceDetector(settings)
    assert det.backend == "haar"
    model_dir = settings.config.paths.cache_dir / "models"
    assert not (model_dir / faces.YUNET_FILE).exists()
    assert not (model_dir / (faces.YUNET_FILE + ".part")).exists()
    # Fehlschlag wird gemerkt -> kein erneuter Netzversuch im selben Prozess
    assert faces._download_failed_at is not None
    with respx.mock(assert_all_called=False) as router:
        route = router.get(faces.YUNET_URLS[0]).mock(return_value=httpx.Response(200, content=pointer))
        FaceDetector(settings)
        assert not route.called


def test_corrupt_downloaded_model_falls_back_and_is_deleted(settings, no_download):
    junk = b"\x00garbage" * 20000  # > 100 kB, aber kein ONNX
    with respx.mock(assert_all_called=False) as router:
        router.get(faces.YUNET_URLS[0]).mock(return_value=httpx.Response(200, content=junk))
        det = FaceDetector(settings)
    assert det.backend == "haar"
    assert not (settings.config.paths.cache_dir / "models" / faces.YUNET_FILE).exists()


def test_network_error_falls_back(settings, no_download):
    with respx.mock(assert_all_called=False) as router:
        router.get(faces.YUNET_URLS[0]).mock(side_effect=httpx.ConnectError("blocked"))
        router.get(faces.YUNET_URLS[1]).mock(side_effect=httpx.ConnectTimeout("timeout"))
        det = FaceDetector(settings)
    assert det.backend == "haar"


class FakeYuNet:
    def __init__(self, rows):
        self.rows = rows
        self.sizes: list[tuple[int, int]] = []

    def setInputSize(self, size):  # OpenCV-API-Name
        self.sizes.append(tuple(size))

    def detect(self, img):
        assert img.shape[1] == self.sizes[-1][0]
        return 1, (np.array(self.rows, dtype=np.float32) if self.rows else None)


def test_yunet_output_is_normalized_and_filtered(settings):
    det = FaceDetector(settings, allow_download=False)
    rows = [
        [64, 36, 64, 72] + [0] * 10 + [0.95],  # gutes Gesicht (10 % Breite)
        [300, 100, 8, 8] + [0] * 10 + [0.99],  # winzig (< 2.5 %) -> raus
        [400, 200, 80, 90] + [0] * 10 + [0.30],  # Score zu niedrig -> raus
        [600, 300, 80, 80] + [0] * 10 + [0.80],  # ragt über den Rand -> geklemmt
    ]
    det._yunet, det.backend = FakeYuNet(rows), "yunet"
    found = det.detect(np.zeros((720, 1280, 3), dtype=np.uint8))
    assert det._yunet.sizes[-1] == (640, 360)  # auf 640 px Breite verkleinert
    assert len(found) == 2
    best = found[0]
    assert best.score == pytest.approx(0.95)
    assert best.box.x == pytest.approx(0.1) and best.box.w == pytest.approx(0.1)
    assert best.box.y == pytest.approx(0.1) and best.box.h == pytest.approx(0.2)
    clamped = found[1].box
    assert clamped.x + clamped.w <= 1.0 + 1e-6
    assert clamped.w == pytest.approx(40 / 640, abs=1e-3)


def test_yunet_model_from_assets_is_preferred(settings, tmp_path: Path, monkeypatch):
    fake_model = tmp_path / "models" / faces.YUNET_FILE
    fake_model.parent.mkdir(parents=True)
    fake_model.write_bytes(b"x" * (faces.MIN_MODEL_BYTES + 1))
    assert faces.find_yunet_model(tmp_path, settings.config.paths.cache_dir) == fake_model
    created = {}

    class FakeYN:
        @staticmethod
        def create(model, config, size, *args):
            created["model"] = model
            return FakeYuNet([])

    monkeypatch.setattr(faces.cv2, "FaceDetectorYN", FakeYN)
    settings.config.paths.assets_dir = tmp_path
    det = FaceDetector(settings, allow_download=False)
    assert det.backend == "yunet"
    assert created["model"] == str(fake_model)
    assert det.detect(np.zeros((360, 640, 3), dtype=np.uint8)) == []


# --------------------------------------------------------------------------- Wrapper mit echten Frames


class ScriptedDetector:
    """Liefert eine kleine Facecam unten links in jedem Frame."""

    backend = "fake"

    def __init__(self):
        self.shapes: list[tuple[int, ...]] = []

    def detect(self, img):
        self.shapes.append(img.shape)
        return [face(0.08, 0.85, 0.05, 0.09)]


def test_analyze_layout_reads_frames(sample_video: Path):
    from wdvi.media.probe import probe

    info = probe(sample_video)
    det = ScriptedDetector()
    hint = analyze_layout(sample_video, info, det, samples=8)
    assert len(det.shapes) == 8
    assert all(s[:2] == (720, 1280) for s in det.shapes)
    assert hint.kind == "facecam_split"
    assert hint.facecam_box.x == 0.0


def test_analyze_layout_vertical_skips_detection(vertical_video: Path):
    from wdvi.media.probe import probe

    det = ScriptedDetector()
    hint = analyze_layout(vertical_video, probe(vertical_video), det)
    assert hint.kind == "vertical_native"
    assert det.shapes == []


def test_real_haar_on_testsrc_is_gameplay(settings, sample_video: Path):
    from wdvi.media.probe import probe

    hint = analyze_layout(sample_video, probe(sample_video), FaceDetector(settings, allow_download=False))
    assert hint.kind == "gameplay_fit"


# --------------------------------------------------------------------------- echte Erkennung (Haar) auf echtem Gesicht
# tests/media_fixtures/face.jpg: Ausschnitt aus dem gemeinfreien NASA-Foto "astronaut" (scikit-image-Testbild)

FACE_JPG = Path(__file__).parent / "media_fixtures" / "face.jpg"


def _compose(out: Path, overlay_filter: str, seconds: float = 6.0) -> Path:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    f"testsrc2=size=1280x720:rate=30:duration={seconds}", "-loop", "1", "-i", str(FACE_JPG),
                    "-filter_complex", overlay_filter, "-t", str(seconds), "-c:v", "libx264", "-preset",
                    "ultrafast", "-pix_fmt", "yuv420p", str(out)], check=True)
    return out


@pytest.mark.slow
def test_real_facecam_overlay_is_detected(settings, tmp_path: Path):
    from wdvi.media.probe import probe

    video = _compose(tmp_path / "facecam.mp4", "[1:v]scale=320:-2[cam];[0:v][cam]overlay=0:H-h:shortest=1")
    det = FaceDetector(settings, allow_download=False)
    hint = analyze_layout(video, probe(video), det, samples=8)
    assert hint.kind == "facecam_split", hint
    assert hint.confidence >= 0.75
    cam = hint.facecam_box
    assert cam.x == 0.0  # links angeklemmt
    assert cam.y > 0.5 and cam.y + cam.h <= 1.0  # untere Hälfte
    assert 0.2 <= cam.w <= 0.32


@pytest.mark.slow
def test_real_large_moving_face_is_tracked(settings, tmp_path: Path):
    from wdvi.media.probe import probe

    video = _compose(tmp_path / "irl.mp4",
                     "[1:v]scale=-2:800[cam];[0:v][cam]overlay=x='-100+t*60':y=-40:shortest=1")
    det = FaceDetector(settings, allow_download=False)
    hint = analyze_layout(video, probe(video), det, samples=8)
    assert hint.kind == "face_track", hint
    xs = [p.cx for p in hint.track]
    assert len(xs) >= 5
    assert xs[-1] - xs[0] > 0.15  # folgt der Bewegung nach rechts
    assert all(0.15 < x < 0.65 for x in xs)
