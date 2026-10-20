"""Schnittliste aus dem EditPlan."""

from __future__ import annotations

from wdvi.schemas import EditPlan, TimeMap

MIN_SEGMENT_S = 0.2


def build_timemap(plan: EditPlan, duration: float) -> TimeMap:
    """[trim_start, trim_end] minus remove_ranges -> behaltene Segmente (jeweils >= 0.2 s)."""
    dur = float(duration) if duration and duration > 0 else None
    start = max(0.0, float(plan.trim_start or 0.0))
    end = float(plan.trim_end or 0.0)
    if dur is not None:
        start = min(start, dur)
        end = min(end, dur) if end > 0 else dur
    if end - start < MIN_SEGMENT_S:
        # unbrauchbarer Trim -> ganze Quelle statt Abbruch
        start, end = 0.0, (dur if dur is not None else max(0.0, end))
    if end - start < MIN_SEGMENT_S:
        return TimeMap(segments=[])

    cuts: list[tuple[float, float]] = []
    for a, b in plan.remove_ranges:
        lo, hi = max(start, min(a, b)), min(end, max(a, b))
        if hi > lo:
            cuts.append((lo, hi))
    cuts.sort()

    segs: list[tuple[float, float]] = []
    cur = start
    for a, b in cuts:
        if a > cur:
            segs.append((cur, a))
        cur = max(cur, b)
    if end > cur:
        segs.append((cur, end))

    segs = [(round(s, 3), round(e, 3)) for s, e in segs if e - s >= MIN_SEGMENT_S]
    if not segs:  # remove_ranges haben alles entfernt -> ignorieren
        segs = [(round(start, 3), round(end, 3))]
    return TimeMap(segments=segs)


def map_interval(tm: TimeMap, start: float, end: float) -> tuple[float, float] | None:
    """Quellintervall -> Ausgabeintervall (geschnittene Teile werden übersprungen/geklemmt).
    None, wenn nichts davon behalten wurde."""
    if end < start:
        start, end = end, start
    acc = 0.0
    first: float | None = None
    last = 0.0
    for s, e in tm.segments:
        a, b = max(start, s), min(end, e)
        if b > a or (b == a and start == end and s <= a <= e):
            if first is None:
                first = acc + (a - s)
            last = acc + (b - s)
        acc += e - s
    if first is None:
        return None
    return first, last


def cap_duration(tm: TimeMap, max_s: float) -> TimeMap:
    """Kürzt die Schnittliste hinten auf max_s Sekunden Ausgabedauer."""
    if max_s <= 0 or tm.duration <= max_s:
        return tm
    out: list[tuple[float, float]] = []
    left = max_s
    for s, e in tm.segments:
        if left < MIN_SEGMENT_S:
            break
        d = min(e - s, left)
        out.append((s, round(s + d, 3)))
        left -= d
    return TimeMap(segments=out or tm.segments[:1])
