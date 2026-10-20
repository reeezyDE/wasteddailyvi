"""Kick: Top-Clips einer Kategorie (inoffizielle Web-Endpunkte, Cloudflare -> curl_cffi). Experimentell."""

from __future__ import annotations

from datetime import datetime

from wdvi.config import Settings
from wdvi.schemas import CandidateIn


class KickSource:
    name = "kick"

    def __init__(self, settings: Settings, session=None) -> None:
        raise NotImplementedError

    def is_configured(self) -> bool:
        raise NotImplementedError

    def discover(self, now: datetime) -> list[CandidateIn]:
        raise NotImplementedError
