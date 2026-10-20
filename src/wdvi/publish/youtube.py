"""YouTube Data API v3: OAuth (Web-Flow, Redirect {public_base_url}/oauth/youtube/callback, Scopes
youtube.upload + youtube.readonly), resumable videos.insert. Mit native_schedule: privacyStatus=private +
status.publishAt. Titel bekommt " #shorts" nur, wenn noch Platz ist. Metriken via videos.list(statistics).
WICHTIG: Uploads aus nicht geprüften API-Projekten sind auf "privat" gesperrt, bis das Projekt den
YouTube-API-Audit bestanden hat -> in Fehlermeldung/Doku erwähnen."""

from __future__ import annotations

from wdvi.config import Settings
from wdvi.schemas import PostMetrics, PostRequest, PostResult


class YouTubePublisher:
    platform = "youtube"

    def __init__(self, settings: Settings, http=None) -> None:
        raise NotImplementedError

    def is_configured(self) -> bool:
        raise NotImplementedError

    def is_connected(self) -> bool:
        raise NotImplementedError

    def auth_url(self, state: str) -> str:
        raise NotImplementedError

    def handle_callback(self, params: dict[str, str]) -> None:
        raise NotImplementedError

    def publish(self, req: PostRequest) -> PostResult:
        raise NotImplementedError

    def fetch_metrics(self, remote_id: str) -> PostMetrics | None:
        raise NotImplementedError
