"""Gemeinsame Schnittstelle der Plattform-Publisher + Token-Speicher."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from wdvi.config import Settings
from wdvi.schemas import PostMetrics, PostRequest, PostResult


class PublishError(RuntimeError):
    pass


class Publisher(Protocol):
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

    def fetch_metrics(self, remote_id: str) -> PostMetrics | None:
        ...


class TokenStore:
    """JSON-Dateien in paths.tokens_dir/<name>.json (Dateirechte 600)."""

    def __init__(self, settings: Settings, name: str) -> None:
        self.path: Path = settings.config.paths.tokens_dir / f"{name}.json"

    def load(self) -> dict | None:
        if not self.path.exists():
            return None
        return json.loads(self.path.read_text(encoding="utf-8"))

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


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
