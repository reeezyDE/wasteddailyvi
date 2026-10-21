"""Kommandozeile: `wdvi run | init | doctor | discover | process | process-url | requeue | render-demo | auth`."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from wdvi.config import REPO_ROOT, Settings

app = typer.Typer(
    help="WastedDailyVI – virale GTA-6-Clips, vollautomatisch.", no_args_is_help=True, add_completion=False
)

log = logging.getLogger(__name__)

YUNET_FILE = "face_detection_yunet_2023mar.onnx"
YUNET_URL = (
    "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/"
    "face_detection_yunet_2023mar.onnx"
)
YUNET_MIRROR = (
    "https://huggingface.co/opencv/face_detection_yunet/resolve/main/face_detection_yunet_2023mar.onnx"
)
MIN_MODEL_BYTES = 100_000
PLATFORMS = {"youtube": "YouTube", "tiktok": "TikTok", "instagram": "Instagram"}
_LOGIN_RE = re.compile(r"^[A-Za-z0-9_]{2,25}$")


# --------------------------------------------------------------------------- Helfer


def _fail(msg: str, code: int = 1) -> typer.Exit:
    typer.echo(f"❌ {msg}", err=True)
    return typer.Exit(code)


def _settings(ctx: typer.Context) -> Settings:
    """config.yaml + .env laden, Logging einrichten. Verständliche Fehler statt Tracebacks."""
    from pydantic import ValidationError

    from wdvi.config import load_config
    from wdvi.log import setup_logging

    obj = ctx.obj or {}
    setup_logging(obj.get("log_level", "INFO"))
    path = Path(obj.get("config", "config.yaml"))
    if path.is_dir():
        raise _fail(
            f"{path} ist ein Ordner statt einer Datei. Das passiert, wenn Docker die Datei beim Start nicht "
            "fand. Ordner löschen und 'cp config.example.yaml config.yaml' ausführen.",
            2,
        )
    try:
        settings = load_config(path)
    except ValidationError as e:
        lines = [f"  - {'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()[:10]]
        raise _fail("config.yaml ist ungültig:\n" + "\n".join(lines), 2) from e
    except Exception as e:  # YAML-Syntaxfehler, Rechte, ...
        raise _fail(f"config.yaml konnte nicht gelesen werden: {e}", 2) from e
    if settings.unknown_keys:  # Tippfehler/falsche Einrückung würden sonst still ignoriert
        keys = ", ".join(settings.unknown_keys[:10])
        typer.echo(
            f"⚠️  Unbekannte Einträge in {path} werden ignoriert (Tippfehler oder falsch eingerückt?): {keys} – "
            "Schreibweise mit config.example.yaml vergleichen.",
            err=True,
        )
        log.warning("unknown config keys ignored: %s", keys)
    return settings


def _init_db(settings: Settings) -> None:
    from sqlalchemy.exc import OperationalError

    from wdvi import db

    data_dir = settings.config.paths.data_dir
    try:
        settings.config.paths.ensure()
        db.init_engine(settings.config.paths.db_path)
    except (OSError, OperationalError) as e:
        raise _fail(
            f"Datenordner {data_dir} bzw. Datenbank nicht nutzbar ({str(e)[:200]}). Linux/WSL: im "
            f"Projektordner 'sudo chown -R {_owner()} data claude-home' ausführen (Docker: APP_UID in .env = "
            "deine Benutzer-ID, siehe 'id -u'); sonst Speicherplatz und Rechte prüfen.",
            2,
        ) from e


def _owner() -> str:
    """UID:GID dieses Prozesses (im Container = APP_UID) für chown-Hinweise."""
    try:
        return f"{os.getuid()}:{os.getgid()}"
    except AttributeError:  # Windows ohne Docker
        return "1000:1000"


def _job_messages(since: datetime, prefix: str) -> list[tuple[str, bool, str]]:
    from sqlmodel import col, select

    from wdvi import db
    from wdvi.models import JobRun

    with db.session_scope() as s:
        rows = s.exec(
            select(JobRun)
            .where(JobRun.started_at >= since, col(JobRun.job).startswith(prefix))
            .order_by(col(JobRun.started_at).asc())
        ).all()
        return [(r.job, r.ok, r.message or "") for r in rows]


@app.callback()
def main(
    ctx: typer.Context,
    config: Annotated[
        Path, typer.Option("--config", "-c", envvar="WDVI_CONFIG", help="Pfad zur config.yaml.")
    ] = Path("config.yaml"),
    log_level: Annotated[
        str,
        typer.Option("--log-level", envvar="WDVI_LOG_LEVEL", help="Log-Level: DEBUG, INFO, WARNING, ERROR."),
    ] = "INFO",
) -> None:
    """WastedDailyVI – virale GTA-6-Clips finden, schneiden, prüfen lassen und posten."""
    _utf8_console()
    ctx.obj = {"config": config, "log_level": log_level}


def _utf8_console() -> None:
    """Windows-Konsolen (cp1252) stürzen sonst bei Emojis wie ✅ mit UnicodeEncodeError ab."""
    for stream in (sys.stdout, sys.stderr):
        enc = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
        if enc != "utf8" and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


# --------------------------------------------------------------------------- run


@app.command()
def run(
    ctx: typer.Context,
    host: Annotated[
        str | None,
        typer.Option("--host", envvar="WDVI_HOST", help="Adresse fürs Dashboard (Standard: dashboard.host)."),
    ] = None,
    port: Annotated[
        int | None,
        typer.Option("--port", envvar="WDVI_PORT", help="Port fürs Dashboard (Standard: dashboard.port)."),
    ] = None,
    no_scheduler: Annotated[
        bool, typer.Option("--no-scheduler", help="Nur das Dashboard starten, keine Hintergrund-Jobs.")
    ] = False,
) -> None:
    """Dauerbetrieb: Hintergrund-Jobs (Suchen, Schneiden, Posten) + Review-Dashboard."""
    import uvicorn

    from wdvi.dashboard.app import create_app
    from wdvi.pipeline import Pipeline
    from wdvi.publish.base import build_publishers
    from wdvi.scheduler import start_scheduler

    settings = _settings(ctx)
    _init_db(settings)
    h = host or settings.config.dashboard.host
    p = port or settings.config.dashboard.port
    if h not in ("127.0.0.1", "localhost", "::1") and not settings.secrets.dashboard_password:
        log.warning(
            "Dashboard listens on %s without WDVI_DASHBOARD_PASSWORD – only local requests will be allowed", h
        )
    publishers = dict(build_publishers(settings))
    pipeline = Pipeline(settings, publishers=publishers)
    # Dashboard vor dem Scheduler bauen: scheitert das, läuft nichts im Hintergrund weiter
    web = create_app(settings, pipeline, publishers)
    sched = None
    if not no_scheduler:
        # einziger Worker: was noch 'queued'/'processing' ist, hat der vorige Prozess nicht beendet
        n = pipeline.reset_interrupted()
        if n:
            log.info("%d interrupted candidates put back into the queue", n)
        sched = start_scheduler(pipeline, settings)
    typer.echo(f"🚀 WastedDailyVI läuft – Dashboard: {settings.config.dashboard.public_base_url}")
    _keep_sigterm_for_cleanup()
    try:
        uvicorn.run(web, host=h, port=p, log_config=None, access_log=False, proxy_headers=True)
    finally:
        pipeline.stop()
        if sched is not None:
            # laufenden Upload/Kandidaten noch sauber abschließen (Docker: stop_grace_period), statt ihn
            # mitten im Upload abzuwürgen
            log.info("shutting down – waiting for running jobs to finish")
            sched.shutdown(wait=True)


def _keep_sigterm_for_cleanup() -> None:
    """uvicorn fährt bei SIGTERM (docker stop) sauber herunter und löst das Signal danach erneut aus. Mit
    dem Standard-Handler würde der Prozess dann sofort sterben – mitten in einem laufenden Upload. Ein
    eigener Handler sorgt dafür, dass uvicorn.run normal zurückkehrt und die Jobs geordnet enden."""
    import signal
    import threading

    if threading.current_thread() is not threading.main_thread() or not hasattr(signal, "SIGTERM"):
        return

    def _on_term(signum: int, frame: Any) -> None:
        log.info("SIGTERM – finishing running jobs before exit")

    signal.signal(signal.SIGTERM, _on_term)


# --------------------------------------------------------------------------- init


def _find_example() -> Path | None:
    for p in (Path.cwd() / "config.example.yaml", REPO_ROOT / "config.example.yaml"):
        if p.is_file():
            return p
    return None


def download_face_model(dest_dir: Path, url: str = YUNET_URL, timeout: float = 60.0) -> Path:
    """YuNet-ONNX laden (folgt GitHub-Redirects), Größe > 100 kB prüfen. Wirft RuntimeError mit Grund."""
    import httpx

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / YUNET_FILE
    tmp = dest_dir / (YUNET_FILE + ".part")
    try:
        with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(timeout, connect=15.0)) as client:
            with client.stream("GET", url) as r:
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code} von {r.url}")
                with tmp.open("wb") as fh:
                    for chunk in r.iter_bytes():
                        fh.write(chunk)
        size = tmp.stat().st_size
        head = tmp.read_bytes()[:64]
        if (
            size <= MIN_MODEL_BYTES
            or head.startswith(b"version https://git-lfs")
            or head.lstrip().startswith(b"<")
        ):
            raise RuntimeError(f"Download lieferte kein Modell ({size} Bytes)")
        tmp.replace(dest)
        return dest
    except httpx.HTTPError as e:
        raise RuntimeError(f"Netzwerkfehler: {e}") from e
    finally:
        tmp.unlink(missing_ok=True)


def _fetch_face_model(targets: list[Path]) -> tuple[Path | None, list[str]]:
    """Erster beschreibbarer Ordner; je Ordner erst GitHub, dann der Hugging-Face-Spiegel."""
    errors: list[str] = []
    for target in targets:
        for url in (YUNET_URL, YUNET_MIRROR):
            try:
                return download_face_model(target, url), errors
            except OSError as e:  # Ordner nicht beschreibbar -> nächster Ordner
                errors.append(f"{target}: {e}")
                break
            except RuntimeError as e:  # Netzwerk/Server -> nächste Quelle
                errors.append(str(e))
        else:
            return None, errors  # Ordner war ok, aber keine Quelle erreichbar
    return None, errors


@app.command()
def init(ctx: typer.Context) -> None:
    """Ersteinrichtung: config.yaml anlegen, Datenordner erstellen, Gesichts-Modell laden."""
    from wdvi.doctor import find_face_model

    cfg_path = Path((ctx.obj or {}).get("config", "config.yaml"))
    if cfg_path.is_dir():
        raise _fail(
            f"{cfg_path} ist ein Ordner (von Docker angelegt, weil die Datei fehlte). Ordner löschen, dann "
            "'cp config.example.yaml config.yaml' und erneut 'wdvi init'.",
            2,
        )
    if cfg_path.exists():
        typer.echo(f"✅ {cfg_path} existiert bereits – bleibt unverändert.")
    else:
        example = _find_example()
        if example is None:
            typer.echo("⚠️  config.example.yaml nicht gefunden – es gelten die Standardwerte.")
        else:
            try:
                shutil.copyfile(example, cfg_path)
                typer.echo(
                    f"✅ {cfg_path} aus config.example.yaml erstellt – bitte anpassen (Kanäle, Slots …)."
                )
            except OSError as e:
                typer.echo(
                    f"⚠️  {cfg_path} konnte nicht angelegt werden ({e}). Bitte selbst kopieren: "
                    "cp config.example.yaml config.yaml"
                )

    settings = _settings(ctx)
    _init_db(settings)
    typer.echo(
        f"✅ Datenordner bereit: {settings.config.paths.data_dir} (Datenbank {settings.config.paths.db_path})"
    )

    model = find_face_model(settings)
    if model is not None:
        typer.echo(f"✅ Gesichts-Modell vorhanden: {model}")
    else:
        paths = settings.config.paths
        # zuerst data/cache/models: liegt im Docker-Volume ./data und überlebt 'docker compose run --rm'
        path, errors = _fetch_face_model([paths.models_dir, paths.assets_dir / "models"])
        if path is not None:
            typer.echo(f"✅ Gesichts-Modell (YuNet) geladen: {path}")
        else:
            typer.echo(
                f"⚠️  Gesichts-Modell konnte nicht geladen werden ({'; '.join(errors)[:300]}). Es wird die "
                f"ungenauere Haar-Erkennung genutzt. Manuell: {YUNET_URL} nach {paths.models_dir}/ speichern."
            )

    in_docker = Path("/.dockerenv").exists()  # im Container kommen die Secrets per env_file, nicht als Datei
    if not in_docker and not Path(".env").exists() and not settings.secrets.anthropic_api_key:
        typer.echo(
            "⚠️  Keine .env gefunden: 'cp .env.example .env' und Zugangsdaten eintragen (docs/SETUP.md)."
        )
    typer.echo(
        "\nNächste Schritte:\n  1. config.yaml und .env ausfüllen (docs/SETUP.md)\n  2. wdvi doctor\n"
        "  3. wdvi run   (Docker: docker compose up -d)"
    )


# --------------------------------------------------------------------------- doctor


@app.command()
def doctor(
    ctx: typer.Context,
    full: Annotated[
        bool,
        typer.Option("--full", help="Auch das Whisper-Modell laden (dauert; lädt beim ersten Mal ~1,6 GB)."),
    ] = False,
) -> None:
    """Installation prüfen: ffmpeg, GPU, Schriften, Modelle, Claude, Quellen, Plattformen, Dashboard."""
    from wdvi import doctor as doc

    settings = _settings(ctx)
    if str((ctx.obj or {}).get("log_level", "INFO")).upper() == "INFO":
        logging.getLogger().setLevel(logging.WARNING)  # Ausgabe der Checks nicht mit Logs zumüllen
    cfg_path = Path((ctx.obj or {}).get("config", "config.yaml"))
    typer.echo("WastedDailyVI – Systemcheck\n")
    checks = doc.run_checks(settings, full=full, config_path=cfg_path if cfg_path.exists() else None)
    for c in checks:
        typer.echo(c.render())
    errors = sum(c.status == "error" for c in checks)
    warns = sum(c.status == "warn" for c in checks)
    typer.echo("")
    if errors:
        typer.echo(f"❌ {errors} Problem(e) müssen behoben werden, {warns} Warnung(en).")
        raise typer.Exit(1)
    typer.echo("✅ Alles bereit" + (f" ({warns} Warnung(en))." if warns else "."))


# --------------------------------------------------------------------------- discover / process


@app.command()
def discover(
    ctx: typer.Context,
    source: Annotated[
        str | None,
        typer.Option("--source", "-s", help="Nur diese Quelle: twitch, kick, youtube oder reddit."),
    ] = None,
) -> None:
    """Quellen jetzt nach neuen Clips durchsuchen (sonst automatisch im Dauerbetrieb)."""
    from wdvi.pipeline import Pipeline

    settings = _settings(ctx)
    _init_db(settings)
    started = datetime.now(UTC)
    pipeline = Pipeline(settings)
    try:
        n = pipeline.discover(source.strip().lower() if source else None)
    except ValueError as e:
        raise _fail(str(e)) from e
    for job, ok, msg in _job_messages(started, "discover:"):
        typer.echo(f"{'✅' if ok else '❌'} {job}: {msg}")
    typer.echo(f"{n} neue Kandidaten.")


@app.command()
def process(ctx: typer.Context) -> None:
    """Einen Verarbeitungszyklus ausführen (beste neue Kandidaten herunterladen, bewerten, rendern)."""
    from wdvi.pipeline import Pipeline

    settings = _settings(ctx)
    _init_db(settings)
    started = datetime.now(UTC)
    n = Pipeline(settings).process_cycle()
    for _job, ok, msg in _job_messages(started, "process"):
        typer.echo(f"{'✅' if ok else '❌'} {msg}")
    typer.echo(f"{n} neue Clips zur Prüfung.")


# --------------------------------------------------------------------------- process-url


def extract_info(url: str, settings: Settings | None = None) -> dict:
    """yt-dlp-Metadaten ohne Download (mit cookies.txt aus WDVI_YTDLP_COOKIES_FILE, falls gesetzt)."""
    import yt_dlp

    from wdvi.media.download import cookie_file

    opts: dict[str, Any] = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}
    cookies = cookie_file(settings) if settings is not None else None
    if cookies:
        opts["cookiefile"] = cookies
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not isinstance(info, dict):
        raise RuntimeError("yt-dlp lieferte keine Metadaten")
    if info.get("_type") == "playlist":
        entries = [e for e in info.get("entries") or [] if isinstance(e, dict)]
        if not entries:
            raise RuntimeError("Link zeigt auf eine leere Playlist")
        info = entries[0]
    return info


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f >= 0 else None


def _created(info: dict, now: datetime) -> datetime:
    for key in ("timestamp", "release_timestamp"):
        ts = _num(info.get(key))
        if ts:
            return datetime.fromtimestamp(ts, UTC)
    date = str(info.get("upload_date") or "")
    if re.fullmatch(r"\d{8}", date):
        return datetime(int(date[:4]), int(date[4:6]), int(date[6:]), tzinfo=UTC)
    return now


def candidate_from_info(
    info: dict,
    url: str,
    settings: Settings,
    *,
    start: float | None = None,
    end: float | None = None,
    now: datetime | None = None,
):
    """yt-dlp-Info -> CandidateIn (Twitch-/Kick-Clip, YouTube-Video/-Ausschnitt, Reddit-Post)."""
    from wdvi.pipeline import _handle_from_url
    from wdvi.schemas import CandidateIn, SourceKind

    now = now or datetime.now(UTC)
    extractor = str(info.get("extractor_key") or info.get("extractor") or "").lower()
    page = str(info.get("webpage_url") or url)
    title = str(info.get("title") or "").strip() or "Clip"
    duration = _num(info.get("duration"))
    views = info.get("view_count")
    engagement = {
        k: float(v)
        for k, v in (("likes", _num(info.get("like_count"))), ("comments", _num(info.get("comment_count"))))
        if v is not None
    }
    common: dict[str, Any] = dict(
        title=title,
        created_at=_created(info, now),
        view_count=int(views) if isinstance(views, int | float) else None,
        engagement=engagement,
        thumbnail_url=info.get("thumbnail"),
    )

    if "twitch" in extractor:
        if "clip" not in extractor and "/clip" not in page and "clips.twitch.tv" not in page:
            raise ValueError("Bei Twitch werden nur Clips unterstützt (keine VODs oder Livestreams).")
        slug = str(info.get("display_id") or info.get("id") or "").strip()
        channel = str(info.get("channel") or info.get("uploader") or "Twitch-Streamer")
        login = _handle_from_url(page) or _handle_from_url(url)
        if not login and _LOGIN_RE.match(channel):
            login = channel.lower()
        return CandidateIn(
            source=SourceKind.twitch_clip,
            source_id=slug,
            url=page,
            creator_name=channel,
            creator_handle=login,
            creator_url=f"https://twitch.tv/{login}" if login else None,
            platform_label="Twitch",
            duration_s=duration,
            extra={
                "broadcaster_id": str(info.get("channel_id") or ""),
                "broadcaster_login": login,
                "clip_creator_name": info.get("uploader"),
                "manual": True,
            },
            **common,
        )
    if "kick" in extractor:
        clip_id = str(info.get("id") or "").strip()
        slug = str(info.get("channel") or "").strip() or (_handle_from_url(page) or "")
        name = slug or str(info.get("uploader") or "Kick-Streamer")
        return CandidateIn(
            source=SourceKind.kick_clip,
            source_id=clip_id,
            url=page,
            creator_name=name,
            creator_handle=slug.lower() or None,
            creator_url=f"https://kick.com/{slug}" if slug else None,
            platform_label="Kick",
            duration_s=duration,
            extra={
                "channel_id": str(info.get("channel_id") or ""),
                "channel_slug": slug or None,
                "manual": True,
            },
            **common,
        )
    if "youtube" in extractor:
        vid = str(info.get("id") or "").strip()
        channel = str(info.get("channel") or info.get("uploader") or "YouTube-Creator")
        handle = str(info.get("uploader_id") or "").lstrip("@") or None
        base = dict(
            source_id=vid,
            url=f"https://www.youtube.com/watch?v={vid}",
            creator_name=channel,
            creator_handle=handle.lower() if handle else None,
            creator_url=info.get("channel_url") or info.get("uploader_url"),
            platform_label="YouTube",
            language=(str(info.get("language"))[:2].lower() if info.get("language") else None),
            extra={"channel_id": info.get("channel_id"), "manual": True},
            **common,
        )
        f = settings.config.filters
        if start is not None or end is not None:
            s = max(0.0, float(start or 0.0))
            e = float(end) if end is not None else s + f.max_output_duration_s
            if duration:
                e = min(e, duration)
            if e - s < 3:
                raise ValueError("Der Ausschnitt ist zu kurz (--start/--end prüfen).")
            return CandidateIn(
                source=SourceKind.youtube_moment, start_s=s, end_s=e, duration_s=round(e - s, 3), **base
            )
        if duration and duration <= f.max_source_duration_s:
            return CandidateIn(
                source=SourceKind.youtube_moment, start_s=0.0, end_s=duration, duration_s=duration, **base
            )
        return CandidateIn(source=SourceKind.youtube_video, duration_s=duration, **base)
    if "reddit" in extractor:
        post_id = str(info.get("display_id") or info.get("id") or "").strip()
        user = str(info.get("uploader") or "").strip() or "unbekannt"
        return CandidateIn(
            source=SourceKind.reddit_post,
            source_id=post_id,
            url=page,
            creator_name=f"u/{user}",
            creator_handle=user,
            creator_url=f"https://reddit.com/u/{user}",
            platform_label="Reddit",
            duration_s=duration,
            extra={"subreddit": info.get("channel_id"), "author": user, "manual": True},
            **common,
        )
    raise ValueError(
        f"Nicht unterstützte Plattform ({info.get('extractor_key') or 'unbekannt'}). Unterstützt: "
        "Twitch-Clips, Kick-Clips, YouTube, Reddit."
    )


@app.command("process-url")
def process_url(
    ctx: typer.Context,
    url: Annotated[
        str, typer.Argument(help="Link zu einem Twitch-/Kick-Clip, YouTube-Video oder Reddit-Post.")
    ],
    start: Annotated[
        float | None, typer.Option("--start", help="YouTube: Start des Ausschnitts in Sekunden.")
    ] = None,
    end: Annotated[
        float | None, typer.Option("--end", help="YouTube: Ende des Ausschnitts in Sekunden.")
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Auch verarbeiten, wenn es dazu schon einen Clip im Review/freigegeben/eingeplant gibt.",
        ),
    ] = False,
) -> None:
    """Einen bestimmten Clip sofort verarbeiten (Claude bewertet, Render, landet im Review)."""
    from wdvi import db
    from wdvi.models import Clip
    from wdvi.pipeline import Pipeline
    from wdvi.schemas import SourceKind

    settings = _settings(ctx)
    _init_db(settings)
    typer.echo(f"🔎 Lese Metadaten: {url}")
    try:
        info = extract_info(url, settings=settings)
        cand = candidate_from_info(info, url, settings, start=start, end=end)
    except ValueError as e:
        raise _fail(str(e)) from e
    except Exception as e:
        raise _fail(f"Link konnte nicht gelesen werden: {str(e)[:300]}") from e

    pipeline = Pipeline(settings)
    try:  # schon ein Clip im Review/freigegeben/eingeplant -> ablehnen (sonst Duplikat), außer --force
        cid = pipeline.add_candidate(cand, force=force)
    except ValueError as e:
        raise _fail(f"{e} Den vorhandenen Clip findest du im Dashboard.") from e
    if force and (existing := pipeline.open_clips(cand.key)):
        ids = ", ".join(f"#{i}" for i, _ in existing)
        typer.echo(f"⚠️  Es gibt schon Clip {ids} – wegen --force wird trotzdem ein neuer Clip gerendert.")
    if cand.source == SourceKind.youtube_video:
        typer.echo("🎬 Langes YouTube-Video – Claude sucht die besten Momente …")
        found = pipeline.mine_youtube(cid)
        moments = pipeline.moment_ids(cand.key)
        if not found or not moments:
            _status, reason = pipeline.candidate_state(cid)
            raise _fail(f"Keine Momente gefunden: {reason or 'unbekannt'}")
        typer.echo(
            f"   {found} Momente gefunden – verarbeite den besten (Rest folgt automatisch im Dauerbetrieb)."
        )
        cid = moments[0]
    typer.echo("⚙️  Download, Analyse, Claude, Rendern …")
    clip_id = pipeline.process_candidate(cid)
    if clip_id is None:
        status, reason = pipeline.candidate_state(cid)
        raise _fail(f"Kein Clip erzeugt (Status {status}): {reason or 'unbekannt'}")
    with db.session_scope() as s:
        clip = s.get(Clip, clip_id)
        path, score, status = clip.video_path, clip.virality_score, clip.status
    typer.echo(f"✅ Clip #{clip_id} (Score {score}, Status {status}):")
    typer.echo(path)


# --------------------------------------------------------------------------- requeue


@app.command()
def requeue(
    ctx: typer.Context,
    clip_id: Annotated[int, typer.Argument(help="Clip-Nummer (Dashboard: „Clip #…“).")],
) -> None:
    """Fehlgeschlagene oder durch einen Neustart unterbrochene Posts eines Clips neu einplanen."""
    from wdvi.pipeline import Pipeline

    settings = _settings(ctx)
    _init_db(settings)
    try:
        planned = Pipeline(settings).requeue_clip(clip_id)
    except ValueError as e:
        raise _fail(str(e)) from e
    for platform, when in planned:
        typer.echo(f"✅ {PLATFORMS.get(platform, platform)}: neu eingeplant für {when:%d.%m.%Y %H:%M} UTC")
    typer.echo("Läuft der Dauerbetrieb (docker compose up -d), wird zum Termin automatisch hochgeladen.")


# --------------------------------------------------------------------------- render-demo


def _demo_video(path: Path, seconds: float) -> Path:
    """Synthetisches 16:9-Video mit 'Facecam'-Kasten oben links und Ton mit lauter Stelle."""
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size=1920x1080:rate=30:duration={seconds}",
        "-f",
        "lavfi",
        "-i",
        f"testsrc=size=480x270:rate=30:duration={seconds}",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=330:sample_rate=48000:duration={seconds},"
        "volume='if(between(t,6.4,7.2),1.0,0.2)':eval=frame",
        "-filter_complex",
        "[0:v][1:v]overlay=32:32[v]",
        "-map",
        "[v]",
        "-map",
        "2:a",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
        str(path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg konnte das Demo-Video nicht erzeugen: {r.stderr.strip()[-300:]}")
    return path


def demo_inputs(src: Path, seconds: float, layout: str):
    """Analyse + handgeschriebener EditPlan + Kandidat für das Demo (ohne Claude/Whisper)."""
    from wdvi.media import probe as media_probe
    from wdvi.schemas import (
        Box,
        CandidateIn,
        EditPlan,
        Emphasis,
        LayoutHint,
        MediaAnalysis,
        PlatformText,
        Segment,
        SourceKind,
        Transcript,
        Word,
        Zoom,
    )

    info = media_probe.probe(src)
    words, t = [], 0.5
    for w in "no way he just jumped the whole bridge bro that was insane wasted".split():
        words.append(Word(text=w, start=round(t, 2), end=round(t + 0.34, 2)))
        t += 0.62
    transcript = Transcript(
        language="en",
        segments=[
            Segment(
                text=" ".join(x.text for x in words), start=words[0].start, end=words[-1].end, words=words
            )
        ],
    )
    if layout == "facecam_split":
        hint = LayoutHint(
            kind="facecam_split",
            facecam_box=Box(x=32 / 1920, y=32 / 1080, w=480 / 1920, h=270 / 1080),
            gameplay_box=Box(x=0.02, y=0.02, w=0.96, h=0.96),
            confidence=0.9,
        )
    else:
        hint = LayoutHint(kind="gameplay_fit", confidence=0.9)
    analysis = MediaAnalysis(
        info=info, transcript=transcript, music_prob=0.0, speech_ratio=0.5, loudness_peaks=[6.6], layout=hint
    )
    end = min(seconds, info.duration) - 0.2
    pt_title = PlatformText(
        youtube="He jumped the WHOLE bridge 💀 GTA 6",
        tiktok="He jumped the whole bridge 💀",
        instagram="He jumped the whole bridge 💀",
    )
    pt_desc = PlatformText(
        youtube="Render-Test von WastedDailyVI.", tiktok="Render-Test 🔥", instagram="Render-Test 🔥"
    )
    plan = EditPlan(
        keep=True,
        virality_score=88,
        reasoning="Demo-Plan (ohne Claude).",
        trim_start=0.2,
        trim_end=end,
        remove_ranges=[(4.0, 4.5)],
        layout=hint.kind,
        hook_text="HE JUMPED THE WHOLE BRIDGE 💀",
        highlight_words=["insane", "wasted", "bridge"],
        zooms=[
            Zoom(start=2.0, end=3.6, scale=1.25, focus="center"),
            Zoom(start=6.3, end=7.8, scale=1.35, focus="gameplay"),
        ],
        emphasis=[Emphasis(t=2.0, emoji="😳", sfx="whoosh"), Emphasis(t=6.6, emoji="💀", sfx="boom")],
        titles=pt_title,
        descriptions=pt_desc,
        hashtags=["GTA6", "GTAVI", "gaming"],
    )
    cand = CandidateIn(
        source=SourceKind.twitch_clip,
        source_id="WastedDailyRenderDemo",
        url="https://clips.twitch.tv/WastedDailyRenderDemo",
        title="Render-Demo",
        creator_name="WastedDailyVI",
        creator_handle="wasteddailyvi",
        creator_url="https://twitch.tv/wasteddailyvi",
        platform_label="Twitch",
        created_at=datetime.now(UTC),
        duration_s=info.duration,
    )
    return analysis, plan, cand


@app.command("render-demo")
def render_demo(
    ctx: typer.Context,
    layout: Annotated[
        str, typer.Option("--layout", help="gameplay_fit oder facecam_split.")
    ] = "facecam_split",
    seconds: Annotated[
        float, typer.Option("--seconds", min=8.0, max=60.0, help="Länge des Testvideos.")
    ] = 10.0,
) -> None:
    """Testvideo rendern (ohne Claude/Whisper) – prüft ffmpeg, Schriften, Encoder und Captions."""
    from wdvi.render import renderer

    if layout not in ("gameplay_fit", "facecam_split"):
        raise _fail("--layout muss gameplay_fit oder facecam_split sein.", 2)
    settings = _settings(ctx)
    settings.config.paths.ensure()
    work = settings.config.paths.work_dir / "render-demo"
    out = settings.config.paths.clips_dir / "demo"
    typer.echo("🎞️  Erzeuge Testvideo …")
    try:
        src = _demo_video(work / "demo_source.mp4", seconds)
        analysis, plan, cand = demo_inputs(src, seconds, layout)
        typer.echo("⚙️  Rendere 9:16-Short …")
        res = renderer.render_clip(src, analysis, plan, cand, settings, work_dir=work, out_dir=out)
    except Exception as e:
        raise _fail(f"Render-Demo fehlgeschlagen: {str(e)[:500]}") from e
    typer.echo(f"✅ Fertig ({res.width}x{res.height}, {res.duration:.1f} s, Encoder {res.encoder}):")
    typer.echo(res.video_path)


# --------------------------------------------------------------------------- auth


@app.command()
def auth(
    ctx: typer.Context,
    platform: Annotated[str, typer.Argument(help="youtube, tiktok oder instagram.")],
) -> None:
    """Zeigt, wo du ein Plattform-Konto verbindest (OAuth läuft über das Dashboard)."""
    p = platform.strip().lower()
    if p not in PLATFORMS:
        raise _fail("Plattform muss youtube, tiktok oder instagram sein.", 2)
    settings = _settings(ctx)
    base = settings.config.dashboard.public_base_url.rstrip("/")
    typer.echo(f"Öffne im Browser: {base}/connections")
    typer.echo(f"und klicke bei {PLATFORMS[p]} auf „Verbinden“.")
    if p in ("tiktok", "instagram"):
        typer.echo(
            f"Hinweis: {PLATFORMS[p]} verlangt eine https-Redirect-URL. Landest du nach dem Login auf einer "
            "Fehlerseite, kopiere die komplette Adresse aus der Browserzeile und füge sie im Dashboard unter "
            "„Redirect-URL einfügen“ ein."
        )
    elif p == "youtube":
        typer.echo(f"Redirect-URL in der Google Cloud Console: {base}/oauth/youtube/callback")


# --------------------------------------------------------------------------- studio (Claude Code als Editor)

studio_app = typer.Typer(
    help="Studio: Claude Code schneidet selbst (ohne API-Kosten). Ablauf: prepare -> plan.json schreiben -> render.",
    no_args_is_help=True,
)
app.add_typer(studio_app, name="studio")


@studio_app.command("prepare")
def studio_prepare(
    ctx: typer.Context,
    source: Annotated[Path, typer.Argument(help="Video-Datei (MP4, beliebiger Codec).")],
    out: Annotated[Path, typer.Option("--out", "-o", help="Arbeitsordner für diesen Clip.")],
    start: Annotated[float | None, typer.Option("--start", help="Start des Ausschnitts (s).")] = None,
    end: Annotated[float | None, typer.Option("--end", help="Ende des Ausschnitts (s).")] = None,
) -> None:
    """Ausschnitt -> source.mp4, Transkript (transcript.txt), Analyse (analysis.json), Übersicht (sheet.jpg)."""
    from wdvi import studio

    if not source.is_file():
        raise _fail(f"Datei nicht gefunden: {source}", 2)
    settings = _settings(ctx)
    settings.config.paths.ensure()
    typer.echo("⚙️  Schneiden, Whisper, Analyse …")
    p = studio.prepare(source, out, settings, start=start, end=end)
    typer.echo(studio.summary(p))


@studio_app.command("frame")
def studio_frame(
    ctx: typer.Context,
    workdir: Annotated[Path, typer.Argument(help="Arbeitsordner aus 'studio prepare'.")],
    t: Annotated[float, typer.Argument(help="Zeitpunkt in source.mp4 (s).")],
) -> None:
    """Einzelbild mit Koordinatengitter – um Boxen für focus/annotations abzulesen."""
    from wdvi import studio

    _settings(ctx)
    out = studio.grid_still(workdir / "source.mp4", t, workdir / f"frame_{t:.2f}.jpg")
    typer.echo(str(out))


@studio_app.command("render")
def studio_render(
    ctx: typer.Context,
    workdir: Annotated[Path, typer.Argument(help="Arbeitsordner aus 'studio prepare'.")],
    creator: Annotated[str, typer.Option("--creator", help="Name des Creators für das Credit-Label.")],
    platform: Annotated[str, typer.Option("--platform", help="Twitch, YouTube, Kick, …")] = "Twitch",
    plan: Annotated[Path | None, typer.Option("--plan", help="Plan-Datei (Standard: <workdir>/plan.json).")] = None,
    creator_url: Annotated[str | None, typer.Option("--creator-url")] = None,
    facecam: Annotated[str | None, typer.Option("--facecam", help="Facecam-Box überschreiben: x,y,w,h")] = None,
    gameplay: Annotated[str | None, typer.Option("--gameplay", help="Gameplay-Box überschreiben: x,y,w,h")] = None,
    no_watermark: Annotated[bool, typer.Option("--no-watermark", help="Ohne Wasted-Daily-Logo.")] = False,
) -> None:
    """plan.json prüfen und rendern -> out/*.mp4 + preview.jpg (Frames an jedem Shot-Wechsel)."""
    from wdvi import studio

    settings = _settings(ctx)
    settings.config.paths.ensure()
    if no_watermark:
        settings.config.brand.show_watermark = False
    try:
        video, preview, clean = studio.render(workdir, plan or workdir / "plan.json", settings, creator, platform,
                                              creator_url, facecam, gameplay)
    except ValueError as e:
        raise _fail(str(e)) from e
    typer.echo(f"✅ {video}")
    typer.echo(f"   Vorschau: {preview}")
    typer.echo(f"   Lautheit: {studio.loudness(video)} | Shots: {len(clean.shots)} | "
               f"Markierungen: {len(clean.annotations)} | bereinigter Plan: {workdir / 'plan.sanitized.json'}")


if __name__ == "__main__":  # python -m wdvi.cli
    app()
