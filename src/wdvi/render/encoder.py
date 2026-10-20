"""Encoder-Auswahl (NVENC, wenn verfügbar und funktionsfähig; sonst libx264)."""

from __future__ import annotations

import logging
import subprocess
import threading

from wdvi.config import RenderConfig

log = logging.getLogger(__name__)

NVENC = "h264_nvenc"
X264 = "libx264"

_lock = threading.Lock()
_auto_result: str | None = None


def _nvenc_works() -> bool:
    """1-Frame-Testencode: NVENC ist oft einkompiliert, aber ohne GPU/Treiber nicht nutzbar."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-f", "lavfi", "-i",
           "color=c=black:s=256x256:d=0.1", "-frames:v", "1", "-c:v", NVENC, "-f", "null", "-"]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=30, check=False, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        log.info("NVENC test failed to run: %s", e)
        return False
    if proc.returncode != 0:
        msg = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        log.info("NVENC not usable (%s), falling back to libx264", msg[-1] if msg else proc.returncode)
        return False
    return True


def detect_encoder(pref: str = "auto") -> str:
    """'h264_nvenc' oder 'libx264'. 'auto' testet NVENC mit einem 1-Frame-Encode (Ergebnis gecacht)."""
    global _auto_result
    p = (pref or "auto").strip().lower()
    if p in ("nvenc", NVENC):
        return NVENC
    if p in ("x264", X264, "cpu"):
        return X264
    if p != "auto":
        log.warning("Unknown encoder preference %r, using auto detection", pref)
    with _lock:
        if _auto_result is None:
            _auto_result = NVENC if _nvenc_works() else X264
            log.info("Video encoder: %s", _auto_result)
        return _auto_result


def reset_cache() -> None:
    """Nur für Tests: erneute Erkennung erzwingen."""
    global _auto_result
    with _lock:
        _auto_result = None


def video_codec_args(encoder: str, cfg: RenderConfig) -> list[str]:
    """ffmpeg-Argumente für Qualität (CRF/CQ), Profil high, yuv420p, GOP = 2*fps, +faststart."""
    if encoder in (NVENC, "nvenc"):
        args = ["-c:v", NVENC, "-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", str(cfg.nvenc_cq),
                "-b:v", "0", "-profile:v", "high"]
    else:
        args = ["-c:v", X264, "-preset", "medium", "-crf", str(cfg.x264_crf), "-profile:v", "high"]
    return args + ["-pix_fmt", "yuv420p", "-g", str(2 * int(cfg.fps)), "-movflags", "+faststart"]
