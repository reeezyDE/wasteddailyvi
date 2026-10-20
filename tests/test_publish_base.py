"""TokenStore (atomares Schreiben, Backup) und Redirect-URIs aus `.env`."""

from __future__ import annotations

import json
import os
import resource
import signal
import stat
import threading

import pytest

from wdvi.config import Secrets
from wdvi.publish import base
from wdvi.publish._common import https_redirect_uri
from wdvi.publish.base import TokenStore

GOOD = {
    "refresh_token": "1//0g-very-important",
    "client_id": "cid",
    "client_secret": "sec",
    "token": "ya29." + "x" * 400,
}


def leftovers(store: TokenStore) -> list[str]:
    return sorted(p.name for p in store.path.parent.iterdir() if p.name.endswith(".tmp"))


def test_save_load_roundtrip_with_private_permissions(settings):
    store = TokenStore(settings, "tiktok")
    assert store.load() is None
    store.save(GOOD)
    assert store.load() == GOOD
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert leftovers(store) == []
    assert not store.backup_path.exists()  # erste Fassung: noch nichts zu sichern

    store.save({**GOOD, "token": "ya29.new"})
    assert store.load()["token"] == "ya29.new"
    assert json.loads(store.backup_path.read_text())["token"] == GOOD["token"]
    assert stat.S_IMODE(store.backup_path.stat().st_mode) == 0o600


def test_failed_write_keeps_existing_token(settings):
    """Volle Platte beim Token-Refresh (simuliert mit RLIMIT_FSIZE -> EFBIG, wie ENOSPC): die alte
    Datei bleibt vollständig erhalten, statt abgeschnitten zu werden."""
    store = TokenStore(settings, "youtube")
    store.save(GOOD)

    old_handler = signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
    resource.setrlimit(resource.RLIMIT_FSIZE, (64, hard))
    try:
        with pytest.raises(OSError):
            store.save({**GOOD, "token": "ya29." + "y" * 400})
    finally:
        resource.setrlimit(resource.RLIMIT_FSIZE, (soft, hard))
        signal.signal(signal.SIGXFSZ, old_handler)

    assert store.load() == GOOD
    assert leftovers(store) == []


def test_replace_failure_keeps_existing_token(settings, monkeypatch):
    store = TokenStore(settings, "instagram")
    store.save(GOOD)

    def boom(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(base.os, "replace", boom)
    with pytest.raises(OSError):
        store.save({"access_token": "new"})
    monkeypatch.undo()
    assert store.load() == GOOD
    assert leftovers(store) == []


def test_load_falls_back_to_backup(settings):
    store = TokenStore(settings, "youtube")
    store.save(GOOD)
    store.save({**GOOD, "token": "ya29.second"})
    store.path.write_text('{\n  "refresh_token": "1//0g-ver', encoding="utf-8")  # z. B. Plattendefekt
    assert store.load() == GOOD  # vorige, lesbare Fassung

    # kaputte Hauptdatei wird beim nächsten Speichern nicht als Backup übernommen
    store.save({**GOOD, "token": "ya29.third"})
    assert store.load()["token"] == "ya29.third"
    assert json.loads(store.backup_path.read_text()) == GOOD


def test_load_raises_when_main_and_backup_unreadable(settings):
    store = TokenStore(settings, "tiktok")
    store.path.write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError):
        store.load()
    store.path.write_text("[1, 2]", encoding="utf-8")  # kein Objekt
    with pytest.raises(ValueError):
        store.load()


def test_clear_removes_token_and_backup(settings):
    store = TokenStore(settings, "tiktok")
    store.save(GOOD)
    store.save({**GOOD, "token": "new"})
    assert store.backup_path.exists()
    store.clear()
    assert not store.path.exists() and not store.backup_path.exists()
    assert store.load() is None  # getrennt bleibt getrennt (kein Rückgriff aufs Backup)
    store.clear()  # idempotent


def test_concurrent_readers_never_see_partial_file(settings):
    """Ein Refresh im Scheduler-Thread darf is_connected() in einem anderen Thread nicht stören."""
    store = TokenStore(settings, "tiktok")
    store.save({"access_token": "a" * 5000, "n": 0})
    stop = threading.Event()
    errors: list[BaseException] = []

    def writer() -> None:
        n = 0
        while not stop.is_set():
            n += 1
            store.save({"access_token": "b" * (5000 + n % 7), "n": n})

    t = threading.Thread(target=writer)
    t.start()
    try:
        for _ in range(300):
            try:
                assert store.load()["access_token"]
            except BaseException as e:  # noqa: BLE001
                errors.append(e)
    finally:
        stop.set()
        t.join()
    assert errors == []
    assert leftovers(store) == []


# ---------------------------------------------------------------------------- Redirect-URIs


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    for name in ("WDVI_TIKTOK_REDIRECT_URI", "WDVI_INSTAGRAM_REDIRECT_URI"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _reload_secrets(settings) -> None:
    """Wie beim Programmstart (load_config): Secrets aus Umgebung + `.env` im Arbeitsordner lesen."""
    settings.secrets = Secrets()


def test_redirect_uri_default_is_https(settings, clean_env):
    settings.config.dashboard.public_base_url = "http://localhost:8420/"
    assert https_redirect_uri(settings, "tiktok") == "https://localhost:8420/oauth/tiktok/callback"


def test_redirect_uri_from_dotenv_without_docker(settings, clean_env):
    """`.env` wird nur ins Secrets-Modell geladen, nicht nach os.environ – der Eintrag muss trotzdem
    wirken (wdvi run im venv)."""
    (clean_env / ".env").write_text(
        "WDVI_TIKTOK_REDIRECT_URI=https://wdvi.example.com/oauth/tiktok/callback\n"
        'WDVI_INSTAGRAM_REDIRECT_URI="https://wdvi.example.com/oauth/instagram/callback"\n',
        encoding="utf-8",
    )
    assert "WDVI_TIKTOK_REDIRECT_URI" not in os.environ
    _reload_secrets(settings)
    assert https_redirect_uri(settings, "tiktok") == "https://wdvi.example.com/oauth/tiktok/callback"
    assert https_redirect_uri(settings, "instagram") == "https://wdvi.example.com/oauth/instagram/callback"


def test_redirect_uri_environment_beats_dotenv(settings, clean_env, monkeypatch):
    (clean_env / ".env").write_text("WDVI_TIKTOK_REDIRECT_URI=https://from-dotenv/cb\n", encoding="utf-8")
    monkeypatch.setenv("WDVI_TIKTOK_REDIRECT_URI", "  https://from-env/cb  ")
    _reload_secrets(settings)
    assert https_redirect_uri(settings, "tiktok") == "https://from-env/cb"


def test_redirect_uri_only_from_secrets(settings, clean_env, monkeypatch):
    """Secrets sind die einzige Quelle: eine erst nach dem Laden gesetzte Umgebungsvariable wirkt nicht,
    ein leerer Eintrag (wie in .env.example) gilt als nicht gesetzt."""
    monkeypatch.setenv("WDVI_TIKTOK_REDIRECT_URI", "https://late/cb")
    assert https_redirect_uri(settings, "tiktok").endswith("/oauth/tiktok/callback")
    assert "late" not in https_redirect_uri(settings, "tiktok")
    settings.secrets.instagram_redirect_uri = "https://set-in-secrets/cb"
    assert https_redirect_uri(settings, "instagram") == "https://set-in-secrets/cb"
    monkeypatch.setenv("WDVI_TIKTOK_REDIRECT_URI", "")
    _reload_secrets(settings)
    assert settings.secrets.tiktok_redirect_uri is None
