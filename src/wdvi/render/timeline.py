"""Schnittliste aus dem EditPlan."""

from __future__ import annotations

from wdvi.schemas import EditPlan, TimeMap


def build_timemap(plan: EditPlan, duration: float) -> TimeMap:
    """[trim_start, trim_end] minus remove_ranges -> behaltene Segmente (jeweils >= 0.2 s)."""
    raise NotImplementedError
