"""YouTube: neue Videos von Kanälen (RSS, ohne API-Key) + optionale Suche (Data API).

Lange Videos werden als SourceKind.youtube_video gemeldet und später per `fetch_video_context`
+ brain.editor.mine_moments in youtube_moment-Kandidaten (Ausschnitte) zerlegt.
"""

from __future__ import annotations

from datetime import datetime

import httpx
from pydantic import BaseModel, Field

from wdvi.config import Settings
from wdvi.schemas import CandidateIn, Transcript


class HeatPoint(BaseModel):
    start: float
    end: float
    value: float  # 0..1 ("Most replayed")


class VideoContext(BaseModel):
    """Alles, was Claude braucht, um in einem langen Video Momente zu finden – ohne Video-Download."""

    video_id: str
    title: str
    channel: str
    duration: float
    description: str = ""
    chapters: list[dict] = Field(default_factory=list)  # [{"start_time":..,"end_time":..,"title":..}]
    heatmap: list[HeatPoint] = Field(default_factory=list)
    transcript: Transcript | None = None  # aus automatischen Untertiteln (Segment-Ebene genügt)
    view_count: int | None = None


class YouTubeSource:
    name = "youtube"

    def __init__(self, settings: Settings, http: httpx.Client | None = None) -> None:
        raise NotImplementedError

    def is_configured(self) -> bool:
        raise NotImplementedError

    def discover(self, now: datetime) -> list[CandidateIn]:
        """RSS https://www.youtube.com/feeds/videos.xml?channel_id=... je Kanal (+ search.list wenn Key).

        Nur Videos jünger als max_age_hours. duration ggf. unbekannt (None) – Mining prüft später.
        Shorts (/shorts/ URL oder < min_video_minutes) werden NICHT gemeldet.
        """
        raise NotImplementedError


def fetch_video_context(url: str, settings: Settings) -> VideoContext:
    """yt-dlp-Metadaten (download=False): Dauer, Kapitel, heatmap, automatische Untertitel (en, json3/vtt)."""
    raise NotImplementedError


def make_moment_candidate(parent: CandidateIn, start: float, end: float, score: int, why: str) -> CandidateIn:
    """youtube_moment-Kandidat aus einem Elternvideo (source_id = video_id, start_s/end_s gesetzt,
    parent_key = parent.key, extra.moment_score/why)."""
    raise NotImplementedError
