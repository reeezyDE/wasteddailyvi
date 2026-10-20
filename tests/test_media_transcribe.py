from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from tests.conftest import make_video
from wdvi.media import transcribe as tr
from wdvi.media.transcribe import TranscribeError, Transcriber


def W(word: str, start: float, end: float, p: float = 0.9):
    return SimpleNamespace(word=word, start=start, end=end, probability=p)


class FakeWhisper:
    def __init__(self, segments=None, language="en", prob=0.97, exc: Exception | None = None):
        self.segments = segments or []
        self.language, self.prob, self.exc = language, prob, exc
        self.calls: list[dict] = []

    def transcribe(self, audio, **kw):
        self.calls.append({"audio": audio, **kw})
        if self.exc:
            raise self.exc
        info = SimpleNamespace(language=kw.get("language") or self.language, language_probability=self.prob)
        return iter(self.segments), info


def seg(text: str, start: float, end: float, words):
    return SimpleNamespace(text=text, start=start, end=end, words=words)


@pytest.fixture(autouse=True)
def clear_model_cache(monkeypatch):
    monkeypatch.setattr(Transcriber, "_models", {})


@pytest.fixture
def clock(monkeypatch) -> list[float]:
    """Steuerbare Uhr nur für das transcribe-Modul (Abkühlphase nach CUDA-Fehlern)."""
    now = [1000.0]
    monkeypatch.setattr(tr, "time", SimpleNamespace(monotonic=lambda: now[0]))
    return now


def by_device(device_exc: dict[str, Exception | None], loads: list[str]):
    """_load_model-Ersatz: Fake-Modell, das seinen Device-Namen transkribiert bzw. die Exception wirft."""
    def fake_load(self, name, device, compute):
        loads.append(device)
        exc = device_exc.get(device)
        if isinstance(exc, OSError):
            raise exc  # Ladefehler
        return FakeWhisper([seg(device, 0.0, 0.5, [W(device, 0.0, 0.5)])], exc=exc)
    return fake_load


def test_transcribe_with_fake_model(settings, sample_video: Path):
    fake = FakeWhisper([
        seg(" No way, bro!", 0.5, 1.4, [W(" No", 0.5, 0.7, 0.95), W(" way,", 0.7, 1.0, 0.9), W(" bro!", 1.0, 1.4, 0.8)]),
        seg(" ", 2.0, 2.1, []),  # leeres Segment -> raus
        seg(" Look at this.", 3.0, 4.0, [W(" Look", 3.0, 3.3), W(" ", 3.3, 3.4), W(" at", 3.4, 3.5),
                                        W(" this.", 3.5, 3.4)]),
    ])
    t = Transcriber(settings, model=fake).transcribe(sample_video)
    call = fake.calls[0]
    assert isinstance(call["audio"], np.ndarray) and call["audio"].dtype == np.float32
    assert call["audio"].size == pytest.approx(8 * 16000, rel=0.02)
    assert call["word_timestamps"] is True and call["vad_filter"] is True
    assert call["beam_size"] == 5 and call["condition_on_previous_text"] is False
    assert call["language"] is None
    assert t.language == "en" and t.language_prob == pytest.approx(0.97)
    assert [s.text for s in t.segments] == ["No way, bro!", "Look at this."]
    words = t.words()
    assert [w.text for w in words] == ["No", "way,", "bro!", "Look", "at", "this."]  # Satzzeichen bleiben dran
    assert words[0].prob == pytest.approx(0.95)
    assert words[-1].end >= words[-1].start  # kaputte Zeitstempel geklemmt
    assert t.text == "No way, bro! Look at this."


def test_language_hint_is_passed(settings, sample_video: Path):
    fake = FakeWhisper()
    t = Transcriber(settings, model=fake).transcribe(sample_video, language="de")
    assert fake.calls[0]["language"] == "de"
    assert t.language == "de" and t.segments == []


def test_no_audio_returns_empty_transcript(settings, tmp_path: Path):
    silent = make_video(tmp_path / "silent.mp4", seconds=2, size="320x240", audio=False)
    fake = FakeWhisper()
    t = Transcriber(settings, model=fake).transcribe(silent)
    assert t.segments == [] and fake.calls == []


def test_hallucination_on_noise_is_dropped(settings, sample_video: Path):
    fake = FakeWhisper([
        seg("Thanks for watching!", 6.0, 7.0, [W("Thanks", 6.0, 6.3, 0.2), W("for", 6.3, 6.5, 0.3),
                                               W("watching!", 6.5, 7.0, 0.2)]),
        seg("thanks for watching guys", 1.0, 2.0, [W("thanks", 1.0, 1.2, 0.95), W("for", 1.2, 1.4, 0.95),
                                                   W("watching", 1.4, 1.7, 0.9), W("guys", 1.7, 2.0, 0.9)]),
    ])
    t = Transcriber(settings, model=fake).transcribe(sample_video)
    assert [s.text for s in t.segments] == ["thanks for watching guys"]


def test_device_and_compute_resolution(monkeypatch):
    assert tr.resolve_device("cpu") == "cpu"
    assert tr.resolve_device("cuda") == "cuda"
    import ctranslate2

    monkeypatch.setattr(ctranslate2, "get_cuda_device_count", lambda: 1)
    assert tr.resolve_device("auto") == "cuda"
    monkeypatch.setattr(ctranslate2, "get_cuda_device_count", lambda: 0)
    assert tr.resolve_device("auto") == "cpu"
    assert tr.resolve_compute_type("auto", "cuda") == "float16"
    assert tr.resolve_compute_type("auto", "cpu") == "int8"
    assert tr.resolve_compute_type("int8_float16", "cuda") == "int8_float16"


def test_model_is_loaded_lazily_once(settings, sample_video: Path, monkeypatch):
    loads: list[tuple] = []

    def fake_load(self, name, device, compute):
        loads.append((name, device, compute))
        return FakeWhisper()

    monkeypatch.setattr(Transcriber, "_load_model", fake_load)
    a, b = Transcriber(settings), Transcriber(settings)
    assert loads == []  # lazy
    a.transcribe(sample_video)
    b.transcribe(sample_video)
    assert loads == [("large-v3-turbo", "cpu", "int8")]


def test_cuda_failure_falls_back_to_cpu(settings, sample_video: Path, monkeypatch):
    settings.config.whisper.device = "cuda"
    loads: list[tuple] = []

    def fake_load(self, name, device, compute):
        loads.append((device, compute))
        if device == "cuda":
            return FakeWhisper(exc=RuntimeError("CUDA failed with error out of memory"))
        return FakeWhisper([seg("hi", 0.0, 0.5, [W("hi", 0.0, 0.5)])])

    monkeypatch.setattr(Transcriber, "_load_model", fake_load)
    t = Transcriber(settings)
    assert (t.device, t.compute_type) == ("cuda", "float16")
    out = t.transcribe(sample_video)
    assert out.text == "hi"
    assert loads == [("cuda", "float16"), ("cpu", "int8")]
    # das kaputte CUDA-Modell belegt kein VRAM mehr (nicht mehr im Prozess-Cache)
    assert list(Transcriber._models) == [("large-v3-turbo", "cpu", "int8")]


def test_cuda_oom_uses_cpu_only_for_cool_down(settings, sample_video: Path, monkeypatch, clock):
    settings.config.whisper.device = "cuda"
    loads: list[str] = []
    oom: dict[str, Exception | None] = {"cuda": RuntimeError("CUDA failed with error out of memory")}
    monkeypatch.setattr(Transcriber, "_load_model", by_device(oom, loads))
    t = Transcriber(settings)
    assert t.transcribe(sample_video).text == "cpu"
    assert list(Transcriber._models) == [("large-v3-turbo", "cpu", "int8")]
    clock[0] += tr.CUDA_RETRY_AFTER_S - 60  # noch in der Abkühlphase -> CPU, kein neuer CUDA-Versuch
    assert t.transcribe(sample_video).text == "cpu"
    assert loads == ["cuda", "cpu"]

    oom["cuda"] = None  # VRAM wieder frei
    clock[0] += 120
    assert t.transcribe(sample_video).text == "cuda"
    assert (t.device, t.compute_type) == ("cuda", "float16")
    assert loads == ["cuda", "cpu", "cuda"]
    # CPU-Kopie wird nicht zusätzlich im RAM gehalten
    assert list(Transcriber._models) == [("large-v3-turbo", "cuda", "float16")]


def test_cuda_oom_frees_torch_cache(settings, sample_video: Path, monkeypatch, clock):
    settings.config.whisper.device = "cuda"
    emptied: list[bool] = []
    fake_torch = SimpleNamespace(cuda=SimpleNamespace(is_initialized=lambda: True,
                                                      empty_cache=lambda: emptied.append(True)))
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setattr(Transcriber, "_load_model",
                        by_device({"cuda": RuntimeError("CUBLAS_STATUS_ALLOC_FAILED")}, []))
    assert Transcriber(settings).transcribe(sample_video).text == "cpu"
    assert emptied == [True]


def test_broken_cuda_runtime_falls_back_permanently(settings, sample_video: Path, monkeypatch, clock):
    settings.config.whisper.device = "cuda"
    loads: list[str] = []
    monkeypatch.setattr(Transcriber, "_load_model", by_device(
        {"cuda": RuntimeError("CUDA failed with error unspecified launch failure")}, loads))
    t = Transcriber(settings)
    assert t.transcribe(sample_video).text == "cpu"
    clock[0] += 10 * tr.CUDA_RETRY_AFTER_S
    assert t.transcribe(sample_video).text == "cpu"
    assert loads == ["cuda", "cpu"] and t.device == "cpu"
    assert list(Transcriber._models) == [("large-v3-turbo", "cpu", "int8")]


def test_cuda_load_oom_is_retried_later(settings, sample_video: Path, monkeypatch, clock):
    settings.config.whisper.device = "cuda"
    loads: list[str] = []
    fail: dict[str, Exception | None] = {"cuda": OSError("CUDA failed with error out of memory")}
    monkeypatch.setattr(Transcriber, "_load_model", by_device(fail, loads))
    t = Transcriber(settings)
    assert t.transcribe(sample_video).text == "cpu"
    fail["cuda"] = None
    clock[0] += tr.CUDA_RETRY_AFTER_S + 1
    assert t.transcribe(sample_video).text == "cuda"
    assert loads == ["cuda", "cpu", "cuda"]


def test_network_error_on_cuda_load_does_not_pin_cpu(settings, sample_video: Path, monkeypatch, clock):
    settings.config.whisper.device = "cuda"
    loads: list[str] = []
    down = OSError("connection refused")
    fail: dict[str, Exception | None] = {"cuda": down, "cpu": down}
    monkeypatch.setattr(Transcriber, "_load_model", by_device(fail, loads))
    t = Transcriber(settings)
    with pytest.raises(TranscribeError, match="konnte nicht geladen werden"):
        t.transcribe(sample_video)
    fail.clear()  # Netz wieder da
    clock[0] += tr.CUDA_RETRY_AFTER_S + 1
    assert t.transcribe(sample_video).text == "cuda"


def test_cuda_failure_kind():
    assert tr.cuda_failure_kind(RuntimeError("CUDA failed with error out of memory")) == "transient"
    assert tr.cuda_failure_kind(RuntimeError("CUBLAS_STATUS_ALLOC_FAILED")) == "transient"
    assert tr.cuda_failure_kind(RuntimeError("cudaErrorMemoryAllocation")) == "transient"
    assert tr.cuda_failure_kind(RuntimeError("Library libcublas.so.12 is not found")) == "permanent"
    assert tr.cuda_failure_kind(RuntimeError("CUDA driver version is insufficient")) == "permanent"
    assert tr.cuda_failure_kind(ValueError("boom")) is None


def test_cuda_load_failure_falls_back_to_cpu(settings, monkeypatch):
    settings.config.whisper.device = "cuda"

    def fake_load(self, name, device, compute):
        if device == "cuda":
            raise RuntimeError("Library libcublas.so.12 is not found")
        return FakeWhisper()

    monkeypatch.setattr(Transcriber, "_load_model", fake_load)
    t = Transcriber(settings)
    assert isinstance(t.model, FakeWhisper)
    assert t.device == "cpu"
    # fehlende CUDA-Bibliotheken: dauerhaft CPU, kein erneuter CUDA-Versuch
    t._maybe_retry_cuda()
    assert t._gpu is None and t.device == "cpu"


def test_load_failure_gives_german_error(settings, sample_video: Path, monkeypatch):
    def fake_load(self, name, device, compute):
        raise OSError("connection refused")

    monkeypatch.setattr(Transcriber, "_load_model", fake_load)
    with pytest.raises(TranscribeError, match="konnte nicht geladen werden") as ei:
        Transcriber(settings).transcribe(sample_video)
    assert ei.value.retryable is True  # Modell-Download/-Laden: später erneut versuchen


def test_runtime_error_is_wrapped(settings, sample_video: Path):
    t = Transcriber(settings, model=FakeWhisper(exc=ValueError("boom")))
    with pytest.raises(TranscribeError, match="fehlgeschlagen") as ei:
        t.transcribe(sample_video)
    assert ei.value.retryable is False
