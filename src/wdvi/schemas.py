"""Gemeinsame Datentypen zwischen den Modulen (die "Verträge").

Fluss:  sources -> CandidateIn -> media (MediaAnalysis) -> brain (EditPlan) -> render (RenderResult)
        -> db.Clip -> publish (PostRequest/PostResult)

Konventionen:
- Zeiten in Sekunden (float). "Quellzeit" = Zeit im heruntergeladenen Quellclip,
  "Ausgabezeit" = Zeit im fertigen 9:16-Video.
- Boxen sind normalisiert (0..1) relativ zur Quellbildgröße.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- sources


class SourceKind(StrEnum):
    twitch_clip = "twitch_clip"
    kick_clip = "kick_clip"
    youtube_video = "youtube_video"  # langes Video -> wird in Momente zerlegt
    youtube_moment = "youtube_moment"  # Ausschnitt (start_s/end_s) aus einem youtube_video
    reddit_post = "reddit_post"


class CandidateIn(BaseModel):
    """Ein potenzieller Clip, wie ihn eine Quelle meldet (noch nichts heruntergeladen)."""

    source: SourceKind
    source_id: str  # eindeutig je Quelle (Clip-Slug, Video-ID, Reddit-ID)
    url: str  # Seiten-URL, die yt-dlp versteht
    title: str
    creator_name: str  # Anzeigename für das Credit-Overlay
    creator_handle: str | None = None  # z. B. Twitch-Login, YouTube-@handle, Reddit-User
    creator_url: str | None = None
    platform_label: str  # "Twitch" | "Kick" | "YouTube" | "Reddit"
    created_at: datetime  # tz-aware UTC
    duration_s: float | None = None
    view_count: int | None = None
    engagement: dict[str, float] = Field(default_factory=dict)  # upvotes, comments, likes, ...
    language: str | None = None  # ISO-639-1, falls bekannt
    thumbnail_url: str | None = None
    start_s: float | None = None  # nur youtube_moment: Ausschnitt im Quellvideo
    end_s: float | None = None
    parent_key: str | None = None  # youtube_moment -> key des youtube_video
    extra: dict = Field(default_factory=dict)  # quellspezifisch (vod_offset, broadcaster_id, heatmap, ...)

    @property
    def key(self) -> str:
        base = f"{self.source.value}:{self.source_id}"
        if self.start_s is not None:
            base += f"@{self.start_s:.1f}-{(self.end_s or 0):.1f}"
        return base


# --------------------------------------------------------------------------- media


class Word(BaseModel):
    text: str
    start: float
    end: float
    prob: float = 1.0


class Segment(BaseModel):
    text: str
    start: float
    end: float
    words: list[Word] = Field(default_factory=list)


class Transcript(BaseModel):
    language: str
    language_prob: float = 1.0
    segments: list[Segment] = Field(default_factory=list)

    def words(self) -> list[Word]:
        return [w for s in self.segments for w in s.words]

    @property
    def text(self) -> str:
        return " ".join(s.text.strip() for s in self.segments).strip()


class Box(BaseModel):
    """Normalisierte Box (0..1) relativ zur Quellbildgröße."""

    x: float
    y: float
    w: float
    h: float

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2


class FaceDet(BaseModel):
    box: Box
    score: float


class FrameSample(BaseModel):
    t: float  # Quellzeit
    path: str  # JPEG auf Platte (lange Kante = llm.frame_width, Seitenverhältnis bleibt)
    faces: list[FaceDet] = Field(default_factory=list)


LayoutKind = Literal["facecam_split", "face_track", "gameplay_fit", "vertical_native"]


class TrackPoint(BaseModel):
    t: float
    cx: float  # normalisiert
    cy: float


class LayoutHint(BaseModel):
    """Vorschlag, wie aus 16:9 ein 9:16-Bild wird.

    - facecam_split: Facecam-Overlay erkannt -> oben Facecam (facecam_box), unten Gameplay (gameplay_box)
    - face_track:   Person füllt das Bild (IRL/Kamera) -> 9:16-Fenster folgt dem Gesicht (track)
    - gameplay_fit: kein Gesicht -> Gameplay groß, unscharfer Hintergrund
    - vertical_native: Quelle ist schon hochkant
    """

    kind: LayoutKind
    facecam_box: Box | None = None
    gameplay_box: Box | None = None
    track: list[TrackPoint] = Field(default_factory=list)
    confidence: float = 0.5


class MediaInfo(BaseModel):
    path: str
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool


class MediaAnalysis(BaseModel):
    info: MediaInfo
    transcript: Transcript | None = None
    music_prob: float | None = None  # None = nicht gemessen (Modell fehlt)
    speech_ratio: float = 0.0  # Anteil der Zeit mit Sprache
    loudness_peaks: list[float] = Field(default_factory=list)  # Quellzeiten lauter Momente (Schreien, Lachen)
    frames: list[FrameSample] = Field(default_factory=list)  # für Claude (max llm.max_frames)
    layout: LayoutHint
    phashes: list[str] = Field(default_factory=list)  # Hex-pHashes von ~3 Frames (Dedup)


# --------------------------------------------------------------------------- brain (Claude)

SfxName = Literal["pop", "whoosh", "boom", "ding", "riser", "record_scratch"]


class Zoom(BaseModel):
    start: float  # Quellzeit
    end: float
    scale: float = 1.2  # 1.05 – 1.5
    focus: Literal["face", "gameplay", "center"] = "center"


class Emphasis(BaseModel):
    t: float  # Quellzeit (typisch: Start eines Wortes)
    emoji: str | None = None  # genau EIN Emoji-Zeichen, z. B. "💀"
    sfx: SfxName | None = None


class PlatformText(BaseModel):
    youtube: str
    tiktok: str
    instagram: str


class EditPlan(BaseModel):
    """Claudes Schnitt- und Verpackungsentscheidung für einen Kandidaten."""

    keep: bool
    virality_score: int  # 0–100
    reasoning: str  # 1–3 Sätze, warum (nicht) viral
    reject_reason: str | None = None
    is_leak_or_unreleased: bool = False  # geleaktes / unveröffentlichtes Material
    is_unsafe: bool = False  # NSFW, Hass, Gewalt gegen reale Personen, Doxxing, ...
    content_language: str = "en"
    trim_start: float  # Quellzeit
    trim_end: float
    remove_ranges: list[tuple[float, float]] = Field(default_factory=list)  # Jump-Cuts (Quellzeit)
    layout: LayoutKind | None = None  # None = media.layout übernehmen
    hook_text: str  # max ~6 Wörter, steht die ersten Sekunden oben im Bild
    highlight_words: list[str] = Field(default_factory=list)  # Wörter, die in Captions farbig werden
    zooms: list[Zoom] = Field(default_factory=list)
    emphasis: list[Emphasis] = Field(default_factory=list)
    titles: PlatformText  # YouTube ≤ 100 Zeichen, TikTok/IG kurz
    descriptions: PlatformText  # Caption/Beschreibung inkl. Hashtags (Credit hängt der Code an)
    hashtags: list[str] = Field(default_factory=list)  # ohne "#"


class MomentPick(BaseModel):
    """Ein Ausschnitt aus einem langen YouTube-Video, den Claude als Clip vorschlägt."""

    start: float
    end: float
    score: int  # 0–100
    why: str


class MomentPicks(BaseModel):
    moments: list[MomentPick] = Field(default_factory=list)


class PerformanceExample(BaseModel):
    """Vergangene Posts für den Feedback-Loop im Prompt."""

    platform: str
    title: str
    hook_text: str
    views: int
    hours_after: int


# --------------------------------------------------------------------------- render


class TimeMap(BaseModel):
    """Behaltene Quellabschnitte in Reihenfolge; definiert Quellzeit <-> Ausgabezeit."""

    segments: list[tuple[float, float]]  # (src_start, src_end)

    @property
    def duration(self) -> float:
        return sum(e - s for s, e in self.segments)

    def to_output(self, src_t: float) -> float | None:
        """Quellzeit -> Ausgabezeit; None, wenn src_t herausgeschnitten wurde."""
        acc = 0.0
        for s, e in self.segments:
            if s <= src_t <= e:
                return acc + (src_t - s)
            acc += e - s
        return None

    def to_source(self, out_t: float) -> float:
        acc = 0.0
        for s, e in self.segments:
            d = e - s
            if out_t <= acc + d:
                return s + (out_t - acc)
            acc += d
        return self.segments[-1][1] if self.segments else 0.0


class RenderResult(BaseModel):
    video_path: str
    thumb_path: str
    duration: float
    width: int
    height: int
    encoder: str


# --------------------------------------------------------------------------- publish

Platform = Literal["youtube", "tiktok", "instagram"]


class PostRequest(BaseModel):
    platform: Platform
    video_path: str
    title: str
    description: str
    hashtags: list[str] = Field(default_factory=list)
    publish_at: datetime | None = None  # UTC; None = sofort
    thumb_path: str | None = None


class PostResult(BaseModel):
    ok: bool
    remote_id: str | None = None
    remote_url: str | None = None
    status: Literal["posted", "scheduled", "draft_sent", "failed"] = "failed"
    error: str | None = None
    # optional für Fehlschläge: True = vorübergehend (Netz/Limit, kein Fehlversuch), False = endgültig,
    # None = Pipeline ordnet anhand der Fehlermeldung ein; retry_after_s = frühestens nach so vielen Sekunden
    retryable: bool | None = None
    retry_after_s: float | None = None


class PostMetrics(BaseModel):
    views: int | None = None
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
