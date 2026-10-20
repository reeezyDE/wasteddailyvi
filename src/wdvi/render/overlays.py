"""PNG-Overlays mit Pillow: Hook-Titel, Credit-Tag, Wasserzeichen, Emojis (Twemoji, CC-BY 4.0)."""

from __future__ import annotations

from pathlib import Path

from wdvi.config import Settings


def render_hook_png(text: str, settings: Settings, out: Path) -> Path:
    """Großer Hook-Text (font_hook, Großbuchstaben, max 2 Zeilen, auto-skaliert auf ~90 % Breite) auf
    abgerundetem Verlaufsbalken (color_primary -> color_secondary), schwarze Kontur. RGBA, Breite = render.width."""
    raise NotImplementedError


def render_credit_png(platform_label: str, creator_name: str, settings: Settings, out: Path) -> Path:
    """Kleines halbtransparentes Pill-Label, z. B. "🎥 TWITCH · xQc" (Emoji als Bild, nicht als Font)."""
    raise NotImplementedError


def render_watermark_png(settings: Settings, out: Path) -> Path:
    """Dezentes "WASTED DAILY VI" (WASTED in color_wasted)."""
    raise NotImplementedError


def emoji_png(emoji: str, size: int, cache_dir: Path) -> Path | None:
    """Twemoji-SVG (raw.githubusercontent.com/jdecked/twemoji) -> PNG in size px, gecacht. None bei Fehler."""
    raise NotImplementedError
