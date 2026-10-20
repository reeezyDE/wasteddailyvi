"""TikTok Content Posting API v2: OAuth v2 (client_key, Scopes user.info.basic,video.upload,video.publish),
mode "inbox" -> /v2/post/publish/inbox/video/init/ (Entwurf in der App), mode "direct" ->
/v2/post/publish/creator_info/query/ + /v2/post/publish/video/init/ (FILE_UPLOAD, Chunks per PUT mit
Content-Range) + Status-Polling /v2/post/publish/status/fetch/. Token-Refresh automatisch.
Ohne App-Audit sind Direktposts nur SELF_ONLY."""

from __future__ import annotations

from wdvi.config import Settings
from wdvi.schemas import PostMetrics, PostRequest, PostResult


class TikTokPublisher:
    platform = "tiktok"

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
