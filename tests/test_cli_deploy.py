"""Betriebsdateien: Docker, Compose, Skripte, Healthcheck, .env.example."""

from __future__ import annotations

import importlib.util
import re
import subprocess
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from wdvi.config import REPO_ROOT


def _read(name: str) -> str:
    return (REPO_ROOT / name).read_text(encoding="utf-8")


def test_setup_ps1_is_ascii_for_windows_powershell_51():
    """PowerShell 5.1 liest Skripte ohne BOM als ANSI: Gedankenstriche/Umlaute zerlegen das Skript."""
    raw = (REPO_ROOT / "scripts" / "setup.ps1").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf") or all(b < 0x80 for b in raw), (
        "setup.ps1 must be ASCII or UTF-8 BOM"
    )


@pytest.mark.parametrize("script", sorted(p.name for p in (REPO_ROOT / "scripts").glob("*.sh")))
def test_shell_scripts_parse(script):
    path = REPO_ROOT / "scripts" / script
    assert b"\r\n" not in path.read_bytes()
    res = subprocess.run(["sh", "-n", str(path)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


def test_update_script_refreshes_ytdlp_and_explains_pull_failures():
    s = _read("scripts/update.sh")
    assert "WDVI_REFRESH=" in s and "docker compose build --pull" in s
    assert "git pull --ff-only" in s and "git checkout --" in s


def test_backup_never_drops_the_wal():
    s = _read("scripts/backup.sh")
    assert ".backup(" in s and "db-wal" in s
    assert "\ncp data/wdvi.db " not in s  # nicht nur die Hauptdatei kopieren


def test_setup_sh_records_host_uid():
    s = _read("scripts/setup.sh")
    assert "APP_UID=" in s and "id -u" in s


def test_dockerfile_layers():
    s = _read("Dockerfile")
    assert re.search(r"^FROM nvidia/cuda:12\.[89]\.\d+-cudnn-runtime-ubuntu24\.04$", s, re.M)
    assert "whl/cu128" in s  # Blackwell (RTX 50xx) braucht cu128+
    torch_at = s.index('pip install --index-url "${TORCH_INDEX_URL}" torch')
    # ARGs, die sich ändern, erst nach dem torch-Layer (sonst lädt jede Änderung ~3 GB neu)
    for arg in ("ARG WITH_CLAUDE_CLI", "ARG REFRESH", "ARG APP_UID"):
        assert s.index(arg) > torch_at, arg
    refresh = s[s.index("ARG REFRESH") :]
    assert 'pip install --upgrade "yt-dlp[default,deno]"' in refresh
    assert "@anthropic-ai/claude-code" in refresh


def test_compose_is_configurable_via_env():
    s = _read("docker-compose.yml")
    d = yaml.safe_load(s)["services"]["wdvi"]
    assert d["ports"] == ["${WDVI_BIND_ADDR:-127.0.0.1}:${WDVI_HOST_PORT:-8420}:8420"]
    assert set(d["build"]["args"]) == {"WITH_CLAUDE_CLI", "APP_UID", "TORCH_INDEX_URL", "REFRESH"}
    assert d["healthcheck"]["test"] == ["CMD", "python", "/app/scripts/healthcheck.py"]
    m = re.fullmatch(r"(\d+)m", str(d["stop_grace_period"]))
    assert m and int(m.group(1)) >= 6  # TikTok prüft bis zu 5 min nach dem Upload


def test_env_example_documents_quoting_and_docker_vars():
    s = _read(".env.example")
    assert "ohne Anführungszeichen" not in s and "EINFACHE Anführungszeichen" in s
    for var in ("APP_UID", "WDVI_BIND_ADDR", "WDVI_HOST_PORT", "TORCH_INDEX_URL", "WITH_CLAUDE_CLI"):
        assert f"# {var}=" in s, var


# --------------------------------------------------------------------------- Healthcheck


@pytest.fixture
def health(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "wdvi_healthcheck", REPO_ROOT / "scripts" / "healthcheck.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "DB", str(tmp_path / "wdvi.db"))
    return mod


def _db_with_job(path: Path, job: str, age: timedelta) -> None:
    from wdvi import db as dbmod
    from wdvi.models import JobRun

    dbmod.init_engine(path)
    with dbmod.session_scope() as s:
        s.add(JobRun(job=job, started_at=datetime.now(UTC) - age))


class _Resp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return None


def test_healthcheck_healthy(health, tmp_path, monkeypatch):
    _db_with_job(tmp_path / "wdvi.db", "publish", timedelta(minutes=1))
    monkeypatch.setattr(health.urllib.request, "urlopen", lambda *a, **k: _Resp())
    assert health.main() == 0


def test_healthcheck_401_counts_as_answering(health, tmp_path, monkeypatch):
    _db_with_job(tmp_path / "wdvi.db", "schedule", timedelta(minutes=2))

    def unauthorized(*a, **k):
        raise urllib.error.HTTPError("http://x/health", 401, "Unauthorized", {}, None)

    monkeypatch.setattr(health.urllib.request, "urlopen", unauthorized)
    assert health.main() == 0


def test_healthcheck_detects_stalled_scheduler(health, tmp_path, monkeypatch, capsys):
    _db_with_job(tmp_path / "wdvi.db", "publish", timedelta(hours=2))
    monkeypatch.setattr(health.urllib.request, "urlopen", lambda *a, **k: _Resp())
    assert health.main() == 1
    assert "Scheduler" in capsys.readouterr().out


def test_healthcheck_detects_dead_dashboard(health, tmp_path, monkeypatch, capsys):
    _db_with_job(tmp_path / "wdvi.db", "publish", timedelta(minutes=1))

    def refused(*a, **k):
        raise urllib.error.URLError("Connection refused")

    monkeypatch.setattr(health.urllib.request, "urlopen", refused)
    assert health.main() == 1 and "nicht erreichbar" in capsys.readouterr().out


SIGTERM_CHILD = """
import sys, time, threading, uvicorn
from wdvi.cli import _keep_sigterm_for_cleanup

async def app(scope, receive, send):
    pass

def job():  # langer Hintergrund-Job (wie ein Upload)
    time.sleep(1.0)
    print("job finished", flush=True)

_keep_sigterm_for_cleanup()
t = threading.Thread(target=job)
print("ready", flush=True)
t.start()
try:
    uvicorn.run(app, host="127.0.0.1", port=0, log_level="error")
finally:
    t.join()
    print("cleanup ran", flush=True)
"""


def test_sigterm_lets_cleanup_run():
    """docker stop: uvicorn löst SIGTERM nach dem Herunterfahren erneut aus – der Prozess darf dabei nicht
    sterben, bevor laufende Jobs (Uploads) fertig sind."""
    import signal
    import sys
    import time

    proc = subprocess.Popen(
        [sys.executable, "-c", SIGTERM_CHILD], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        assert proc.stdout.readline().strip() == "ready"
        time.sleep(0.5)  # uvicorn läuft
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=20)
    finally:
        proc.kill()
    assert "job finished" in out and "cleanup ran" in out, (out, err)
    assert proc.returncode == 0, err
