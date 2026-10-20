"""Konfiguration: `config.yaml` (Verhalten) + Umgebungsvariablen / `.env` (Secrets).

Alles, was geheim ist (API-Keys, Client-Secrets, Passwörter), kommt NUR aus der Umgebung
(`Secrets`). Alles andere steht in `config.yaml` (`AppConfig`). `load_config()` liefert beides.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- secrets


class Secrets(BaseSettings):
    """Secrets aus Umgebungsvariablen (Präfix `WDVI_`) bzw. `.env`.

    `ANTHROPIC_API_KEY` liest das Anthropic-SDK selbst; hier nur zur Statusanzeige.
    """

    model_config = SettingsConfigDict(env_prefix="WDVI_", env_file=".env", extra="ignore")

    twitch_client_id: str | None = None
    twitch_client_secret: str | None = None
    reddit_client_id: str | None = None
    reddit_client_secret: str | None = None
    youtube_api_key: str | None = None  # optional: nur für YouTube-Suchanfragen (search.list)
    google_client_secrets_file: str | None = None  # Pfad zu client_secret.json (OAuth, Upload)
    tiktok_client_key: str | None = None
    tiktok_client_secret: str | None = None
    meta_app_id: str | None = None
    meta_app_secret: str | None = None
    dashboard_password: str | None = None
    anthropic_api_key: str | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")


# --------------------------------------------------------------------------- config


class BrandConfig(BaseModel):
    name: str = "Wasted Daily"
    handle: str = "WastedDailyVI"
    display_name: str = "Wasted Daily | GTA 6 Clips"
    # Neon-Vice-City-Palette (Hex, ohne Alpha)
    color_primary: str = "#FF2D87"  # Pink – aktives Caption-Wort, Hook-Verlauf
    color_secondary: str = "#FF8A00"  # Orange – Hook-Verlauf, Keyword-Highlight
    color_accent: str = "#7B2FF7"  # Lila – Akzente / Trennlinie
    color_wasted: str = "#E0112B"  # WASTED-Rot
    color_text: str = "#FFFFFF"
    color_outline: str = "#000000"
    font_captions: str = "ArchivoBlack-Regular.ttf"  # Dateiname in assets/fonts
    font_hook: str = "Anton-Regular.ttf"
    caption_uppercase: bool = True
    caption_max_words: int = 3
    caption_max_chars: int = 18
    show_credit: bool = True
    show_watermark: bool = True


class LLMConfig(BaseModel):
    # "api" = Anthropic-API (ANTHROPIC_API_KEY), "cli" = lokales `claude -p` (Claude-Code-Login/Abo)
    backend: Literal["api", "cli"] = "api"
    model: str = "claude-opus-5-5"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    max_frames: int = 8  # Standbilder pro Clip, die Claude sieht
    frame_width: int = 640  # Breite der Standbilder (Tokenkosten!)
    use_fallbacks: bool = True  # server-side refusal fallbacks ("default")
    cli_path: str = "claude"
    cli_timeout_s: int = 600
    max_calls_per_hour: int = 40


class TwitchSourceConfig(BaseModel):
    enabled: bool = True
    game_names: list[str] = ["Grand Theft Auto VI"]
    languages: list[str] = ["en"]  # Twitch-Clip-Sprache; leer = alle
    lookback_hours: int = 24
    min_views: int = 300
    max_clips_per_poll: int = 100
    poll_minutes: int = 10


class KickSourceConfig(BaseModel):
    enabled: bool = False  # inoffizielle Endpunkte – experimentell
    category_slugs: list[str] = ["grand-theft-auto-vi"]
    time_range: Literal["day", "week"] = "day"
    min_views: int = 300
    poll_minutes: int = 15


class YouTubeSourceConfig(BaseModel):
    enabled: bool = True
    channel_ids: list[str] = []  # UC... IDs von GTA-YouTubern/Streamern
    search_queries: list[str] = []  # braucht WDVI_YOUTUBE_API_KEY (100 Quota-Einheiten/Suche)
    max_age_hours: int = 48
    min_video_minutes: float = 3.0  # kürzer = vermutlich Short -> wird nicht recycelt
    max_video_minutes: float = 240.0
    moments_per_video: int = 3
    poll_minutes: int = 15


class RedditSourceConfig(BaseModel):
    enabled: bool = True
    subreddits: list[str] = ["GTA6"]
    listings: list[Literal["hot", "top", "rising"]] = ["hot", "top"]
    top_time: Literal["hour", "day", "week"] = "day"
    min_score: int = 300
    user_agent: str = "wdvi/0.1 (by u/WastedDailyVI)"
    poll_minutes: int = 20


class SourcesConfig(BaseModel):
    twitch: TwitchSourceConfig = TwitchSourceConfig()
    kick: KickSourceConfig = KickSourceConfig()
    youtube: YouTubeSourceConfig = YouTubeSourceConfig()
    reddit: RedditSourceConfig = RedditSourceConfig()


class FilterConfig(BaseModel):
    min_source_duration_s: float = 5.0
    max_source_duration_s: float = 180.0  # für Clips/Posts (nicht für lange YT-Videos)
    max_output_duration_s: float = 59.0  # <60 s: Shorts >1 min mit Content-ID-Claim werden global blockiert
    min_output_duration_s: float = 7.0
    languages: list[str] = ["en"]  # Whisper-erkannte Sprache; leer = alle
    min_virality_score: int = 70  # 0–100, von Claude
    leak_policy: Literal["reject", "allow"] = "reject"  # geleaktes/unveröffentlichtes Material
    music_policy: Literal["reject", "allow"] = "reject"  # reject = Clips mit erkannter Musik verwerfen
    music_threshold: float = 0.45  # AudioSet-"Music"-Wahrscheinlichkeit, ab der Musik als erkannt gilt
    dedup_days: int = 14
    phash_max_distance: int = 10
    candidates_per_cycle: int = 6  # wie viele Top-Kandidaten pro Verarbeitungszyklus
    max_candidate_age_hours: int = 48


class CaptionStyle(BaseModel):
    font_size: int = 82
    outline: int = 7
    shadow: int = 3
    y_ratio: float = 0.66  # vertikale Position (0=oben, 1=unten) der Caption-Mitte; Shorts-UI beachten
    pop_ms: int = 90  # Pop-in-Animation des aktiven Wortes
    active_scale: int = 112  # % Skalierung des aktiven Wortes


class RenderConfig(BaseModel):
    width: int = 1080
    height: int = 1920
    fps: int = 30
    encoder: Literal["auto", "nvenc", "x264"] = "auto"
    x264_crf: int = 18
    nvenc_cq: int = 19
    audio_bitrate: str = "192k"
    target_lufs: float = -14.0
    hook_seconds: float = 3.0
    captions: CaptionStyle = CaptionStyle()
    sfx: bool = True
    sfx_gain_db: float = -8.0
    music_bed: bool = False  # eigene lizenzfreie Tracks in assets/music
    music_gain_db: float = -24.0
    split_facecam_ratio: float = 0.40  # Anteil der Höhe für die Facecam im Split-Layout


class PlatformSlots(BaseModel):
    enabled: bool = False
    slots: list[str] = ["09:00", "12:30", "16:00", "19:00", "22:00"]  # HH:MM in publish.timezone


class YouTubePublishConfig(PlatformSlots):
    enabled: bool = True
    privacy: Literal["public", "unlisted", "private"] = "public"
    native_schedule: bool = True  # früh hochladen als private + publishAt
    category_id: str = "20"  # Gaming
    made_for_kids: bool = False
    max_uploads_per_day: int = 6  # Standard-API-Quota ≈ 6 Uploads/Tag


class TikTokPublishConfig(PlatformSlots):
    # "inbox" = Entwurf in der TikTok-App (du postest selbst), "direct" = Direktpost (App-Audit nötig)
    mode: Literal["inbox", "direct"] = "inbox"
    privacy_level: str = "PUBLIC_TO_EVERYONE"  # ohne Audit nur SELF_ONLY möglich


class InstagramPublishConfig(PlatformSlots):
    share_to_feed: bool = True
    graph_version: str = "v23.0"


class PublishConfig(BaseModel):
    timezone: str = "America/New_York"  # Zielpublikum US/EN
    require_approval: bool = True
    youtube: YouTubePublishConfig = YouTubePublishConfig()
    tiktok: TikTokPublishConfig = TikTokPublishConfig()
    instagram: InstagramPublishConfig = InstagramPublishConfig()
    min_gap_minutes: int = 90  # Mindestabstand zwischen zwei Posts pro Plattform


class DashboardConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8420
    public_base_url: str = "http://localhost:8420"  # für OAuth-Redirects


class FeedbackConfig(BaseModel):
    enabled: bool = True
    fetch_after_hours: list[int] = [24, 72]
    examples_in_prompt: int = 6


class PathsConfig(BaseModel):
    data_dir: Path = Path("data")
    assets_dir: Path = REPO_ROOT / "assets"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "wdvi.db"

    @property
    def work_dir(self) -> Path:  # Downloads + Zwischendateien (wird aufgeräumt)
        return self.data_dir / "work"

    @property
    def clips_dir(self) -> Path:  # fertige MP4s + Thumbnails
        return self.data_dir / "clips"

    @property
    def tokens_dir(self) -> Path:  # OAuth-Tokens (geheim!)
        return self.data_dir / "tokens"

    @property
    def cache_dir(self) -> Path:  # Modelle, Emoji-PNGs, ...
        return self.data_dir / "cache"

    @property
    def fonts_dir(self) -> Path:
        return self.assets_dir / "fonts"

    def ensure(self) -> None:
        for p in (self.data_dir, self.work_dir, self.clips_dir, self.tokens_dir, self.cache_dir):
            p.mkdir(parents=True, exist_ok=True)


class WhisperConfig(BaseModel):
    model: str = "large-v3-turbo"
    device: Literal["auto", "cuda", "cpu"] = "auto"
    compute_type: str = "auto"  # auto -> float16 (cuda) / int8 (cpu)


class AppConfig(BaseModel):
    brand: BrandConfig = BrandConfig()
    llm: LLMConfig = LLMConfig()
    whisper: WhisperConfig = WhisperConfig()
    sources: SourcesConfig = SourcesConfig()
    filters: FilterConfig = FilterConfig()
    render: RenderConfig = RenderConfig()
    publish: PublishConfig = PublishConfig()
    dashboard: DashboardConfig = DashboardConfig()
    feedback: FeedbackConfig = FeedbackConfig()
    paths: PathsConfig = PathsConfig()


class Settings(BaseModel):
    """Alles zusammen – wird überall herumgereicht."""

    config: AppConfig
    secrets: Secrets

    model_config = {"arbitrary_types_allowed": True}


def load_config(path: str | Path | None = "config.yaml") -> Settings:
    """Lädt `config.yaml` (fehlende Datei = Defaults) und Secrets aus der Umgebung."""
    data: dict = {}
    if path is not None and Path(path).exists():
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    cfg = AppConfig.model_validate(data)
    return Settings(config=cfg, secrets=Secrets())
