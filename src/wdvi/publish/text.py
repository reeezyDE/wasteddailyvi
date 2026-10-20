"""Finale Titel/Beschreibungen je Plattform (Credit + Fan-Hinweis werden IMMER vom Code angehängt)."""

from __future__ import annotations

from wdvi.config import Settings
from wdvi.schemas import CandidateIn


def credit_line(c: CandidateIn) -> str:
    """z. B. "🎥 Clip: xQc on Twitch – twitch.tv/xqc" (creator_url ohne https://, falls vorhanden)."""
    raise NotImplementedError


def compose_description(platform: str, body: str, hashtags: list[str], c: CandidateIn, settings: Settings) -> str:
    """body + Leerzeile + credit_line + Fan-Hinweis ("Fan channel – not affiliated with Rockstar Games.")
    + Hashtags (#GTA6 immer dabei). Plattformlimits: YouTube-Beschreibung 5000, TikTok 2200,
    Instagram 2200 Zeichen / max 30 Hashtags (wir nutzen <= 8)."""
    raise NotImplementedError


def compose_title(platform: str, title: str) -> str:
    """YouTube: <= 100 Zeichen. TikTok/Instagram: Titel ist Teil der Caption (wird vorangestellt)."""
    raise NotImplementedError
