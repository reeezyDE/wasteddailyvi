"""Instagram Graph API (Business/Creator-Account): Reels per Resumable Upload
(POST /{ig-user-id}/media media_type=REELS upload_type=resumable -> rupload.facebook.com) -> Status
pollen (status_code FINISHED) -> /{ig-user-id}/media_publish. Long-lived Token (60 Tage) automatisch
verlängern. Alternativ: Token im Dashboard einfügen."""

from __future__ import annotations

from wdvi.config import Settings
from wdvi.schemas import PostMetrics, PostRequest, PostResult


class InstagramPublisher:
    platform = "instagram"

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
