"""Twitch: meistgesehene Clips der GTA-6-Kategorie über die offizielle Helix-API (App-Access-Token)."""

from __future__ import annotations

from datetime import datetime

import httpx

from wdvi.config import Settings
from wdvi.schemas import CandidateIn


class TwitchSource:
    name = "twitch"

    def __init__(self, settings: Settings, http: httpx.Client | None = None) -> None:
        raise NotImplementedError

    def is_configured(self) -> bool:
        raise NotImplementedError

    def discover(self, now: datetime) -> list[CandidateIn]:
        """Clips der Spiele aus config.sources.twitch.game_names, Fenster [now-lookback, now].

        - extra muss enthalten: broadcaster_id, broadcaster_login (lowercase, falls ableitbar),
          video_id, vod_offset, clip_creator_name, is_featured
        - creator_name = broadcaster_name, creator_url = https://twitch.tv/<login>
        - Filter: languages, min_views
        """
        raise NotImplementedError
