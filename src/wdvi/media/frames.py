"""Standbilder für Claude und Layout-Analyse."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path

from wdvi.media import _ff
from wdvi.media import probe as probe_mod
from wdvi.schemas import FrameSample

log = logging.getLogger(__name__)

FOCUS_PAYOFF_S = 0.5  # zweites Bild kurz nach dem Peak (Reaktion/Payoff)
FOCUS_MIN_GAP_S = 1.0  # Frames verschiedener Peaks liegen mindestens so weit auseinander
_END_MARGIN_S = 0.25  # Payoff-Frame nicht direkt ans Clipende legen (Seek liefert dort oft nichts)


def scale_filter(size: int) -> str:
    """Skaliert die lange Kante auf `size` (Hochkant 1080x1920 -> 360x640 statt 640x1138: ~1/3 der Bild-Tokens)."""
    s = int(size)
    return f"scale='if(gt(iw,ih),{s},-2)':'if(gt(iw,ih),-2,{s})'"


def _extract(path: str | Path, t: float, width: int, out: Path) -> bool:
    out.unlink(missing_ok=True)
    cmd = ["ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error", "-ss", f"{max(0.0, t):.3f}",
           "-i", str(path), "-frames:v", "1", "-vf", scale_filter(width), "-q:v", "3", "-update", "1",
           str(out)]
    try:
        _ff.run(cmd, timeout=120)
    except _ff.FFmpegError as e:
        if "nicht gefunden" in str(e):  # ffmpeg fehlt -> kein Sinn, weiterzumachen
            raise
        log.debug("frame extraction failed at %.2fs: %s", t, e)
        return False
    return out.is_file() and out.stat().st_size > 0


def _focus_times(start: float, end: float, focus: Sequence[float], budget: int) -> list[float]:
    """Je Peak (stärkster zuerst) der Peak selbst und +FOCUS_PAYOFF_S, bis `budget` voll ist."""
    chosen: list[float] = []
    for p in focus:
        if len(chosen) >= budget:
            break
        p = float(p)
        if not start <= p <= end or any(abs(p - c) < FOCUS_MIN_GAP_S for c in chosen):
            continue
        q = p + FOCUS_PAYOFF_S
        payoff_ok = q <= end - _END_MARGIN_S and all(abs(q - c) >= FOCUS_MIN_GAP_S for c in chosen)
        chosen += [p, q] if payoff_ok and len(chosen) + 2 <= budget else [p]
    return chosen


def _fill_gaps(start: float, end: float, fixed: list[float], k: int) -> list[float]:
    """k Zeitpunkte, die die Lücken zwischen den festen Zeitpunkten möglichst gleichmäßig füllen.
    Wie bei even_times liegt nie ein Frame genau am Anfang/Ende (Randlücken zählen halb)."""
    if k <= 0:
        return []
    pts = sorted(fixed)
    if not pts:
        return _ff.even_times(start, end, k)
    bounds = [start, *pts, end]
    # (von, bis, Gewicht linker Rand, Gewicht rechter Rand): Clip-Rand 0.5, fester Frame 1.0
    gaps = [(bounds[i], bounds[i + 1], 0.5 if i == 0 else 1.0, 0.5 if i == len(pts) else 1.0)
            for i in range(len(bounds) - 1)]
    counts = [0] * len(gaps)

    def spacing(i: int) -> float:  # Abstand der Frames in Lücke i bei counts[i] Frames darin
        a, b, lo, hi = gaps[i]
        return (b - a) / (counts[i] - 1 + lo + hi)

    for _ in range(k):  # jeder weitere Frame kommt in die Lücke mit dem aktuell größten Abstand
        counts[max(range(len(gaps)), key=spacing)] += 1
    out: list[float] = []
    for i, m in enumerate(counts):
        if m:
            a, _b, lo, _hi = gaps[i]
            d = spacing(i)
            out += [a + (lo + j) * d for j in range(m)]
    return out


def frame_times(start: float, end: float, n: int, focus: Sequence[float] = ()) -> list[float]:
    """n Zeitpunkte in [start, end], aufsteigend. Ohne `focus` gleichmäßig verteilt; sonst bis zur Hälfte an
    den Lautstärke-Peaks (Einschlag + Payoff), der Rest füllt die Lücken dazwischen gleichmäßig."""
    if n <= 0:
        return []
    end = max(start, end)
    fixed = _focus_times(start, end, focus, n // 2) if focus else []
    return sorted(fixed + _fill_gaps(start, end, fixed, n - len(fixed)))


def sample_frames(path: str | Path, out_dir: Path, n: int, width: int, start: float | None = None,
                  end: float | None = None, prefix: str = "f",
                  focus: Sequence[float] = ()) -> list[FrameSample]:
    """n Frames (JPEG, Qualität ~85, lange Kante = `width`, Hochkant also `width` hoch) aus [start, end].
    `focus`: Zeitpunkte der lautesten Momente (stärkster zuerst) -> bis zu n/2 Frames dort, Rest gleichmäßig."""
    if n <= 0:
        return []
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    s = max(0.0, start or 0.0)
    e = end if end is not None else probe_mod.probe(path).duration
    samples: list[FrameSample] = []
    for i, t in enumerate(frame_times(s, max(s, e), n, focus)):
        out = out_dir / f"{prefix}_{i}.jpg"
        ok = _extract(path, t, width, out)
        if not ok and t - s > 0.3:  # kurz vor dem Ende liefert der Seek manchmal nichts -> etwas früher
            t = max(s, t - 0.5)
            ok = _extract(path, t, width, out)
        if ok:
            samples.append(FrameSample(t=round(t, 3), path=str(out)))
        else:
            log.warning("Could not extract frame %d at %.2fs from %s", i, t, Path(path).name)
    return samples
