"""Review-Dashboard (FastAPI + Jinja2, ohne JS-Framework).

Seiten: /  Review-Queue (Video-Vorschau, Score, Begründung, editierbare Titel/Beschreibungen je
Plattform, Plattform-Checkboxen, Freigeben/Ablehnen) · /scheduled (inkl. freigegebener Clips ohne Slot) ·
/posted (mit Metriken, TikTok-Inbox-Caption zum Kopieren, fehlgeschlagene Uploads neu einplanen) ·
/candidates (Pipeline-Status & Ablehnungsgründe) · /creators (Block/Allow-Liste) · /connections
(OAuth-Verbinden je Plattform, Token-Status, "Redirect-URL einfügen"-Fallback) · /health (JobRuns,
Konfig-Check). Medien unter /media/clips/... Auth: HTTP Basic (User beliebig, Passwort
WDVI_DASHBOARD_PASSWORD); ohne Passwort nur von 127.0.0.1 erreichbar.
"""

from __future__ import annotations

import logging
import math
import shutil
import traceback
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import jinja2
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData
from starlette.exceptions import HTTPException as StarletteHTTPException

from wdvi.config import Settings
from wdvi.dashboard import _data, _fmt
from wdvi.dashboard._security import SecurityMiddleware
from wdvi.dashboard._state import FlashStore, OAuthStates, parse_pasted_redirect

log = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
TEMPLATES_DIR = HERE / "templates"
STATIC_DIR = HERE / "static"
PAGE_SIZE = 10

NO_PIPELINE = (
    "Das geht gerade nicht: Das Dashboard läuft ohne Pipeline (Worker). "
    "Bitte WastedDailyVI komplett starten (wdvi run)."
)
STATE_ERROR = (
    "Ungültiger oder abgelaufener Anmeldevorgang. Bitte unter „Verbindungen“ erneut auf „Verbinden“ "
    "tippen und die Anmeldung innerhalb von 15 Minuten abschließen."
)
SETUP_HINTS = {
    "youtube": "WDVI_GOOGLE_CLIENT_SECRETS_FILE (Pfad zur client_secret.json, OAuth-Client „Webanwendung“)",
    "tiktok": "WDVI_TIKTOK_CLIENT_KEY und WDVI_TIKTOK_CLIENT_SECRET",
    "instagram": "WDVI_META_APP_ID und WDVI_META_APP_SECRET (Instagram-App-ID/-Secret)",
}
REJECT_REASONS = ["Langweilig", "Schlechter Schnitt", "Schon gepostet", "Kein GTA 6", "Captions falsch"]
ERROR_TEXTS = {
    404: "Diese Seite gibt es nicht.",
    405: "Diese Aktion geht nur über die Buttons im Dashboard.",
}
HTTP_DEFAULT_DETAILS = {"Not Found", "Method Not Allowed", "Internal Server Error", "Bad Request"}


def _static_version() -> str:
    try:
        return str(int(max(p.stat().st_mtime for p in STATIC_DIR.iterdir() if p.is_file())))
    except (OSError, ValueError):
        return "1"


def _resolve_publishers(settings: Settings, pipeline: Any, publishers: dict | None) -> dict:
    """Explizit übergebene Publisher > pipeline.publishers > aus der Konfiguration gebaut."""
    if publishers is not None:
        return dict(publishers)
    pubs = getattr(pipeline, "publishers", None) if pipeline is not None else None
    if isinstance(pubs, dict):
        return dict(pubs)
    try:
        from wdvi.publish import base

        return dict(base.build_publishers(settings))
    except Exception as e:  # noqa: BLE001 - Dashboard soll trotzdem starten
        log.warning("Could not build publishers for dashboard: %s", _fmt.redact(str(e)))
        return {}


def _safe_next(value: Any, default: str) -> str:
    """Nur lokale Pfade als Rücksprungziel (kein offener Redirect)."""
    v = str(value or "").strip()
    if not v.startswith("/") or v.startswith("//") or v.startswith("/\\") or "\n" in v or "\r" in v:
        return default
    return v


def _form_str(form: FormData, key: str) -> str:
    v = form.get(key)
    return v if isinstance(v, str) else ""


def _err_text(e: BaseException) -> str:
    return _fmt.redact(str(e)).strip() or type(e).__name__


class _Dashboard:
    """Gemeinsamer Zustand + Aktionen (synchron; werden im Threadpool ausgeführt)."""

    def __init__(self, settings: Settings, pipeline: Any, publishers: dict) -> None:
        self.settings = settings
        self.pipeline = pipeline
        self.publishers = publishers
        self.tz_name = settings.config.publish.timezone
        self.tz, self.tz_ok = _fmt.load_tz(self.tz_name)
        self.clips_dir = Path(settings.config.paths.clips_dir).resolve()
        self.states = OAuthStates()
        self.flash = FlashStore()
        self._cancel_pipeline: Any = None
        order = list(_data.ALL_PLATFORMS)
        if publishers:
            self.platforms = [p for p in order if p in publishers] + [p for p in publishers if p not in order]
        else:
            pub_cfg = settings.config.publish
            self.platforms = [p for p in order if getattr(pub_cfg, p).enabled]

    # ------------------------------------------------------------------ Publisher-Status

    def publisher(self, platform: str) -> Any:
        pub = self.publishers.get(platform)
        if pub is None:
            raise HTTPException(status_code=404, detail=f"Plattform „{platform}“ ist nicht aktiv.")
        return pub

    @staticmethod
    def _call_bool(fn: Callable[[], Any] | None) -> tuple[bool, str]:
        if fn is None:
            return False, ""
        try:
            return bool(fn()), ""
        except Exception as e:  # noqa: BLE001 - Statusanzeige darf nie crashen
            return False, _err_text(e)

    def connected_flags(self) -> dict[str, bool]:
        return {p: self._call_bool(getattr(pub, "is_connected", None))[0] for p, pub in self.publishers.items()}

    def connections(self) -> list[dict]:
        out = []
        for p in self.platforms:
            pub = self.publishers.get(p)
            if pub is None:
                continue
            configured, err1 = self._call_bool(getattr(pub, "is_configured", None))
            connected, err2 = self._call_bool(getattr(pub, "is_connected", None))
            redirect = ""
            fn = getattr(pub, "redirect_uri", None)
            if callable(fn):
                try:
                    redirect = str(fn())
                except Exception:  # noqa: BLE001
                    redirect = ""
            out.append(
                {
                    "platform": p,
                    "name": _data.platform_name(p),
                    "configured": configured,
                    "connected": connected,
                    "error": err1 or err2,
                    "redirect_uri": redirect,
                    "https_only": p in ("tiktok", "instagram"),
                    "can_disconnect": callable(getattr(pub, "disconnect", None)),
                    "setup_hint": SETUP_HINTS.get(p, ""),
                }
            )
        return out

    # ------------------------------------------------------------------ Review-Aktionen

    def approve(self, clip_id: int, platforms: list[str], titles: dict[str, str],
                descriptions: dict[str, str]) -> tuple[bool, str]:
        if self.pipeline is None:
            return False, NO_PIPELINE
        status = _data.get_clip_status(clip_id)
        if status is None:
            return False, f"Clip #{clip_id} nicht gefunden."
        if status != "pending_review":
            return False, f"Clip #{clip_id} wurde schon bearbeitet (Status: {_data.CLIP_STATUS.get(status, status)})."
        if not platforms:
            return False, "Bitte mindestens eine Plattform auswählen."
        if "youtube" in platforms and "youtube" in titles and not titles["youtube"]:
            return False, "Für YouTube wird ein Titel gebraucht."
        try:
            self.pipeline.approve_clip(clip_id, platforms, titles, descriptions)
        except Exception as e:  # noqa: BLE001 - Fehler dem Nutzer zeigen statt 500
            log.exception("approve_clip(%s) failed", clip_id)
            return False, f"Freigabe fehlgeschlagen: {_err_text(e)}"
        names = ", ".join(_data.platform_name(p) for p in platforms)
        log.info("Clip %s approved via dashboard for %s", clip_id, ",".join(platforms))
        return True, f"Clip #{clip_id} freigegeben für {names} – wird automatisch eingeplant."

    def reject(self, clip_id: int, reason: str | None) -> tuple[bool, str]:
        if self.pipeline is None:
            return False, NO_PIPELINE
        status = _data.get_clip_status(clip_id)
        if status is None:
            return False, f"Clip #{clip_id} nicht gefunden."
        if status != "pending_review":
            return False, f"Clip #{clip_id} wurde schon bearbeitet (Status: {_data.CLIP_STATUS.get(status, status)})."
        try:
            self.pipeline.reject_clip(clip_id, reason)
        except Exception as e:  # noqa: BLE001
            log.exception("reject_clip(%s) failed", clip_id)
            return False, f"Ablehnen fehlgeschlagen: {_err_text(e)}"
        log.info("Clip %s rejected via dashboard", clip_id)
        return True, f"Clip #{clip_id} abgelehnt."

    def save_texts(self, clip_id: int, titles: dict[str, str], descriptions: dict[str, str]) -> tuple[bool, str]:
        err = _data.save_clip_texts(clip_id, titles, descriptions, self.settings)
        return (False, err) if err else (True, "Texte gespeichert.")

    def cancel_post(self, post_id: int) -> tuple[bool, str]:
        """Geplanten Post abbrechen – immer über Pipeline.cancel_post (zieht bei YouTube schon geplante Videos
        per unschedule() zurück und zieht den Clip-Status nach). Hat die übergebene Pipeline das nicht
        (Tests/Dashboard allein), eine mit den Dashboard-Publishern bauen."""
        fn = getattr(self.pipeline, "cancel_post", None) if self.pipeline is not None else None
        if not callable(fn):
            if self._cancel_pipeline is None:
                from wdvi.pipeline import Pipeline

                self._cancel_pipeline = Pipeline(self.settings, publishers=self.publishers)
            fn = self._cancel_pipeline.cancel_post
        try:
            ok, msg = fn(post_id)
        except Exception as e:  # noqa: BLE001 - Fehler dem Nutzer zeigen statt 500
            log.exception("cancel_post(%s) failed", post_id)
            return False, f"Abbrechen fehlgeschlagen: {_err_text(e)}"
        if ok:
            log.info("Post %s canceled via dashboard", post_id)
        return ok, msg

    def requeue(self, clip_id: int) -> tuple[bool, str]:
        """Fehlgeschlagene Posts eines Clips neu einplanen (wie `wdvi requeue`)."""
        fn = getattr(self.pipeline, "requeue_clip", None) if self.pipeline is not None else None
        if not callable(fn):
            return False, NO_PIPELINE
        try:
            planned = fn(clip_id)
        except Exception as e:  # noqa: BLE001 - z. B. Video gelöscht, Creator blockiert
            log.warning("requeue_clip(%s) failed: %s", clip_id, _err_text(e))
            return False, f"Neu einplanen fehlgeschlagen: {_err_text(e)}"
        when = ", ".join(
            f"{_data.platform_name(p)} {_fmt.fmt_local(dt, self.tz)}" for p, dt in (planned or [])
        )
        log.info("Clip %s requeued via dashboard", clip_id)
        return True, f"Clip #{clip_id} neu eingeplant" + (f": {when}." if when else ".")

    # ------------------------------------------------------------------ OAuth

    def finish_oauth(self, platform: str, params: dict[str, str]) -> tuple[bool, str]:
        pub = self.publisher(platform)
        name = _data.platform_name(platform)
        if not self.states.consume(platform, params.get("state")):
            log.warning("OAuth callback for %s with unknown/expired state", platform)
            return False, STATE_ERROR
        try:
            pub.handle_callback(params)
        except Exception as e:  # noqa: BLE001
            log.warning("OAuth callback for %s failed: %s", platform, _err_text(e))
            return False, f"{name}-Verbindung fehlgeschlagen: {_err_text(e)}"
        log.info("%s connected via dashboard", platform)
        return True, f"{name} ist jetzt verbunden ✓"

    def store_token(self, platform: str, params: dict[str, str]) -> tuple[bool, str]:
        pub = self.publisher(platform)
        name = _data.platform_name(platform)
        try:
            pub.handle_callback(params)
        except Exception as e:  # noqa: BLE001
            log.warning("Manual token for %s rejected: %s", platform, _err_text(e))
            return False, f"{name}-Token nicht gespeichert: {_err_text(e)}"
        return True, f"{name}-Token gespeichert ✓"

    def disconnect(self, platform: str) -> tuple[bool, str]:
        pub = self.publisher(platform)
        name = _data.platform_name(platform)
        fn = getattr(pub, "disconnect", None)
        if not callable(fn):
            return False, f"{name} kann hier nicht getrennt werden."
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            return False, f"{name} trennen fehlgeschlagen: {_err_text(e)}"
        return True, f"{name} getrennt."

    # ------------------------------------------------------------------ Health

    def health(self) -> dict[str, Any]:
        cfg = self.settings.config
        data_dir = Path(cfg.paths.data_dir)
        disk = None
        try:
            du = shutil.disk_usage(data_dir if data_dir.exists() else Path.cwd())
            disk = {"free_gb": du.free / 1e9, "total_gb": du.total / 1e9, "low": du.free < 10e9}
        except OSError:
            pass
        return {
            "jobs": _data.latest_jobs(self.settings),
            "counts": _data.status_counts(),
            "secrets": _data.secrets_present(self.settings),
            "llm": {"backend": cfg.llm.backend, "model": cfg.llm.model, "effort": cfg.llm.effort},
            "encoder": cfg.render.encoder,
            "ffmpeg": bool(shutil.which("ffmpeg")),
            "nvidia_smi": bool(shutil.which("nvidia-smi")),
            "password_set": bool(self.settings.secrets.dashboard_password),
            "pipeline": self.pipeline is not None,
            "require_approval": cfg.publish.require_approval,
            "timezone": self.tz_name,
            "tz_ok": self.tz_ok,
            "disk": disk,
            "platforms": [
                {"name": c["name"], "configured": c["configured"], "connected": c["connected"]}
                for c in self.connections()
            ],
        }


def _build_templates(dash: _Dashboard, settings: Settings) -> Jinja2Templates:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=True,  # alles escapen – Daten kommen aus fremden APIs
        trim_blocks=True,
        lstrip_blocks=True,
    )
    tz = dash.tz
    env.filters.update(
        ago=_fmt.ago,
        local=lambda dt, with_day=True: _fmt.fmt_local(dt, tz, with_day),
        day=lambda dt: _fmt.fmt_day(dt, tz),
        num=_fmt.fmt_int,
        dur=_fmt.fmt_duration,
        safe_url=_fmt.safe_url,
        short_host=_fmt.short_host,
        score_class=_fmt.score_class,
        iso=lambda dt: (_fmt.as_utc(dt).isoformat() if dt else ""),
    )
    env.globals.update(
        brand=settings.config.brand,
        platform_name=_data.platform_name,
        tz_name=dash.tz_name,
        tz_label=_fmt.tz_label(dash.tz_name),
        static_v=_static_version(),
        candidate_status=_data.CANDIDATE_STATUS,
        clip_status=_data.CLIP_STATUS,
        post_status=_data.POST_STATUS,
    )
    return Jinja2Templates(env=env)


def create_app(settings: Settings, pipeline=None, publishers=None) -> FastAPI:
    pubs = _resolve_publishers(settings, pipeline, publishers)
    dash = _Dashboard(settings, pipeline, pubs)
    templates = _build_templates(dash, settings)

    app = FastAPI(title="WastedDailyVI Dashboard", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.dashboard = dash

    public_host = urlsplit(settings.config.dashboard.public_base_url.strip()).netloc
    app.add_middleware(
        SecurityMiddleware,
        password=lambda: settings.secrets.dashboard_password,
        extra_hosts=[public_host] if public_host else [],
    )

    # Medien (nur clips_dir; StaticFiles blockt Pfad-Tricks und kann HTTP-Range für Safari)
    try:
        dash.clips_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.warning("Cannot create clips dir %s: %s", dash.clips_dir, e)
    app.mount("/media/clips", StaticFiles(directory=dash.clips_dir, check_dir=False), name="media")
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    fonts_dir = Path(settings.config.paths.fonts_dir)
    if fonts_dir.is_dir():
        app.mount("/fonts", StaticFiles(directory=fonts_dir), name="fonts")

    # ------------------------------------------------------------------ Helfer

    def render(request: Request, name: str, active: str, status_code: int = 200, **context: Any) -> Response:
        flash = dash.flash.pop(request.query_params.get("flash"))
        try:
            pending = _data.pending_count()
        except Exception:  # noqa: BLE001 - Navigation soll auch bei DB-Problemen rendern
            log.exception("pending_count failed")
            pending = None
        ctx = {
            "active": active,
            "flash": flash,
            "pending_count": pending,
            "pipeline_ok": dash.pipeline is not None,
            # TikTok-Inbox: Entwurf ohne Text -> Caption muss von Hand in die App
            "tiktok_inbox": settings.config.publish.tiktok.mode == "inbox",
            **context,
        }
        return templates.TemplateResponse(request, name, ctx, status_code=status_code)

    def is_ajax(request: Request) -> bool:
        return request.headers.get("x-wdvi-ajax") == "1"

    def flash_redirect(back: str, ok: bool, message: str) -> RedirectResponse:
        token = dash.flash.add("ok" if ok else "error", message)
        sep = "&" if "?" in back else "?"
        return RedirectResponse(f"{back}{sep}flash={token}", status_code=303)

    def done(request: Request, ok: bool, message: str, back: str, **extra: Any) -> Response:
        if is_ajax(request):
            return JSONResponse({"ok": ok, "message": message, **extra}, status_code=200 if ok else 400)
        return flash_redirect(back, ok, message)

    def texts_from_form(form: FormData) -> tuple[dict[str, str], dict[str, str]]:
        titles, descs = {}, {}
        for p in dash.platforms:
            if f"title_{p}" in form:
                titles[p] = _data.text_limit(_form_str(form, f"title_{p}")).replace("\n", " ")
            if f"desc_{p}" in form:
                descs[p] = _data.text_limit(_form_str(form, f"desc_{p}"))
        return titles, descs

    async def pending_after() -> int | None:
        try:
            return await run_in_threadpool(_data.pending_count)
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------ Fehlerseiten

    async def error_page(request: Request, code: int, detail: str, headers: dict | None = None) -> Response:
        path = request.url.path
        if is_ajax(request):
            return JSONResponse({"ok": False, "message": detail}, status_code=code, headers=headers)
        if path.startswith(("/media/", "/static/", "/fonts/")) or request.method not in ("GET", "HEAD"):
            return PlainTextResponse(detail, status_code=code, headers=headers)
        resp = await run_in_threadpool(render, request, "error.html", "", code, code=code, detail=detail)
        if headers:
            resp.headers.update(headers)
        return resp

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        own = exc.detail if isinstance(exc.detail, str) and exc.detail not in HTTP_DEFAULT_DETAILS else None
        detail = own or ERROR_TEXTS.get(exc.status_code, f"Fehler {exc.status_code}.")
        return await error_page(request, exc.status_code, detail, getattr(exc, "headers", None))

    @app.exception_handler(Exception)
    async def server_error(request: Request, exc: Exception) -> Response:
        # ohne Template/DB, die könnten selbst die Ursache sein
        tb = _fmt.redact("".join(traceback.format_exception(exc)))  # Tokens in URLs nicht ins Log
        log.error("Dashboard error on %s %s:\n%s", request.method, request.url.path, tb)
        text = "Interner Fehler im Dashboard – Details stehen im Log. Bitte Seite neu laden."
        if is_ajax(request):
            return JSONResponse({"ok": False, "message": text}, status_code=500)
        return HTMLResponse(
            '<!doctype html><html lang="de"><head><meta charset="utf-8"><meta name="viewport" '
            'content="width=device-width, initial-scale=1"><title>Fehler</title><link rel="stylesheet" '
            f'href="/static/app.css"></head><body class="plain"><main class="container narrow"><div class="card">'
            f'<h1 class="page-title">Fehler 500</h1><p>{text}</p><p><a class="btn primary" href="/">Zur Review</a>'
            "</p></div></main></body></html>",
            status_code=500,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> Response:
        return await error_page(request, 400, "Ungültige Eingabe (z. B. falsche ID in der Adresse).")

    # ------------------------------------------------------------------ Review

    @app.get("/", response_class=HTMLResponse)
    def review(request: Request, page: int = 1) -> Response:
        page = max(1, page)
        items, total = _data.review_items(dash.platforms, dash.clips_dir, (page - 1) * PAGE_SIZE, PAGE_SIZE)
        if not items and page > 1:  # Seite leer geworden (alles erledigt) -> zurück zur ersten
            flash = request.query_params.get("flash")
            return RedirectResponse("/" + (f"?flash={quote(flash)}" if flash else ""), status_code=303)
        return render(
            request, "review.html", "review",
            items=items, total=total, page=page, pages=max(1, math.ceil(total / PAGE_SIZE)),
            connected=dash.connected_flags(), reject_reasons=REJECT_REASONS,
            platforms=dash.platforms,
        )

    @app.post("/clips/{clip_id}/approve")
    async def approve(request: Request, clip_id: int) -> Response:
        form = await request.form()
        seen: list[str] = []
        for p in form.getlist("platforms"):
            if isinstance(p, str) and p in dash.platforms and p not in seen:
                seen.append(p)
        titles, descs = texts_from_form(form)
        ok, msg = await run_in_threadpool(dash.approve, clip_id, seen, titles, descs)
        return done(request, ok, msg, _safe_next(form.get("next"), "/"), pending=await pending_after())

    @app.post("/clips/{clip_id}/reject")
    async def reject(request: Request, clip_id: int) -> Response:
        form = await request.form()
        reason = _form_str(form, "reason").strip()[:500] or None
        ok, msg = await run_in_threadpool(dash.reject, clip_id, reason)
        return done(request, ok, msg, _safe_next(form.get("next"), "/"), pending=await pending_after())

    @app.post("/clips/{clip_id}/texts")
    async def save_texts(request: Request, clip_id: int) -> Response:
        form = await request.form()
        titles, descs = texts_from_form(form)
        ok, msg = await run_in_threadpool(dash.save_texts, clip_id, titles, descs)
        return done(request, ok, msg, _safe_next(form.get("next"), "/"))

    # ------------------------------------------------------------------ Posts

    @app.get("/scheduled", response_class=HTMLResponse)
    def scheduled(request: Request) -> Response:
        posts = _data.scheduled_posts(dash.clips_dir)
        groups: list[dict] = []
        for p in posts:
            label = _fmt.fmt_day(p["scheduled_at"], dash.tz)
            if not groups or groups[-1]["label"] != label:
                groups.append({"label": label, "posts": []})
            groups[-1]["posts"].append(p)
        return render(request, "scheduled.html", "scheduled", groups=groups, total=len(posts),
                      tz_ok=dash.tz_ok, waiting=_data.approved_waiting(dash.clips_dir))

    @app.post("/posts/{post_id}/cancel")
    async def cancel_post(request: Request, post_id: int) -> Response:
        await request.form()
        ok, msg = await run_in_threadpool(dash.cancel_post, post_id)
        return done(request, ok, msg, "/scheduled")

    @app.get("/posted", response_class=HTMLResponse)
    def posted(request: Request) -> Response:
        posts = _data.posted_posts(dash.clips_dir)
        views = [p["metrics"]["views"] for p in posts if p["metrics"] and p["metrics"]["views"] is not None]
        summary = {
            "count": len(posts),
            "views": sum(views),
            "avg": (sum(views) / len(views)) if views else None,
            "best": max(views) if views else None,
        }
        return render(request, "posted.html", "posted", posts=posts, summary=summary,
                      failed=_data.failed_by_clip(dash.clips_dir))

    @app.post("/clips/{clip_id}/requeue")
    async def requeue_clip(request: Request, clip_id: int) -> Response:
        await request.form()
        ok, msg = await run_in_threadpool(dash.requeue, clip_id)
        return done(request, ok, msg, "/scheduled" if ok else "/posted")

    # ------------------------------------------------------------------ Kandidaten

    @app.get("/candidates", response_class=HTMLResponse)
    def candidates(request: Request, status: str | None = None) -> Response:
        status = (status or "").strip()[:40] or None
        counts = _data.candidate_status_counts()
        order = list(_data.CANDIDATE_STATUS) + sorted(k for k in counts if k not in _data.CANDIDATE_STATUS)
        filters = [
            {"status": s, "label": _data.CANDIDATE_STATUS.get(s, s), "count": counts.get(s, 0)}
            for s in order
            if counts.get(s) or s == status
        ]
        return render(request, "candidates.html", "candidates", rows=_data.candidates(status),
                      status=status, filters=filters, total=sum(counts.values()))

    # ------------------------------------------------------------------ Creator-Regeln

    @app.get("/creators", response_class=HTMLResponse)
    def creators(request: Request) -> Response:
        return render(request, "creators.html", "creators", rules=_data.creator_rules(),
                      sources=_data.SOURCE_LABELS, rule_names=_data.RULES)

    @app.post("/creators")
    async def add_creator(request: Request) -> Response:
        form = await request.form()
        ok, msg = await run_in_threadpool(
            _data.upsert_creator_rule,
            _form_str(form, "platform_label").strip(),
            _form_str(form, "handle"),
            _form_str(form, "rule").strip(),
            _form_str(form, "note"),
        )
        return done(request, ok, msg, "/creators")

    @app.post("/creators/{rule_id}/delete")
    async def delete_creator(request: Request, rule_id: int) -> Response:
        await request.form()
        ok, msg = await run_in_threadpool(_data.delete_creator_rule, rule_id)
        return done(request, ok, msg, "/creators")

    # ------------------------------------------------------------------ Verbindungen / OAuth

    @app.get("/connections", response_class=HTMLResponse)
    def connections(request: Request) -> Response:
        return render(request, "connections.html", "connections", conns=dash.connections(),
                      public_base_url=settings.config.dashboard.public_base_url)

    @app.get("/oauth/{platform}/start")
    def oauth_start(platform: str) -> Response:
        pub = dash.publisher(platform)
        name = _data.platform_name(platform)
        state = dash.states.create(platform)
        try:
            url = str(pub.auth_url(state))
        except Exception as e:  # noqa: BLE001
            dash.states.discard(state)
            log.warning("auth_url for %s failed: %s", platform, _err_text(e))
            return flash_redirect("/connections", False, f"{name}: Anmeldung kann nicht starten – {_err_text(e)}")
        if not _fmt.safe_url(url):
            dash.states.discard(state)
            return flash_redirect("/connections", False, f"{name}: ungültige Anmelde-URL vom Publisher.")
        return RedirectResponse(url, status_code=303)

    @app.get("/oauth/{platform}/callback")
    def oauth_callback(request: Request, platform: str) -> Response:
        dash.publisher(platform)
        ok, msg = dash.finish_oauth(platform, dict(request.query_params))
        return flash_redirect("/connections", ok, msg)

    @app.post("/oauth/instagram/token")
    async def instagram_token(request: Request) -> Response:
        dash.publisher("instagram")
        form = await request.form()
        token = _form_str(form, "access_token").strip()
        user_id = _form_str(form, "user_id").strip()
        if not token:
            return done(request, False, "Bitte ein Instagram-Access-Token einfügen.", "/connections")
        if user_id and not user_id.isdigit():
            return done(request, False, "Die Instagram-User-ID besteht nur aus Ziffern.", "/connections")
        params = {"access_token": token, **({"user_id": user_id} if user_id else {})}
        ok, msg = await run_in_threadpool(dash.store_token, "instagram", params)
        return done(request, ok, msg, "/connections")

    @app.post("/oauth/{platform}/paste")
    async def oauth_paste(request: Request, platform: str) -> Response:
        dash.publisher(platform)
        form = await request.form()
        params = parse_pasted_redirect(_form_str(form, "url"))
        if not params.get("code") and not params.get("error"):
            return done(
                request, False,
                "In der eingefügten Adresse steht kein „code=“. Bitte die KOMPLETTE Adresse aus der "
                "Adresszeile der Fehlerseite kopieren (beginnt mit https://…/oauth/…/callback?code=…).",
                "/connections",
            )
        ok, msg = await run_in_threadpool(dash.finish_oauth, platform, params)
        return done(request, ok, msg, "/connections")

    @app.post("/oauth/{platform}/disconnect")
    async def oauth_disconnect(request: Request, platform: str) -> Response:
        dash.publisher(platform)
        await request.form()
        ok, msg = await run_in_threadpool(dash.disconnect, platform)
        return done(request, ok, msg, "/connections")

    # ------------------------------------------------------------------ Health

    @app.get("/health", response_class=HTMLResponse)
    def health(request: Request) -> Response:
        return render(request, "health.html", "health", h=dash.health(), now=datetime.now(UTC))

    return app
