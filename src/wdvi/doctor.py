"""`wdvi doctor`: prüft die Installation und sagt auf Deutsch, was zu tun ist.

Jeder Check liefert ✅ (ok), ⚠️ (läuft, aber eingeschränkt), ❌ (muss behoben werden)
oder ➖ (Info/übersprungen).
Keine Secrets ausgeben – nur "gesetzt"/"fehlt".
"""

from __future__ import annotations

import importlib.util
import logging
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from wdvi.config import Settings

log = logging.getLogger(__name__)

Status = Literal["ok", "warn", "error", "info"]
ICONS: dict[str, str] = {"ok": "✅", "warn": "⚠️", "error": "❌", "info": "➖"}
YUNET_FILE = "face_detection_yunet_2023mar.onnx"
MIN_MODEL_BYTES = 100_000
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}

SOURCE_HINTS = {
    "twitch": "WDVI_TWITCH_CLIENT_ID und WDVI_TWITCH_CLIENT_SECRET in .env eintragen "
    "(docs/SETUP.md → Twitch).",
    "kick": "Kick braucht keine Zugangsdaten – bei Problemen sources.kick.enabled: false setzen.",
    "youtube": "YouTube-Kanal-IDs (UC…) in config.yaml unter sources.youtube.channel_ids eintragen "
    "(docs/SETUP.md → Kanal-IDs finden).",
    "reddit": "Mindestens ein Subreddit unter sources.reddit.subreddits eintragen; optional "
    "WDVI_REDDIT_CLIENT_ID/SECRET (docs/SETUP.md → Reddit).",
}
PUBLISH_HINTS = {
    "youtube": "client_secret.json herunterladen, z. B. nach data/client_secret.json legen und "
    "WDVI_GOOGLE_CLIENT_SECRETS_FILE=data/client_secret.json in .env setzen (docs/SETUP.md → YouTube).",
    "tiktok": "WDVI_TIKTOK_CLIENT_KEY und WDVI_TIKTOK_CLIENT_SECRET in .env eintragen "
    "(docs/SETUP.md → TikTok).",
    "instagram": "WDVI_META_APP_ID und WDVI_META_APP_SECRET in .env eintragen (docs/SETUP.md → Instagram).",
}
PLATFORM_LABELS = {"youtube": "YouTube", "tiktok": "TikTok", "instagram": "Instagram"}
SOURCE_LABELS = {"twitch": "Twitch", "kick": "Kick", "youtube": "YouTube", "reddit": "Reddit"}


@dataclass
class Check:
    name: str
    status: Status
    detail: str
    hint: str | None = None

    @property
    def icon(self) -> str:
        return ICONS[self.status]

    def render(self) -> str:
        line = f"{self.icon} {self.name}: {self.detail}"
        if self.hint and self.status != "ok":
            line += f"\n     → {self.hint}"
        return line


# ------------------------------------------------------------------ Systemaufrufe (in Tests patchbar)


def _which(cmd: str) -> str | None:
    return shutil.which(cmd)


def _run(cmd: list[str], timeout: float = 30.0) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


# --------------------------------------------------------------------------- einzelne Checks


def check_config(config_path: Path | None) -> Check:
    if config_path is None or not Path(config_path).exists():
        return Check(
            "Konfiguration",
            "warn",
            "config.yaml fehlt – es gelten die Standardwerte",
            "'wdvi init' ausführen (kopiert config.example.yaml nach config.yaml) und anpassen.",
        )
    return Check("Konfiguration", "ok", f"{config_path} geladen")


def check_ffmpeg() -> list[Check]:
    out: list[Check] = []
    if not _which("ffmpeg"):
        return [
            Check(
                "ffmpeg",
                "error",
                "nicht gefunden",
                "ffmpeg installieren (im Docker-Image enthalten; Linux: sudo apt install ffmpeg).",
            )
        ]
    ver = _run(["ffmpeg", "-hide_banner", "-version"])
    first = (ver.stdout or "").splitlines()[0] if ver.stdout else "Version unbekannt"
    out.append(Check("ffmpeg", "ok", first.replace("ffmpeg version ", "Version ")[:80]))
    filters = _run(["ffmpeg", "-hide_banner", "-filters"])
    if re.search(r"^\s*\S*\s+ass\s", filters.stdout or "", re.MULTILINE):
        out.append(Check("ffmpeg-Untertitel (libass)", "ok", "Filter 'ass' vorhanden"))
    else:
        out.append(
            Check(
                "ffmpeg-Untertitel (libass)",
                "error",
                "Filter 'ass' fehlt – animierte Captions unmöglich",
                "Ein ffmpeg mit libass verwenden (Docker-Image bzw. 'apt install ffmpeg').",
            )
        )
    if not _which("ffprobe"):
        out.append(
            Check("ffprobe", "error", "nicht gefunden", "ffprobe gehört zu ffmpeg – ffmpeg neu installieren.")
        )
    return out


def check_encoder(settings: Settings) -> Check:
    from wdvi.render import encoder

    pref = settings.config.render.encoder
    enc = encoder.detect_encoder(pref)
    if enc == "h264_nvenc":
        return Check("Video-Encoder", "ok", "NVENC (GPU) – schnelles Rendern")
    if pref == "x264":
        return Check("Video-Encoder", "ok", "libx264 (CPU) – so in config.yaml eingestellt")
    return Check(
        "Video-Encoder",
        "warn",
        "NVENC nicht nutzbar – Rendern läuft auf der CPU (libx264, langsamer)",
        "NVIDIA-Treiber aktuell halten; Docker: NVIDIA Container Toolkit installieren und den Container "
        "mit GPU starten (docker-compose.yml enthält die GPU-Reservierung inkl. 'video').",
    )


def check_gpu() -> Check:
    if not _which("nvidia-smi"):
        return Check(
            "NVIDIA-GPU",
            "warn",
            "nvidia-smi nicht gefunden – keine GPU sichtbar",
            "Windows: aktuellen NVIDIA-Treiber + Docker Desktop (WSL2) installieren. Linux: Treiber + "
            "NVIDIA Container Toolkit. Ohne GPU läuft alles, aber deutlich langsamer.",
        )
    r = _run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"])
    if r.returncode != 0 or not (r.stdout or "").strip():
        return Check(
            "NVIDIA-GPU",
            "warn",
            "nvidia-smi meldet einen Fehler",
            f"Treiber prüfen ({(r.stderr or '').strip()[:120] or 'keine Ausgabe'}).",
        )
    return Check("NVIDIA-GPU", "ok", r.stdout.strip().splitlines()[0])


def check_fonts(settings: Settings) -> list[Check]:
    brand = settings.config.brand
    fonts_dir = settings.config.paths.fonts_dir
    out = []
    for label, name in (("Schrift Captions", brand.font_captions), ("Schrift Hook", brand.font_hook)):
        p = fonts_dir / name
        if p.is_file():
            out.append(Check(label, "ok", name))
        else:
            out.append(
                Check(
                    label,
                    "error",
                    f"{p} fehlt",
                    f"Die Schriftdatei nach {fonts_dir} kopieren (das Repo enthält ArchivoBlack und Anton).",
                )
            )
    return out


def find_face_model(settings: Settings) -> Path | None:
    """Wie media.faces.find_yunet_model: assets/models (Docker-Image), data/cache/models ('wdvi init'),
    data/cache (ältere Versionen)."""
    for p in settings.config.paths.face_model_paths(YUNET_FILE):
        try:
            if p.is_file() and p.stat().st_size >= MIN_MODEL_BYTES:
                return p
        except OSError:
            continue
    return None


def check_face_model(settings: Settings) -> Check:
    p = find_face_model(settings)
    if p is not None:
        return Check("Gesichtserkennung", "ok", f"YuNet-Modell vorhanden ({p.name})")
    models = settings.config.paths.models_dir
    return Check(
        "Gesichtserkennung",
        "warn",
        "YuNet-Modell fehlt – Fallback auf Haar-Cascade (ungenauer)",
        f"'wdvi init' ausführen (Docker: docker compose run --rm wdvi wdvi init) – lädt das Modell nach "
        f"{models}/. Ohne Internet: {YUNET_FILE} von github.com/opencv/opencv_zoo selbst dorthin kopieren.",
    )


def check_whisper(settings: Settings, full: bool) -> Check:
    w = settings.config.whisper
    if not full:
        return Check("Whisper (Transkription)", "info", f"Modell {w.model} nicht geladen (nur mit --full)")
    from wdvi.media.transcribe import Transcriber

    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "silence.wav"
        r = _run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=300:sample_rate=16000:duration=2",
                str(wav),
            ],
            timeout=60,
        )
        if r.returncode != 0:
            return Check(
                "Whisper (Transkription)", "error", "Test-Audio konnte nicht erzeugt werden", "ffmpeg prüfen."
            )
        try:
            t = Transcriber(settings)
            t.transcribe(wav)
        except Exception as e:
            return Check(
                "Whisper (Transkription)",
                "error",
                f"Modell {w.model} lädt nicht: {str(e)[:200]}",
                "Internetzugang zu huggingface.co prüfen (erster Start lädt ~1,6 GB) oder in config.yaml "
                "whisper.model: small wählen.",
            )
    device = getattr(t, "device", w.device)
    return Check("Whisper (Transkription)", "ok", f"Modell {w.model} geladen ({device})")


def check_music(settings: Settings) -> Check:
    have = all(importlib.util.find_spec(m) is not None for m in ("torch", "transformers"))
    if have:
        return Check(
            "Musik-Erkennung", "ok", "torch + transformers installiert (Modell lädt beim ersten Clip)"
        )
    if settings.config.filters.music_policy == "reject":
        return Check(
            "Musik-Erkennung",
            "warn",
            "nicht installiert – Clips mit Musik werden NICHT erkannt",
            "Docker-Image verwenden (enthält sie) oder pip install '.[music]'. Musik = Copyright-Risiko.",
        )
    return Check("Musik-Erkennung", "info", "nicht installiert (filters.music_policy: allow)")


def check_llm(settings: Settings) -> Check:
    cfg = settings.config.llm
    if cfg.backend == "api":
        if settings.secrets.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY"):
            return Check("Claude (API)", "ok", f"API-Key gesetzt, Modell {cfg.model}, effort {cfg.effort}")
        return Check(
            "Claude (API)",
            "error",
            "ANTHROPIC_API_KEY fehlt",
            "API-Key unter console.anthropic.com erstellen und in .env eintragen – oder "
            "llm.backend: cli mit Claude-Abo nutzen (docs/SETUP.md → Claude).",
        )
    exe = _which(cfg.cli_path) or cfg.cli_path
    try:
        r = _run([exe, "--version"], timeout=30)
        ok = r.returncode == 0
        version = (r.stdout or r.stderr or "").strip().splitlines()[0] if (r.stdout or r.stderr) else ""
    except (OSError, subprocess.SubprocessError) as e:
        ok, version = False, str(e)
    if not ok:
        return Check(
            "Claude (CLI)",
            "error",
            f"'{cfg.cli_path} --version' funktioniert nicht",
            "Docker-Image mit WITH_CLAUDE_CLI=1 bauen (in .env setzen, dann 'docker compose build') und "
            "einmal einloggen: 'docker compose run --rm wdvi claude' (docs/SETUP.md → Claude).",
        )
    login = (
        "Token per CLAUDE_CODE_OAUTH_TOKEN"
        if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
        else "Login wird beim ersten Aufruf geprüft"
    )
    return Check("Claude (CLI)", "ok", f"{version[:60]} – {login}")


def check_sources(settings: Settings, sources: dict | None = None) -> list[Check]:
    if sources is None:
        from wdvi.sources.base import build_sources

        sources = {s.name: s for s in build_sources(settings)}
    cfg = settings.config.sources
    out: list[Check] = []
    for name in ("twitch", "kick", "youtube", "reddit"):
        label = f"Quelle {SOURCE_LABELS[name]}"
        if name not in sources:
            out.append(Check(label, "info", "deaktiviert"))
            continue
        try:
            configured = bool(sources[name].is_configured())
        except Exception as e:
            out.append(Check(label, "error", f"Fehler: {str(e)[:150]}", SOURCE_HINTS[name]))
            continue
        if not configured:
            out.append(Check(label, "error", "nicht konfiguriert", SOURCE_HINTS[name]))
            continue
        detail = "konfiguriert"
        if name == "twitch":
            detail += f" (Spiele: {', '.join(cfg.twitch.game_names) or '–'})"
        elif name == "reddit":
            mode = "OAuth" if settings.secrets.reddit_client_id else "ohne Login (öffentliches JSON)"
            detail += f" (r/{', r/'.join(cfg.reddit.subreddits)}, {mode})"
        elif name == "youtube":
            detail += f" ({len(cfg.youtube.channel_ids)} Kanäle)"
            if cfg.youtube.search_queries and not settings.secrets.youtube_api_key:
                out.append(
                    Check(
                        label,
                        "warn",
                        "Suchanfragen eingetragen, aber kein WDVI_YOUTUBE_API_KEY",
                        "API-Key in .env setzen oder search_queries leeren.",
                    )
                )
                continue
        out.append(Check(label, "ok", detail))
    return out


def check_publishers(settings: Settings, publishers: dict | None = None) -> list[Check]:
    if publishers is None:
        from wdvi.publish.base import build_publishers

        publishers = dict(build_publishers(settings))
    out: list[Check] = []
    base = settings.config.dashboard.public_base_url.rstrip("/")
    for name in ("youtube", "tiktok", "instagram"):
        label = f"Posten auf {PLATFORM_LABELS[name]}"
        pub = publishers.get(name)
        if pub is None:
            out.append(Check(label, "info", "deaktiviert"))
            continue
        try:
            if not pub.is_configured():
                out.append(Check(label, "error", "App-Zugangsdaten fehlen", PUBLISH_HINTS[name]))
                continue
            connected = bool(pub.is_connected())
        except Exception as e:
            out.append(Check(label, "error", f"Fehler: {str(e)[:150]}", PUBLISH_HINTS[name]))
            continue
        if not connected:
            out.append(
                Check(
                    label,
                    "warn",
                    "noch nicht verbunden",
                    f"Im Dashboard {base}/connections auf „Verbinden“ klicken (oder 'wdvi auth {name}').",
                )
            )
            continue
        detail = "verbunden"
        if name == "tiktok":
            detail += f" (Modus {settings.config.publish.tiktok.mode})"
        if name == "youtube":
            detail += " – öffentliche Uploads erst nach bestandenem YouTube-API-Audit"
        out.append(Check(label, "ok", detail))
    if not publishers:
        out.append(
            Check(
                "Posten",
                "warn",
                "keine Plattform aktiviert",
                "In config.yaml publish.youtube/tiktok/instagram.enabled: true setzen.",
            )
        )
    return out


def effective_host(settings: Settings) -> str:
    return os.environ.get("WDVI_HOST") or settings.config.dashboard.host


def check_dashboard(settings: Settings) -> Check:
    host = effective_host(settings)
    pw = settings.secrets.dashboard_password
    port = settings.config.dashboard.port
    if host not in LOCAL_HOSTS and not pw:
        return Check(
            "Dashboard",
            "error",
            f"lauscht auf {host}:{port}, aber WDVI_DASHBOARD_PASSWORD fehlt",
            "Ein langes Passwort in .env setzen (WDVI_DASHBOARD_PASSWORD=…). Im Docker-Betrieb Pflicht.",
        )
    if pw and len(pw) < 10:
        return Check(
            "Dashboard",
            "warn",
            f"{host}:{port}, Passwort ist sehr kurz",
            "Mindestens 10–12 Zeichen verwenden.",
        )
    return Check("Dashboard", "ok", f"{host}:{port}" + (" mit Passwort" if pw else " (nur lokal)"))


def check_storage(settings: Settings) -> Check:
    paths = settings.config.paths
    try:
        paths.ensure()
        probe = paths.data_dir / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        return Check(
            "Datenordner",
            "error",
            f"{paths.data_dir} nicht beschreibbar ({e})",
            "Ordnerrechte prüfen (Docker: ./data gehört dem Benutzer mit UID 1000).",
        )
    free_gb = shutil.disk_usage(paths.data_dir).free / 1e9
    if free_gb < 10:
        return Check(
            "Datenordner",
            "warn",
            f"{paths.data_dir} – nur noch {free_gb:.1f} GB frei",
            "Platz schaffen: Whisper-Modell (~1,6 GB) + Downloads + fertige Clips brauchen Speicher.",
        )
    return Check("Datenordner", "ok", f"{paths.data_dir} beschreibbar, {free_gb:.0f} GB frei")


def check_database(settings: Settings) -> Check:
    from wdvi import db

    try:
        settings.config.paths.ensure()
        db.init_engine(settings.config.paths.db_path)
    except Exception as e:
        return Check(
            "Datenbank",
            "error",
            f"{settings.config.paths.db_path}: {str(e)[:150]}",
            "Datenordner prüfen; ggf. beschädigte wdvi.db sichern und löschen.",
        )
    return Check("Datenbank", "ok", str(settings.config.paths.db_path))


# --------------------------------------------------------------------------- alle Checks


def run_checks(
    settings: Settings,
    *,
    full: bool = False,
    config_path: Path | None = None,
    sources: dict | None = None,
    publishers: dict | None = None,
) -> list[Check]:
    steps: list[tuple[str, Callable[[], Check | list[Check]]]] = [
        ("Konfiguration", lambda: check_config(config_path)),
        ("Datenordner", lambda: check_storage(settings)),
        ("Datenbank", lambda: check_database(settings)),
        ("ffmpeg", check_ffmpeg),
        ("NVIDIA-GPU", check_gpu),
        ("Video-Encoder", lambda: check_encoder(settings)),
        ("Schriften", lambda: check_fonts(settings)),
        ("Gesichtserkennung", lambda: check_face_model(settings)),
        ("Whisper (Transkription)", lambda: check_whisper(settings, full)),
        ("Musik-Erkennung", lambda: check_music(settings)),
        ("Claude", lambda: check_llm(settings)),
        ("Quellen", lambda: check_sources(settings, sources)),
        ("Posten", lambda: check_publishers(settings, publishers)),
        ("Dashboard", lambda: check_dashboard(settings)),
    ]
    out: list[Check] = []
    for name, fn in steps:
        try:
            res = fn()
        except Exception as e:  # ein kaputter Check darf den Rest nicht verhindern
            log.debug("doctor check %s crashed", name, exc_info=True)
            res = Check(name, "error", f"Prüfung fehlgeschlagen: {type(e).__name__}: {str(e)[:150]}")
        out.extend(res if isinstance(res, list) else [res])
    return out
