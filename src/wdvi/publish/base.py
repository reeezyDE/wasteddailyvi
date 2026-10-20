"""Gemeinsame Schnittstelle der Plattform-Publisher + Token-Speicher."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Protocol

from wdvi.config import Settings
from wdvi.schemas import PostMetrics, PostRequest, PostResult

log = logging.getLogger(__name__)

# Schreibvorgänge im Prozess serialisieren (Dashboard und Scheduler können eigene Publisher-Instanzen haben)
_SAVE_LOCK = threading.Lock()


class PublishError(RuntimeError):
    pass


class Publisher(Protocol):
    """Optional (nur, wo die Plattform selbst plant – YouTube native_schedule), von der Pipeline per getattr
    genutzt: `unschedule(remote_id) -> str` (geplantes Video zurückziehen, PublishError wenn nicht möglich) und
    `check_status(remote_id) -> str | None` ("public"/"unlisted"/"private"/"missing", None = nicht prüfbar)."""

    platform: str  # "youtube" | "tiktok" | "instagram"

    def is_configured(self) -> bool:
        """App-Credentials vorhanden (client id/secret bzw. client_secret.json)."""
        ...

    def is_connected(self) -> bool:
        """Gültiges (oder erneuerbares) Nutzer-Token vorhanden."""
        ...

    def auth_url(self, state: str) -> str:
        """URL, auf die das Dashboard für OAuth weiterleitet."""
        ...

    def handle_callback(self, params: dict[str, str]) -> None:
        """OAuth-Callback-Parameter (code, state, ...) -> Token speichern. Für Plattformen, die
        keinen localhost-Redirect erlauben, akzeptiert das Dashboard auch eine eingefügte Redirect-URL;
        deren Query-Parameter landen ebenfalls hier."""
        ...

    def publish(self, req: PostRequest) -> PostResult:
        """Hochladen. Darf lange dauern (läuft im Worker-Thread). Fehler als PostResult(ok=False)."""
        ...

    def fetch_metrics(self, remote_id: str) -> PostMetrics | None: ...


class TokenStore:
    """JSON-Dateien in paths.tokens_dir/<name>.json (Dateirechte 600).

    Schreiben ist atomar (Temp-Datei mit 600 + fsync + os.replace): Leser sehen nie eine halb geschriebene
    Datei, und eine volle Platte / ein Absturz beim Token-Refresh zerstört das gespeicherte Token nicht.
    Die vorige Fassung bleibt als <name>.json.bak liegen; load() greift darauf zurück, wenn die Hauptdatei
    unlesbar ist.
    """

    def __init__(self, settings: Settings, name: str) -> None:
        self.path: Path = settings.config.paths.tokens_dir / f"{name}.json"
        self.backup_path: Path = self.path.with_name(f"{self.path.name}.bak")

    @staticmethod
    def _parse(raw: bytes) -> dict:
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Token-Datei: JSON-Objekt erwartet")
        return data

    def _read(self, path: Path) -> dict:
        return self._parse(path.read_bytes())

    def load(self) -> dict | None:
        if not self.path.exists():
            return None
        try:
            return self._read(self.path)
        except (OSError, ValueError) as e:
            try:
                data = self._read(self.backup_path)
            except (OSError, ValueError):
                raise e from None
            log.warning("Token file %s unreadable (%s) – using backup", self.path, type(e).__name__)
            return data

    def save(self, data: dict) -> None:
        payload = json.dumps(data, indent=2).encode("utf-8")
        with _SAVE_LOCK:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            previous: bytes | None = None
            try:
                previous = self.path.read_bytes()
                self._parse(previous)  # nur eine lesbare Fassung als Backup behalten
            except (OSError, ValueError):
                previous = None
            _atomic_write(self.path, payload)
            if previous is not None and previous != payload:
                try:
                    _atomic_write(self.backup_path, previous)
                except OSError as e:  # Backup ist nur Zusatz – das neue Token ist schon sicher gespeichert
                    log.warning("Token backup %s not written: %s", self.backup_path, e)

    def clear(self) -> None:
        with _SAVE_LOCK:
            self.path.unlink(missing_ok=True)
            self.backup_path.unlink(missing_ok=True)


def _atomic_write(path: Path, data: bytes) -> None:
    """Temp-Datei im selben Ordner (mkstemp: Rechte 600), fsync, dann os.replace auf den Zielnamen."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(1, 6):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:  # Windows: Ziel gerade von einem Leser geöffnet -> kurz warten
                if attempt == 5:
                    raise
                time.sleep(0.05 * attempt)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    _fsync_dir(path.parent)


def _fsync_dir(directory: Path) -> None:
    """Verzeichniseintrag nach os.replace auf die Platte bringen (Stromausfall); geht nicht überall."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def build_publishers(settings: Settings) -> dict[str, Publisher]:
    from wdvi.publish.instagram import InstagramPublisher
    from wdvi.publish.tiktok import TikTokPublisher
    from wdvi.publish.youtube import YouTubePublisher

    p = settings.config.publish
    out: dict[str, Publisher] = {}
    if p.youtube.enabled:
        out["youtube"] = YouTubePublisher(settings)
    if p.tiktok.enabled:
        out["tiktok"] = TikTokPublisher(settings)
    if p.instagram.enabled:
        out["instagram"] = InstagramPublisher(settings)
    return out
