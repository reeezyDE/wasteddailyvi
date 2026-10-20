"""Gemeinsame Fixtures. Keine echten Netzwerkaufrufe in Tests (APIs werden gemockt)."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from wdvi.config import AppConfig, PathsConfig, Secrets, Settings
from wdvi.schemas import CandidateIn, Segment, SourceKind, Transcript, Word


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    cfg = AppConfig(paths=PathsConfig(data_dir=tmp_path / "data"))
    cfg.render.encoder = "x264"
    cfg.whisper.device = "cpu"
    cfg.paths.ensure()
    return Settings(config=cfg, secrets=Secrets(_env_file=None))


@pytest.fixture
def db(settings: Settings):
    from wdvi import db as dbmod

    return dbmod.init_engine(":memory:")


def make_video(path: Path, seconds: float = 8.0, size: str = "1280x720", fps: int = 30,
               audio: bool = True) -> Path:
    """Synthetisches Testvideo (testsrc2 + Sinuston mit Lautstärkespitze bei ~5 s)."""
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
           f"testsrc2=size={size}:rate={fps}:duration={seconds}"]
    if audio:
        cmd += ["-f", "lavfi", "-i",
                f"sine=frequency=440:sample_rate=48000:duration={seconds},"
                "volume='if(between(t,5,5.6),1.0,0.15)':eval=frame"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if audio:
        cmd += ["-c:a", "aac", "-shortest"]
    cmd += [str(path)]
    subprocess.run(cmd, check=True)
    return path


@pytest.fixture(scope="session")
def sample_video(tmp_path_factory) -> Path:
    return make_video(tmp_path_factory.mktemp("media") / "sample.mp4")


@pytest.fixture(scope="session")
def vertical_video(tmp_path_factory) -> Path:
    return make_video(tmp_path_factory.mktemp("media") / "vertical.mp4", seconds=4, size="720x1280")


@pytest.fixture
def sample_transcript() -> Transcript:
    words = "no way he just did that bro look at this insane jump".split()
    ws, t = [], 0.6
    for w in words:
        ws.append(Word(text=w, start=round(t, 2), end=round(t + 0.32, 2)))
        t += 0.42
    return Transcript(language="en", segments=[Segment(text=" ".join(words), start=ws[0].start,
                                                       end=ws[-1].end, words=ws)])


@pytest.fixture
def candidate() -> CandidateIn:
    return CandidateIn(
        source=SourceKind.twitch_clip, source_id="AwkwardHelplessSalamanderSwiftRage",
        url="https://clips.twitch.tv/AwkwardHelplessSalamanderSwiftRage", title="no way bro",
        creator_name="xQc", creator_handle="xqc", creator_url="https://twitch.tv/xqc",
        platform_label="Twitch", created_at=datetime(2026, 10, 19, 18, 0, tzinfo=UTC),
        duration_s=8.0, view_count=12000, language="en",
        extra={"broadcaster_id": "71092938", "video_id": "123", "vod_offset": 4500, "is_featured": False},
    )
