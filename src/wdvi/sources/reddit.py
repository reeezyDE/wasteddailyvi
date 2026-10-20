"""Reddit: virale Video-Posts aus r/GTA6 (OAuth application-only). Verweist ein Post auf einen
Twitch-/Kick-/YouTube-Clip, wird der ORIGINAL-Creator gutgeschrieben (extra.linked_platform)."""

from __future__ import annotations

from datetime import datetime

import httpx

from wdvi.config import Settings
from wdvi.schemas import CandidateIn


class RedditSource:
    name = "reddit"

    def __init__(self, settings: Settings, http: httpx.Client | None = None) -> None:
        raise NotImplementedError

    def is_configured(self) -> bool:
        raise NotImplementedError

    def discover(self, now: datetime) -> list[CandidateIn]:
        raise NotImplementedError
