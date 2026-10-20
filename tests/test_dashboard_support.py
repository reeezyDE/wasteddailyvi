"""Gemeinsame Fakes/Helfer für die Dashboard-Tests (enthält selbst keine Tests)."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from wdvi import db as dbmod
from wdvi.config import Settings
from wdvi.dashboard.app import create_app
from wdvi.models import Candidate, Clip, JobRun, Metric, Post

PASSWORD = "korrekt-pferd-batterie"
ORIGIN = "http://testserver"


def basic(password: str, user: str = "admin") -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


class FakePipeline:
    def __init__(self, fail: Exception | None = None) -> None:
        self.approved: list[tuple] = []
        self.rejected: list[tuple] = []
        self.requeued: list[int] = []
        self.fail = fail

    def requeue_clip(self, clip_id, now=None) -> list[tuple[str, datetime]]:
        if self.fail:
            raise self.fail
        self.requeued.append(clip_id)
        when = datetime(2026, 10, 21, 13, 0, tzinfo=UTC)  # 09:00 in New York
        out = []
        with dbmod.session_scope() as s:  # wie die echte Pipeline: fehlgeschlagene Posts neu einplanen
            for p in s.exec(select(Post).where(Post.clip_id == clip_id, Post.status == "failed")).all():
                p.status, p.scheduled_at, p.attempts, p.error = "scheduled", when, 0, None
                s.add(p)
                out.append((p.platform, when))
        return out

    def approve_clip(self, clip_id, platforms, titles=None, descriptions=None) -> None:
        if self.fail:
            raise self.fail
        self.approved.append((clip_id, list(platforms), dict(titles or {}), dict(descriptions or {})))
        with dbmod.session_scope() as s:  # wie die echte Pipeline: Status ändern
            clip = s.get(Clip, clip_id)
            clip.status = "approved"
            clip.platforms = list(platforms)
            s.add(clip)

    def reject_clip(self, clip_id, reason=None) -> None:
        if self.fail:
            raise self.fail
        self.rejected.append((clip_id, reason))
        with dbmod.session_scope() as s:
            clip = s.get(Clip, clip_id)
            clip.status = "rejected"
            s.add(clip)


class FakePublisher:
    def __init__(self, platform: str, configured: bool = True, connected: bool = False,
                 callback_error: Exception | None = None, auth_error: Exception | None = None) -> None:
        self.platform = platform
        self.configured = configured
        self.connected = connected
        self.callback_error = callback_error
        self.auth_error = auth_error
        self.callbacks: list[dict] = []
        self.states: list[str] = []
        self.disconnected = 0

    def is_configured(self) -> bool:
        return self.configured

    def is_connected(self) -> bool:
        return self.connected

    def redirect_uri(self) -> str:
        return f"https://localhost:8420/oauth/{self.platform}/callback"

    def auth_url(self, state: str) -> str:
        if self.auth_error:
            raise self.auth_error
        self.states.append(state)
        return f"https://auth.example.com/{self.platform}/authorize?client_id=abc&state={state}"

    def handle_callback(self, params: dict[str, str]) -> None:
        self.callbacks.append(dict(params))
        if self.callback_error:
            raise self.callback_error
        self.connected = True

    def disconnect(self) -> None:
        self.disconnected += 1
        self.connected = False

    def publish(self, req):  # pragma: no cover - im Dashboard nicht benutzt
        raise AssertionError("dashboard must not publish")

    def fetch_metrics(self, remote_id):  # pragma: no cover
        return None


def make_client(app, password: str | None = PASSWORD, origin: str | None = ORIGIN,
                client: tuple[str, int] = ("testclient", 50000), **kw) -> TestClient:
    headers = {}
    if password is not None:
        headers["Authorization"] = basic(password)
    if origin is not None:
        headers["Origin"] = origin
    return TestClient(app, headers=headers, client=client, **kw)


@pytest.fixture(name="dash")
def dash_fixture(settings: Settings, db):
    """App mit Passwort, Fake-Pipeline und drei Fake-Publishern + passender TestClient."""
    settings.secrets.dashboard_password = PASSWORD
    pipe = FakePipeline()
    pubs = {
        "youtube": FakePublisher("youtube", connected=True),
        "tiktok": FakePublisher("tiktok"),
        "instagram": FakePublisher("instagram"),
    }
    app = create_app(settings, pipeline=pipe, publishers=pubs)
    return SimpleNamespace(
        app=app, client=make_client(app), pipeline=pipe, publishers=pubs, settings=settings,
        clips_dir=Path(settings.config.paths.clips_dir),
    )


# --------------------------------------------------------------------------- Seed-Helfer

_counter = {"n": 0}


def add_candidate(**kw) -> int:
    _counter["n"] += 1
    n = _counter["n"]
    data = dict(
        key=f"twitch_clip:c{n}", source="twitch_clip", source_id=f"c{n}", url=f"https://clips.twitch.tv/c{n}",
        title=f"Clip {n}", creator_name="xQc", creator_handle="xqc", creator_url="https://twitch.tv/xqc",
        platform_label="Twitch", created_at=datetime.now(UTC) - timedelta(hours=2), view_count=12345,
        prescore=4.2, status="rendered",
    )
    data.update(kw)
    with dbmod.session_scope() as s:
        c = Candidate(**data)
        s.add(c)
        s.flush()
        return int(c.id)


def add_clip(clips_dir: Path | None = None, filename: str | None = None, candidate_id: int | None = None,
             **kw) -> int:
    cid = candidate_id or add_candidate()
    video_path = "/nonexistent/video.mp4"
    if clips_dir is not None:
        name = filename or f"clip_{cid}.mp4"
        p = Path(clips_dir) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"0" * 64)
        video_path = str(p)
    data = dict(
        candidate_id=cid, status="pending_review", virality_score=87, video_path=video_path, duration=23.0,
        plan={
            "hook_text": "HE ACTUALLY DID IT", "reasoning": "Huge reaction, clean payoff.",
            "titles": {"youtube": "Plan YT title", "tiktok": "Plan TT", "instagram": "Plan IG"},
            "descriptions": {"youtube": "Plan YT desc", "tiktok": "Plan TT desc", "instagram": "Plan IG desc"},
        },
    )
    data.update(kw)
    with dbmod.session_scope() as s:
        clip = Clip(**data)
        s.add(clip)
        s.flush()
        return int(clip.id)


def add_post(clip_id: int, **kw) -> int:
    data = dict(clip_id=clip_id, platform="youtube", status="scheduled",
                scheduled_at=datetime.now(UTC) + timedelta(hours=3), title="Post title", description="desc")
    data.update(kw)
    with dbmod.session_scope() as s:
        p = Post(**data)
        s.add(p)
        s.flush()
        return int(p.id)


def add_metric(post_id: int, **kw) -> None:
    with dbmod.session_scope() as s:
        s.add(Metric(post_id=post_id, **kw))


def add_jobrun(job: str, ok: bool = True, message: str | None = None, minutes_ago: float = 5) -> None:
    now = datetime.now(UTC)
    with dbmod.session_scope() as s:
        s.add(JobRun(job=job, ok=ok, message=message, started_at=now - timedelta(minutes=minutes_ago, seconds=30),
                     finished_at=now - timedelta(minutes=minutes_ago)))


def get(model, id_: int):
    with dbmod.session_scope() as s:
        return s.get(model, id_)
