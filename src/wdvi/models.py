"""Datenbanktabellen (SQLite via SQLModel).

Status-Flüsse:
  Candidate.status: new -> queued -> processing -> rendered | rejected | skipped | failed
                    (youtube_video: new -> mined)
  Clip.status:      pending_review -> approved -> scheduled -> posted   (oder rejected / failed)
  Post.status:      scheduled -> uploading -> posted | draft_sent | failed | canceled
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(UTC)


class Candidate(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    key: str = Field(index=True, unique=True)  # CandidateIn.key
    source: str = Field(index=True)
    source_id: str
    url: str
    title: str
    creator_name: str
    creator_handle: str | None = None
    creator_url: str | None = None
    platform_label: str
    created_at: datetime
    discovered_at: datetime = Field(default_factory=utcnow)
    duration_s: float | None = None
    view_count: int | None = None
    engagement: dict = Field(default_factory=dict, sa_column=Column(JSON))
    language: str | None = None
    thumbnail_url: str | None = None
    start_s: float | None = None
    end_s: float | None = None
    parent_key: str | None = Field(default=None, index=True)
    extra: dict = Field(default_factory=dict, sa_column=Column(JSON))
    prescore: float = Field(default=0.0, index=True)
    status: str = Field(default="new", index=True)
    status_reason: str | None = None
    updated_at: datetime = Field(default_factory=utcnow)


class Clip(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    candidate_id: int = Field(foreign_key="candidate.id", index=True)
    status: str = Field(default="pending_review", index=True)
    virality_score: int = 0
    plan: dict = Field(default_factory=dict, sa_column=Column(JSON))  # EditPlan.model_dump()
    analysis: dict = Field(default_factory=dict, sa_column=Column(JSON))  # kompakte MediaAnalysis-Zusammenfassung
    video_path: str
    thumb_path: str | None = None
    duration: float = 0.0
    phashes: list = Field(default_factory=list, sa_column=Column(JSON))
    # finale (ggf. im Dashboard editierte) Texte je Plattform
    titles: dict = Field(default_factory=dict, sa_column=Column(JSON))  # {"youtube": "...", ...}
    descriptions: dict = Field(default_factory=dict, sa_column=Column(JSON))
    platforms: list = Field(default_factory=list, sa_column=Column(JSON))  # gewählte Plattformen
    created_at: datetime = Field(default_factory=utcnow)
    reviewed_at: datetime | None = None
    error: str | None = None


class Post(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    clip_id: int = Field(foreign_key="clip.id", index=True)
    platform: str = Field(index=True)
    status: str = Field(default="scheduled", index=True)
    scheduled_at: datetime = Field(index=True)  # UTC
    title: str
    description: str
    remote_id: str | None = None
    remote_url: str | None = None
    attempts: int = 0
    error: str | None = None
    posted_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)


class Metric(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    post_id: int = Field(foreign_key="post.id", index=True)
    fetched_at: datetime = Field(default_factory=utcnow)
    hours_after: int = 0
    views: int | None = None
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None


class CreatorRule(SQLModel, table=True):
    """Pro Creator: block (z. B. Opt-out/Takedown-Wunsch), allow (Erlaubnis liegt vor), boost."""

    id: int | None = Field(default=None, primary_key=True)
    platform_label: str = Field(index=True)  # "Twitch", "Kick", "YouTube", "Reddit"
    handle: str = Field(index=True)  # lowercase
    rule: str  # "block" | "allow" | "boost"
    note: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class JobRun(SQLModel, table=True):
    """Letzte Läufe der Hintergrundjobs (für das Health-Panel im Dashboard)."""

    id: int | None = Field(default=None, primary_key=True)
    job: str = Field(index=True)  # "discover:twitch", "process", "publish", "stats", ...
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    ok: bool = True
    message: str | None = None
