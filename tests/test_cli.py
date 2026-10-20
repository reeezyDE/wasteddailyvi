"""CLI: Hilfe, init, doctor (gemockt), auth, discover, process-url, Beispiel-Konfiguration."""

from __future__ import annotations

import subprocess
from pathlib import Path

import httpx
import pytest
import respx
import yaml
from typer.testing import CliRunner

from tests.test_pipeline_support import FakePublisher, FakeSource, MediaFakes, make_cand, make_plan
from wdvi import cli, doctor
from wdvi.config import REPO_ROOT, AppConfig
from wdvi.schemas import SourceKind

runner = CliRunner()
SECRETS = [
    "ANTHROPIC_API_KEY",
    "WDVI_DASHBOARD_PASSWORD",
    "WDVI_TWITCH_CLIENT_ID",
    "WDVI_TWITCH_CLIENT_SECRET",
    "WDVI_REDDIT_CLIENT_ID",
    "WDVI_REDDIT_CLIENT_SECRET",
    "WDVI_GOOGLE_CLIENT_SECRETS_FILE",
    "WDVI_YOUTUBE_API_KEY",
    "WDVI_TIKTOK_CLIENT_KEY",
    "WDVI_TIKTOK_CLIENT_SECRET",
    "WDVI_META_APP_ID",
    "WDVI_META_APP_SECRET",
    "WDVI_HOST",
    "WDVI_CONFIG",
    "CLAUDE_CODE_OAUTH_TOKEN",
]


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Leere Umgebung, eigenes Arbeitsverzeichnis, config.yaml mit Daten/Assets im tmp-Ordner."""
    for k in SECRETS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.chdir(tmp_path)
    assets = tmp_path / "assets"
    (assets / "fonts").mkdir(parents=True)
    for f in ("ArchivoBlack-Regular.ttf", "Anton-Regular.ttf"):
        (assets / "fonts" / f).write_bytes((REPO_ROOT / "assets" / "fonts" / f).read_bytes())
    cfg = {
        "paths": {"data_dir": str(tmp_path / "data"), "assets_dir": str(assets)},
        "render": {"encoder": "x264"},
        "whisper": {"device": "cpu"},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return path


def _invoke(cfg_path: Path, *args: str):
    return runner.invoke(cli.app, ["--config", str(cfg_path), *args])


# --------------------------------------------------------------------------- Hilfe / Beispiel-Konfiguration


@pytest.mark.parametrize(
    "cmd",
    [
        [],
        ["run"],
        ["init"],
        ["doctor"],
        ["discover"],
        ["process"],
        ["process-url"],
        ["requeue"],
        ["render-demo"],
        ["auth"],
    ],
)
def test_help(cmd):
    res = runner.invoke(cli.app, [*cmd, "--help"])
    assert res.exit_code == 0, res.output
    assert "Usage" in res.output


def test_no_args_shows_help():
    res = runner.invoke(cli.app, [])
    assert "process-url" in res.output and "render-demo" in res.output


def test_example_config_is_valid_and_complete():
    data = yaml.safe_load((REPO_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    cfg = AppConfig.model_validate(data)
    assert cfg.publish.require_approval is True
    assert cfg.sources.twitch.game_names == ["Grand Theft Auto VI"]
    assert cfg.sources.reddit.subreddits == ["GTA6"]
    assert cfg.publish.timezone == "America/New_York" and cfg.publish.tiktok.mode == "inbox"
    assert cfg.publish.youtube.enabled and cfg.publish.tiktok.enabled and cfg.publish.instagram.enabled

    def missing(model_cls, d, prefix=""):
        out = []
        for name, field in model_cls.model_fields.items():
            if name not in d:
                out.append(prefix + name)
            elif isinstance(d[name], dict) and hasattr(field.annotation, "model_fields"):
                out += missing(field.annotation, d[name], prefix + name + ".")
        return out

    assert missing(AppConfig, data) == ["paths.assets_dir"]  # bewusst auskommentiert (Standard = assets/)


def test_new_config_options_are_real_fields(settings):
    """Felder, die andere Module früher per getattr gelesen haben, gibt es jetzt wirklich (mit Standard)."""
    from datetime import date

    from wdvi.brain import claude, editor, prompts

    cfg = AppConfig()
    assert cfg.llm.max_usd_per_day == 0 and claude._daily_budget(cfg.llm) == 0.0
    assert cfg.filters.gta6_release_date is None
    assert editor._release_date(settings) == prompts.GTA6_RELEASE_DATE
    data = yaml.safe_load("llm:\n  max_usd_per_day: 7.5\nfilters:\n  gta6_release_date: 2027-03-01\n")
    cfg = AppConfig.model_validate(data)
    assert claude._daily_budget(cfg.llm) == 7.5
    assert cfg.filters.gta6_release_date == date(2027, 3, 1)
    settings.config.filters.gta6_release_date = date(2027, 3, 1)
    assert editor._release_date(settings) == date(2027, 3, 1)


def test_env_example_lists_all_secrets():
    from wdvi.config import Secrets

    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    for name in Secrets.model_fields:
        var = "ANTHROPIC_API_KEY" if name == "anthropic_api_key" else f"WDVI_{name.upper()}"
        assert f"\n{var}=" in text, var


# --------------------------------------------------------------------------- Konfigurationsfehler


def test_config_directory_gives_german_hint(env, tmp_path):
    d = tmp_path / "cfgdir"
    d.mkdir()
    res = _invoke(d, "auth", "youtube")
    assert res.exit_code == 2 and "Ordner" in res.output


def test_unknown_config_keys_are_reported(env):
    env.write_text(
        env.read_text(encoding="utf-8")
        + "publish:\n  require_aproval: false\n  youtube:\n    enabld: false\nfilter: {}\n",
        encoding="utf-8",
    )
    res = _invoke(env, "auth", "youtube")
    assert res.exit_code == 0, res.output
    assert "Unbekannte Einträge" in res.output
    for key in ("publish.require_aproval", "publish.youtube.enabld", "filter"):
        assert key in res.output


def test_known_config_keys_are_quiet(env):
    res = _invoke(env, "auth", "youtube")
    assert res.exit_code == 0 and "Unbekannte" not in res.output


def test_invalid_config(env):
    env.write_text("filters:\n  min_virality_score: viel\n", encoding="utf-8")
    res = _invoke(env, "auth", "youtube")
    assert res.exit_code == 2 and "ungültig" in res.output and "min_virality_score" in res.output


# --------------------------------------------------------------------------- init


def test_init_copies_example_and_reports_model_failure(tmp_path, monkeypatch):
    for k in SECRETS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.chdir(tmp_path)
    targets, urls = [], []

    def fake_download(dest_dir, url=cli.YUNET_URL, timeout=60.0):
        targets.append(dest_dir)
        urls.append(url)
        raise RuntimeError("HTTP 404")

    monkeypatch.setattr(cli, "download_face_model", fake_download)
    monkeypatch.setattr(doctor, "find_face_model", lambda settings: None)
    res = _invoke(tmp_path / "config.yaml", "init")
    assert res.exit_code == 0, res.output
    assert (tmp_path / "config.yaml").read_text(encoding="utf-8") == (
        REPO_ROOT / "config.example.yaml"
    ).read_text(encoding="utf-8")
    assert (tmp_path / "data" / "wdvi.db").is_file() and (tmp_path / "data" / "clips").is_dir()
    assert "Haar" in res.output and urls == [cli.YUNET_URL, cli.YUNET_MIRROR]  # GitHub, dann Spiegel
    assert ".env" in res.output
    res2 = _invoke(tmp_path / "config.yaml", "init")
    assert "existiert bereits" in res2.output


def test_init_downloads_model(env, tmp_path, monkeypatch):
    calls = []
    real = cli.download_face_model

    def spy(dest_dir, url=cli.YUNET_URL, timeout=60.0):
        calls.append(dest_dir)
        with respx.mock:
            respx.get(cli.YUNET_URL).mock(
                return_value=httpx.Response(
                    302, headers={"Location": "https://media.githubusercontent.com/media/model.onnx"}
                )
            )
            respx.get("https://media.githubusercontent.com/media/model.onnx").mock(
                return_value=httpx.Response(200, content=b"\x08\x07" + b"\x00" * 230_000)
            )
            return real(dest_dir, url, timeout)

    monkeypatch.setattr(cli, "download_face_model", spy)
    res = _invoke(env, "init")
    assert res.exit_code == 0, res.output
    # data/cache/models liegt im Docker-Volume (assets/ gehört zum Image und ginge mit --rm verloren)
    model = tmp_path / "data" / "cache" / "models" / cli.YUNET_FILE
    assert model.is_file() and model.stat().st_size > 200_000
    assert not (tmp_path / "assets" / "models").exists()
    assert "geladen" in res.output
    res2 = _invoke(env, "init")
    assert "vorhanden" in res2.output and len(calls) == 1


@respx.mock
def test_download_face_model_rejects_lfs_pointer(tmp_path):
    respx.get(cli.YUNET_URL).mock(
        return_value=httpx.Response(
            200, content=b"version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 232589\n"
        )
    )
    with pytest.raises(RuntimeError, match="kein Modell"):
        cli.download_face_model(tmp_path)
    assert not list(tmp_path.iterdir())
    respx.get(cli.YUNET_URL).mock(return_value=httpx.Response(404))
    with pytest.raises(RuntimeError, match="404"):
        cli.download_face_model(tmp_path)


# --------------------------------------------------------------------------- doctor


class FakeProc:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


def _patch_system(monkeypatch, gpu=True, cli_ok=True):
    from wdvi.render import encoder

    tools = {"ffmpeg", "ffprobe", "claude"} | ({"nvidia-smi"} if gpu else set())
    monkeypatch.setattr(doctor, "_which", lambda c: f"/usr/bin/{c}" if c in tools else None)

    def run(cmd, timeout=30.0):
        exe = Path(cmd[0]).name
        if exe == "ffmpeg" and "-filters" in cmd:
            return FakeProc(" ... ass               V->V       Render ASS subtitles\n")
        if exe == "ffmpeg":
            return FakeProc("ffmpeg version 6.1.1 Copyright\n")
        if exe == "nvidia-smi":
            return FakeProc("NVIDIA GeForce RTX 4070, 12282 MiB, 560.94\n")
        if exe == "claude":
            return FakeProc("2.1.0 (Claude Code)\n", 0 if cli_ok else 1)
        raise AssertionError(cmd)

    monkeypatch.setattr(doctor, "_run", run)
    monkeypatch.setattr(encoder, "detect_encoder", lambda pref="auto": "h264_nvenc" if gpu else "libx264")
    monkeypatch.setattr(doctor.importlib.util, "find_spec", lambda name: object())


def _patch_integrations(monkeypatch, connected=True, configured=True):
    from wdvi.publish import base as pub_base
    from wdvi.sources import base as src_base

    def sources(settings):
        return [FakeSource("twitch", [], configured=configured), FakeSource("reddit", [])]

    monkeypatch.setattr(src_base, "build_sources", sources)
    monkeypatch.setattr(
        pub_base,
        "build_publishers",
        lambda settings: {"youtube": FakePublisher("youtube", connected=connected)},
    )


def test_doctor_all_good(env, monkeypatch, tmp_path):
    _patch_system(monkeypatch)
    _patch_integrations(monkeypatch)
    model = tmp_path / "assets" / "models" / doctor.YUNET_FILE
    model.parent.mkdir(parents=True)
    model.write_bytes(b"\x00" * 200_000)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-123")
    res = _invoke(env, "doctor")
    assert res.exit_code == 0, res.output
    assert "❌" not in res.output and "sk-ant-test-123" not in res.output
    for text in (
        "NVENC",
        "RTX 4070",
        "libass",
        "ArchivoBlack",
        "YuNet",
        "API-Key gesetzt",
        "Quelle Twitch",
        "verbunden",
        "Alles bereit",
    ):
        assert text in res.output, text
    assert "Whisper" in res.output and "--full" in res.output  # ohne --full nicht geladen


def test_face_model_search_is_consistent(settings, monkeypatch):
    """doctor, 'wdvi init' und die Gesichtserkennung suchen/legen das YuNet-Modell an denselben Orten ab."""
    from wdvi.media import faces

    paths = settings.config.paths
    assert doctor.find_face_model(settings) is None
    check = doctor.check_face_model(settings)
    assert check.status == "warn" and str(paths.models_dir) in check.hint and "wdvi init" in check.hint
    assert paths.models_dir == paths.cache_dir / "models"
    model = paths.models_dir / doctor.YUNET_FILE  # dorthin lädt 'wdvi init' zuerst
    model.parent.mkdir(parents=True, exist_ok=True)
    model.write_bytes(b"\x00" * 200_000)
    assert doctor.find_face_model(settings) == model
    assert faces.find_yunet_model(paths.assets_dir, paths.cache_dir) == model
    assert paths.face_model_paths(doctor.YUNET_FILE)[1] == model
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "mkdir -p assets/models" in dockerfile  # Image: assets/models (1. Suchort)
    assert paths.face_model_paths(doctor.YUNET_FILE)[0] == paths.assets_dir / "models" / doctor.YUNET_FILE


def test_doctor_reports_problems_with_hints(env, monkeypatch):
    _patch_system(monkeypatch, gpu=False)
    _patch_integrations(monkeypatch, connected=False, configured=False)
    monkeypatch.setattr(doctor.importlib.util, "find_spec", lambda name: None)
    monkeypatch.setenv("WDVI_HOST", "0.0.0.0")
    env.write_text(env.read_text().replace("encoder: x264", "encoder: auto"), encoding="utf-8")
    res = _invoke(env, "doctor")
    assert res.exit_code == 1
    out = res.output
    assert "❌ Claude (API): ANTHROPIC_API_KEY fehlt" in out
    assert "WDVI_DASHBOARD_PASSWORD" in out and "0.0.0.0" in out
    assert "❌ Quelle Twitch: nicht konfiguriert" in out and "WDVI_TWITCH_CLIENT_ID" in out
    assert "⚠️ Posten auf YouTube: noch nicht verbunden" in out and "/connections" in out
    assert "⚠️ NVIDIA-GPU" in out and "⚠️ Video-Encoder" in out and "⚠️ Musik-Erkennung" in out
    assert "⚠️ Gesichtserkennung" in out and "wdvi init" in out
    assert "→" in out  # jede Warnung/jeder Fehler mit Lösungshinweis


def test_doctor_cli_backend(env, monkeypatch):
    _patch_system(monkeypatch, cli_ok=False)
    _patch_integrations(monkeypatch)
    env.write_text(env.read_text() + "llm:\n  backend: cli\n", encoding="utf-8")
    res = _invoke(env, "doctor")
    assert "❌ Claude (CLI)" in res.output and "WITH_CLAUDE_CLI=1" in res.output


def test_doctor_missing_ffmpeg_and_fonts(env, monkeypatch, tmp_path):
    _patch_system(monkeypatch)
    monkeypatch.setattr(doctor, "_which", lambda c: None)
    _patch_integrations(monkeypatch)
    (tmp_path / "assets" / "fonts" / "Anton-Regular.ttf").unlink()
    res = _invoke(env, "doctor")
    assert res.exit_code == 1
    assert "❌ ffmpeg: nicht gefunden" in res.output and "❌ Schrift Hook" in res.output


def test_doctor_check_crash_is_contained(settings, monkeypatch):
    def boom(_settings):
        raise RuntimeError("kaputt")

    monkeypatch.setattr(doctor, "check_fonts", boom)
    _patch_system(monkeypatch)
    checks = doctor.run_checks(settings, sources={}, publishers={})
    crashed = [c for c in checks if c.name == "Schriften"]
    assert crashed and crashed[0].status == "error" and "kaputt" in crashed[0].detail


# --------------------------------------------------------------------------- auth / discover / process


def test_auth_prints_connections_url(env):
    res = _invoke(env, "auth", "tiktok")
    assert res.exit_code == 0
    assert "http://localhost:8420/connections" in res.output and "Redirect-URL einfügen" in res.output
    res = _invoke(env, "auth", "youtube")
    assert "/oauth/youtube/callback" in res.output
    assert _invoke(env, "auth", "myspace").exit_code == 2


def test_discover_command(env, monkeypatch):
    from wdvi.sources import base as src_base
    from wdvi.sources.base import SourceError

    monkeypatch.setattr(
        src_base,
        "build_sources",
        lambda s: [
            FakeSource("twitch", [make_cand("A"), make_cand("B")]),
            FakeSource("reddit", [], error=SourceError("Reddit antwortet nicht")),
        ],
    )
    res = _invoke(env, "discover")
    assert res.exit_code == 0, res.output
    assert "2 neue Kandidaten" in res.output and "Reddit antwortet nicht" in res.output
    res = _invoke(env, "discover", "--source", "kick")
    assert res.exit_code == 1 and "deaktiviert" in res.output


def test_process_command_without_api_key(env, monkeypatch):
    res = _invoke(env, "process")
    assert res.exit_code == 0
    assert "ANTHROPIC_API_KEY" in res.output and "0 neue Clips" in res.output


# --------------------------------------------------------------------------- process-url


TWITCH_INFO = {
    "extractor_key": "TwitchClips",
    "id": "396245304",
    "display_id": "FaintLightGullWholeWheat",
    "title": "EA Play",
    "duration": 32,
    "view_count": 1200,
    "timestamp": 1760900000,
    "channel": "xQc",
    "channel_id": "71092938",
    "uploader": "clipper_guy",
    "webpage_url": "https://www.twitch.tv/xqc/clip/FaintLightGullWholeWheat",
}


def test_candidate_from_info_platforms(settings):
    c = cli.candidate_from_info(TWITCH_INFO, TWITCH_INFO["webpage_url"], settings)
    assert c.source == SourceKind.twitch_clip and c.source_id == "FaintLightGullWholeWheat"
    assert c.creator_handle == "xqc" and c.creator_url == "https://twitch.tv/xqc" and c.duration_s == 32
    assert c.created_at.year == 2025

    kick = {
        "extractor_key": "KickClip",
        "id": "clip_01ABC",
        "channel": "buddha",
        "uploader": "fan",
        "duration": 20,
        "webpage_url": "https://kick.com/buddha/clips/clip_01ABC",
    }
    k = cli.candidate_from_info(kick, kick["webpage_url"], settings)
    assert k.source == SourceKind.kick_clip and k.creator_handle == "buddha" and k.platform_label == "Kick"

    yt = {
        "extractor_key": "Youtube",
        "id": "dQw4w9WgXcQ",
        "title": "GTA 6 stunts",
        "duration": 2400,
        "channel": "Stunt Guy",
        "uploader_id": "@StuntGuy",
        "channel_id": "UCabc",
        "upload_date": "20261019",
    }
    v = cli.candidate_from_info(yt, "https://youtu.be/dQw4w9WgXcQ", settings)
    assert v.source == SourceKind.youtube_video and v.creator_handle == "stuntguy"
    m = cli.candidate_from_info(yt, "u", settings, start=120, end=150)
    assert m.source == SourceKind.youtube_moment and (m.start_s, m.end_s) == (120.0, 150.0)
    short = cli.candidate_from_info({**yt, "duration": 45}, "u", settings)
    assert short.source == SourceKind.youtube_moment and short.end_s == 45
    with pytest.raises(ValueError):
        cli.candidate_from_info(yt, "u", settings, start=10, end=11)

    reddit = {
        "extractor_key": "Reddit",
        "id": "zv89llsvexdz",
        "display_id": "6rrwyj",
        "uploader": "Antw87",
        "channel_id": "GTA6",
        "webpage_url": "https://www.reddit.com/r/GTA6/comments/6rrwyj/x/",
    }
    r = cli.candidate_from_info(reddit, reddit["webpage_url"], settings)
    assert r.source == SourceKind.reddit_post and r.source_id == "6rrwyj" and r.creator_name == "u/Antw87"

    with pytest.raises(ValueError, match="Nicht unterstützt"):
        cli.candidate_from_info({"extractor_key": "Vimeo", "id": "1"}, "u", settings)
    with pytest.raises(ValueError, match="nur Clips"):
        cli.candidate_from_info(
            {**TWITCH_INFO, "extractor_key": "TwitchVod", "webpage_url": "https://www.twitch.tv/videos/1"},
            "u",
            settings,
        )


def _patch_heavy(monkeypatch):
    from wdvi.media import faces, transcribe

    monkeypatch.setattr(faces, "FaceDetector", lambda settings: object())
    monkeypatch.setattr(transcribe, "Transcriber", lambda settings: object())
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")


def test_process_url_creates_clip(env, monkeypatch, tmp_path):
    _patch_heavy(monkeypatch)
    fakes = MediaFakes(monkeypatch)
    monkeypatch.setattr(cli, "extract_info", lambda url, **kw: dict(TWITCH_INFO))
    res = _invoke(env, "process-url", TWITCH_INFO["webpage_url"])
    assert res.exit_code == 0, res.output
    out_path = res.output.strip().splitlines()[-1]
    assert Path(out_path).is_file() and out_path.startswith(str(tmp_path / "data" / "clips"))
    assert fakes.plans == ["twitch_clip:FaintLightGullWholeWheat"]
    assert "pending_review" in res.output


def test_process_url_refuses_duplicate_unless_forced(env, monkeypatch):
    _patch_heavy(monkeypatch)
    fakes = MediaFakes(monkeypatch)
    monkeypatch.setattr(cli, "extract_info", lambda url, **kw: dict(TWITCH_INFO))
    assert _invoke(env, "process-url", TWITCH_INFO["webpage_url"]).exit_code == 0
    res = _invoke(env, "process-url", TWITCH_INFO["webpage_url"])
    assert res.exit_code == 1 and "--force" in res.output and "im Review" in res.output
    assert len(fakes.plans) == 1  # nichts erneut heruntergeladen/bewertet
    res = _invoke(env, "process-url", "--force", TWITCH_INFO["webpage_url"])
    assert res.exit_code == 0, res.output
    assert "--force" in res.output and len(fakes.plans) == 2


def test_process_url_reports_rejection(env, monkeypatch):
    _patch_heavy(monkeypatch)
    fakes = MediaFakes(monkeypatch)
    fakes.plan_by_key["twitch_clip:FaintLightGullWholeWheat"] = make_plan(
        keep=False, reject_reason="zu ruhig"
    )
    monkeypatch.setattr(cli, "extract_info", lambda url, **kw: dict(TWITCH_INFO))
    res = _invoke(env, "process-url", "https://clips.twitch.tv/FaintLightGullWholeWheat")
    assert res.exit_code == 1 and "zu ruhig" in res.output and "rejected" in res.output


def test_process_url_long_youtube_video_mines_first(env, monkeypatch):
    from wdvi.brain import editor
    from wdvi.schemas import MomentPick
    from wdvi.sources import youtube as yt_source

    _patch_heavy(monkeypatch)
    fakes = MediaFakes(monkeypatch)
    info = {
        "extractor_key": "Youtube",
        "id": "abcdefghijk",
        "title": "4h stream",
        "duration": 3600,
        "channel": "Streamer",
        "channel_id": "UCabc",
        "timestamp": 1760900000,
    }
    monkeypatch.setattr(cli, "extract_info", lambda url, **kw: info)
    monkeypatch.setattr(
        yt_source,
        "fetch_video_context",
        lambda url, s: yt_source.VideoContext(
            video_id="abcdefghijk", title="4h stream", channel="Streamer", duration=3600
        ),
    )
    monkeypatch.setattr(
        editor,
        "mine_moments",
        lambda *a, **kw: [
            MomentPick(start=60, end=95, score=95, why="x"),
            MomentPick(start=300, end=330, score=60, why="y"),
        ],
    )
    res = _invoke(env, "process-url", "https://www.youtube.com/watch?v=abcdefghijk")
    assert res.exit_code == 0, res.output
    assert "2 Momente" in res.output and fakes.plans == ["youtube_moment:abcdefghijk@60.0-95.0"]


def test_process_url_unreadable_link(env, monkeypatch):
    def boom(url, **kw):
        raise RuntimeError("Unsupported URL")

    monkeypatch.setattr(cli, "extract_info", boom)
    res = _invoke(env, "process-url", "https://example.com/x")
    assert res.exit_code == 1 and "nicht gelesen" in res.output


# --------------------------------------------------------------------------- render-demo (echtes ffmpeg)


@pytest.mark.slow
def test_render_demo_renders_real_video(env, tmp_path):
    svg = (
        b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 36 36"><circle cx="18" cy="18" r="16"/></svg>'
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r"https://raw\.githubusercontent\.com/.*").mock(
            return_value=httpx.Response(200, content=svg)
        )
        res = _invoke(env, "render-demo", "--seconds", "8")
    assert res.exit_code == 0, res.output
    video = Path(res.output.strip().splitlines()[-1])
    assert video.is_file() and video.suffix == ".mp4"
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0",
            str(video),
        ],
        capture_output=True,
        text=True,
    )
    assert probe.stdout.strip() == "1080,1920"
    assert "libx264" in res.output


def test_unwritable_data_dir_gives_hint(env, tmp_path):
    blocker = tmp_path / "blocked"
    blocker.write_text("kein Ordner", encoding="utf-8")
    env.write_text(env.read_text().replace(str(tmp_path / "data"), str(blocker / "data")), encoding="utf-8")
    res = _invoke(env, "process")
    assert res.exit_code == 2 and "chown" in res.output


def test_fetch_face_model_falls_back_to_cache_dir_when_assets_readonly(tmp_path, monkeypatch):
    calls = []

    def fake(dest_dir, url=cli.YUNET_URL, timeout=60.0):
        calls.append((dest_dir.name, url))
        if dest_dir.parent.name == "assets":
            raise PermissionError("read-only")
        return dest_dir / cli.YUNET_FILE

    monkeypatch.setattr(cli, "download_face_model", fake)
    path, errors = cli._fetch_face_model([tmp_path / "assets" / "models", tmp_path / "cache" / "models"])
    assert path == tmp_path / "cache" / "models" / cli.YUNET_FILE
    assert calls == [("models", cli.YUNET_URL), ("models", cli.YUNET_URL)] and "read-only" in errors[0]


def test_requeue_command(env, monkeypatch):
    from datetime import UTC, datetime

    from sqlmodel import select

    from tests.test_pipeline_support import make_cand as mk
    from wdvi import db as dbmod
    from wdvi.models import Clip, Post
    from wdvi.pipeline import candidate_from_in

    data = env.parent / "data"
    video = data / "clips" / "x.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"x")
    assert _invoke(env, "requeue", "1").exit_code != 0  # DB anlegen; Clip gibt es noch nicht
    with dbmod.session_scope() as s:
        cand = candidate_from_in(mk("ClipA"), status="rendered")
        s.add(cand)
        s.flush()
        clip = Clip(candidate_id=cand.id, status="failed", video_path=str(video), platforms=["youtube"])
        s.add(clip)
        s.flush()
        s.add(
            Post(
                clip_id=clip.id,
                platform="youtube",
                status="failed",
                scheduled_at=datetime(2026, 1, 1, tzinfo=UTC),
                title="t",
                description="d",
                attempts=4,
            )
        )
        clip_id = clip.id
    res = _invoke(env, "requeue", str(clip_id))
    assert res.exit_code == 0, res.output
    assert "YouTube: neu eingeplant" in res.output
    with dbmod.session_scope() as s:
        post = s.exec(select(Post)).one()
        assert post.status == "scheduled" and post.attempts == 0
        assert s.get(Clip, clip_id).status == "scheduled"
    res = _invoke(env, "requeue", str(clip_id))
    assert res.exit_code == 1 and "keine fehlgeschlagenen" in res.output
