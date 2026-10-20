"""Transkription mit faster-whisper (Wort-Zeitstempel). Modell wird lazy und nur einmal geladen."""

from __future__ import annotations

import logging
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, ClassVar

import numpy as np

from wdvi.config import Settings
from wdvi.media import _ff
from wdvi.schemas import Segment, Transcript, Word

log = logging.getLogger(__name__)

# Typische Whisper-Halluzinationen auf Stille/Gameplay-Geräusch (nur verworfen, wenn unsicher erkannt)
_HALLUCINATIONS = re.compile(
    r"^(thanks? (you )?for watching|thank you\.?$|please subscribe|subtitles? by|amara\.org|"
    r"untertitel (im auftrag|von))", re.IGNORECASE)
_CUDA_ERR = ("cuda", "cublas", "cudnn", "gpu", "out of memory")
# VRAM gerade voll (Spiel läuft, zweiter Prozess lädt ein Modell) -> nur vorübergehend auf die CPU
_CUDA_TRANSIENT = ("out of memory", "alloc_failed", "memoryallocation", "busy or unavailable")
CUDA_RETRY_AFTER_S = 30 * 60


class TranscribeError(RuntimeError):
    """Transkription fehlgeschlagen. `retryable`: vorübergehend (Whisper-Modell konnte nicht geladen/
    heruntergeladen werden) – die Pipeline versucht den Kandidaten später erneut."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable: bool = bool(retryable)


def resolve_device(device: str) -> str:
    """"auto" -> "cuda", wenn ctranslate2 eine CUDA-GPU sieht, sonst "cpu"."""
    if device != "auto":
        return device
    try:
        import ctranslate2

        return "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
    except Exception:  # kein CUDA-Treiber o. ä.
        return "cpu"


def resolve_compute_type(compute_type: str, device: str) -> str:
    if compute_type and compute_type != "auto":
        return compute_type
    return "float16" if device == "cuda" else "int8"


def cuda_failure_kind(e: BaseException) -> str | None:
    """"transient" (VRAM voll), "permanent" (Treiber/Bibliotheken/Gerät fehlen) oder None (kein CUDA-Fehler)."""
    msg = str(e).lower()
    if any(k in msg for k in _CUDA_TRANSIENT):
        return "transient"
    if any(k in msg for k in _CUDA_ERR):
        return "permanent"
    return None


def _free_torch_cuda_cache() -> None:
    """Gibt von torch (Musik-Erkennung) zwischengespeicherten VRAM frei – nur, wenn torch schon geladen ist."""
    torch = sys.modules.get("torch")
    try:
        if torch is not None and torch.cuda.is_initialized():
            torch.cuda.empty_cache()
    except Exception as e:
        log.debug("torch.cuda.empty_cache failed: %s", e)


class Transcriber:
    # Geladene Modelle, prozessweit: (modell, device, compute_type) -> WhisperModel
    _models: ClassVar[dict[tuple[str, str, str], Any]] = {}
    _lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self, settings: Settings, model: Any | None = None) -> None:
        """`model`: optional fertiges (Fake-)Modell mit faster-whisper-kompatiblem `transcribe()` (Tests)."""
        self.settings = settings
        w = settings.config.whisper
        self.model_name = w.model
        self.device = resolve_device(w.device)
        self.compute_type = resolve_compute_type(w.compute_type, self.device)
        self._model = model
        # Bevorzugtes GPU-Setup; None, sobald CUDA dauerhaft ausfällt (kein Treiber o. ä.)
        self._gpu: tuple[str, str] | None = (self.device, self.compute_type) if self.device == "cuda" else None
        self._cpu_until: float | None = None  # nach vollem VRAM: bis dahin CPU, danach wieder CUDA versuchen

    # -- Modell
    def _load_model(self, name: str, device: str, compute_type: str) -> Any:
        from faster_whisper import WhisperModel

        root = self.settings.config.paths.cache_dir / "whisper"
        root.mkdir(parents=True, exist_ok=True)
        log.info("Loading Whisper model %s (%s, %s)", name, device, compute_type)
        return WhisperModel(name, device=device, compute_type=compute_type, download_root=str(root))

    def _get_cached(self, device: str, compute_type: str) -> Any:
        key = (self.model_name, device, compute_type)
        with Transcriber._lock:
            if key not in Transcriber._models:
                Transcriber._models[key] = self._load_model(*key)
            return Transcriber._models[key]

    def _drop_model(self) -> None:
        """Eigenes Modell loslassen und aus dem Prozess-Cache nehmen (VRAM/RAM wird frei, sobald
        keine andere Instanz es mehr hält)."""
        key = (self.model_name, self.device, self.compute_type)
        with Transcriber._lock:
            if self._model is not None and Transcriber._models.get(key) is self._model:
                del Transcriber._models[key]
        self._model = None

    def _fall_back_to_cpu(self, reason: Exception, permanent: bool) -> None:
        """CPU für CUDA_RETRY_AFTER_S, danach wieder CUDA – bzw. dauerhaft, wenn Treiber/Bibliotheken fehlen."""
        self._drop_model()
        _free_torch_cuda_cache()
        msg = str(reason)  # nur Text loggen: die Exception hielte über den Traceback das CUDA-Modell fest
        if not permanent:
            self._cpu_until = time.monotonic() + CUDA_RETRY_AFTER_S
            log.warning("Whisper on CUDA failed (%s) - using CPU (int8, slower) for %d min, then retrying CUDA",
                        msg, CUDA_RETRY_AFTER_S // 60)
        else:
            self._gpu, self._cpu_until = None, None
            log.warning("Whisper on CUDA failed (%s) - falling back to CPU permanently (int8, slower)", msg)
        self.device, self.compute_type = "cpu", "int8"

    def _maybe_retry_cuda(self) -> None:
        if self._gpu is None or self._cpu_until is None or time.monotonic() < self._cpu_until:
            return
        log.info("Whisper: retrying CUDA after temporary CPU fallback")
        self._drop_model()  # CPU-Kopie nicht zusätzlich im RAM halten
        self.device, self.compute_type = self._gpu
        self._cpu_until = None

    @property
    def model(self) -> Any:
        if self._model is None:
            try:
                self._model = self._get_cached(self.device, self.compute_type)
            except Exception as e:
                if self.device == "cuda":
                    # unklare Ladefehler (Netz, Compute-Type, VRAM) nicht endgültig gegen die GPU werten
                    self._fall_back_to_cpu(e, permanent=cuda_failure_kind(e) == "permanent")
                    return self.model
                raise TranscribeError(
                    f"Whisper-Modell '{self.model_name}' konnte nicht geladen werden ({type(e).__name__}: {e}). "
                    "Beim ersten Start wird es von huggingface.co geladen – Internetverbindung prüfen.",
                    retryable=True) from e
        return self._model

    # -- Transkription
    def _run(self, audio: np.ndarray, language: str | None) -> Transcript:
        segments, info = self.model.transcribe(
            audio, language=language, word_timestamps=True, vad_filter=True, beam_size=5,
            condition_on_previous_text=False)
        out: list[Segment] = []
        for seg in segments:  # Generator: hier passiert die eigentliche Arbeit
            words: list[Word] = []
            for w in getattr(seg, "words", None) or []:
                text = (w.word or "").strip()
                if not text:
                    continue
                start = float(w.start)
                words.append(Word(text=text, start=round(start, 3), end=round(max(start, float(w.end)), 3),
                                  prob=round(float(getattr(w, "probability", 1.0) or 0.0), 4)))
            text = (seg.text or "").strip()
            if not text and not words:
                continue
            mean_prob = sum(w.prob for w in words) / len(words) if words else 0.0
            if _HALLUCINATIONS.search(text) and mean_prob < 0.6:
                log.debug("Dropping likely hallucination: %r", text)
                continue
            out.append(Segment(text=text, start=round(float(seg.start), 3), end=round(float(seg.end), 3),
                               words=words))
        return Transcript(language=getattr(info, "language", None) or language or "und",
                          language_prob=round(float(getattr(info, "language_probability", 0.0) or 0.0), 4),
                          segments=out)

    def transcribe(self, path: str | Path, language: str | None = None) -> Transcript:
        """word_timestamps=True, vad_filter=True. Leere Transkripte sind erlaubt (Gameplay ohne Sprache)."""
        self._maybe_retry_cuda()
        try:
            audio = _ff.decode_audio(path, _ff.SAMPLE_RATE)
        except _ff.FFmpegError as e:
            raise TranscribeError(f"Audio von {Path(path).name} konnte nicht dekodiert werden: {e}") from e
        if audio.size < _ff.SAMPLE_RATE // 4:  # kein/kaum Audio
            return Transcript(language=language or "und", language_prob=0.0)
        try:
            return self._run(audio, language)
        except TranscribeError:
            raise
        except Exception as e:
            kind = cuda_failure_kind(e) if self.device == "cuda" else None
            if kind is None:
                raise TranscribeError(f"Transkription von {Path(path).name} fehlgeschlagen: {e}") from e
            self._fall_back_to_cpu(e, permanent=kind == "permanent")
        try:  # zweiter Versuch auf der CPU
            return self._run(audio, language)
        except TranscribeError:
            raise
        except Exception as e:
            raise TranscribeError(f"Transkription von {Path(path).name} fehlgeschlagen: {e}") from e
