"""YouTube Data API v3: OAuth (Web-Flow, Redirect {public_base_url}/oauth/youtube/callback, Scopes
youtube.upload + youtube.readonly + youtube.force-ssl), resumable videos.insert. Mit native_schedule:
privacyStatus=private + status.publishAt; unschedule() zieht so ein Video wieder zurück (privat ohne
publishAt, braucht youtube.force-ssl). Titel bekommt " #shorts" nur, wenn noch Platz ist. Metriken via
videos.list(statistics,status); check_status() liefert die aktuelle Sichtbarkeit. Die Pipeline ruft
unschedule() beim Abbrechen/Ablehnen/Blockieren auf und check_status() 10–30 min nach publishAt.
WICHTIG: Uploads aus nicht geprüften API-Projekten sind auf "privat" gesperrt, bis das Projekt den
YouTube-API-Audit bestanden hat – auch ein publishAt macht sie dann nicht öffentlich, und man kann sie
auch im Studio nicht selbst freischalten -> in Fehlermeldung/Doku erwähnen.

Einrichtung (console.cloud.google.com): Projekt anlegen, "YouTube Data API v3" aktivieren, OAuth-Client
vom Typ "Webanwendung" mit autorisierter Redirect-URI {public_base_url}/oauth/youtube/callback
(z. B. http://localhost:8420/oauth/youtube/callback) erstellen, JSON herunterladen und den Pfad in
WDVI_GOOGLE_CLIENT_SECRETS_FILE eintragen. OAuth-Zustimmungsbildschirm auf "In Produktion" stellen –
im Status "Testing" laufen Refresh-Tokens nach 7 Tagen ab. Standard-Quota: 10.000 Einheiten/Tag,
ein Upload kostet ~1.600 -> ca. 6 Uploads/Tag.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import random
import re
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

from wdvi.config import Settings
from wdvi.publish import text
from wdvi.publish._common import as_utc, failed, redact, utcnow
from wdvi.publish.base import PublishError, TokenStore
from wdvi.schemas import PostMetrics, PostRequest, PostResult

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    # videos.update: geplante Videos beim Abbrechen/Ablehnen wieder zurückziehen (unschedule)
    "https://www.googleapis.com/auth/youtube.force-ssl",
]
UPLOAD_SCOPE = SCOPES[0]
MANAGE_SCOPE = SCOPES[2]
CHUNK_SIZE = 8 * 1024 * 1024
MAX_RETRIES = 8
RETRY_STATUSES = {500, 502, 503, 504}
PENDING_TTL_S = 3600  # offene OAuth-Vorgänge (PKCE-Verifier) 1 h aufheben
SCHEDULE_MIN_LEAD = timedelta(minutes=1)  # publishAt muss sicher in der Zukunft liegen
TAGS_MAX_CHARS = 450  # YouTube-Limit 500 (inkl. Trennzeichen/Anführungszeichen)
TAG_MAX_LEN = 60  # einzelnes Keyword (YouTube erlaubt mehr, lange Tags bringen nichts)
_TAG_BAD_RE = re.compile(r"[<>\"',#\x00-\x1f\x7f]")

AUDIT_HINT = (
    "Hinweis: Uploads aus nicht geprüften Google-API-Projekten werden von YouTube auf 'privat' "
    "gesperrt, bis das Projekt den YouTube-API-Audit (Compliance Audit) bestanden hat."
)
# für die Pipeline: nativ geplantes Video ist nach publishAt immer noch privat (check_status == "private")
LOCKED_PRIVATE_HINT = (
    "YouTube hat das Video zum geplanten Zeitpunkt nicht veröffentlicht – es ist weiterhin privat. "
    + AUDIT_HINT
    + " Gesperrte Videos lassen sich auch im YouTube Studio nicht selbst öffentlich stellen."
)

_REASONS = {
    "quotaExceeded": (
        "YouTube-API-Kontingent für heute aufgebraucht (Standard 10.000 Einheiten ≈ 6 Uploads/Tag, Reset um "
        "Mitternacht pazifischer Zeit). Später erneut versuchen oder Quota-Erhöhung beantragen."
    ),
    "dailyLimitExceeded": "YouTube-API-Tageslimit erreicht – morgen erneut versuchen.",
    "uploadLimitExceeded": (
        "YouTube-Upload-Limit des Kanals erreicht (zu viele Uploads in kurzer Zeit) – in einigen Stunden "
        "erneut versuchen."
    ),
    "rateLimitExceeded": "Zu viele Anfragen an die YouTube-API – später erneut versuchen.",
    "userRateLimitExceeded": "Zu viele Anfragen an die YouTube-API – später erneut versuchen.",
    "forbidden": (
        "YouTube hat den Upload verweigert (forbidden). Prüfe, ob der verbundene Account einen YouTube-Kanal "
        "hat und die Upload-Berechtigung erteilt wurde."
    ),
    "insufficientPermissions": (
        "YouTube-Berechtigung fehlt (youtube.upload) – im Dashboard neu verbinden und alle Häkchen setzen."
    ),
    "unauthorized": "YouTube-Anmeldung ungültig oder abgelaufen – im Dashboard neu verbinden.",
    "authError": "YouTube-Anmeldung ungültig oder abgelaufen – im Dashboard neu verbinden.",
    "youtubeSignupRequired": (
        "Das Google-Konto hat noch keinen YouTube-Kanal – zuerst auf youtube.com einen Kanal anlegen."
    ),
    "invalidTitle": "YouTube lehnt den Titel ab (leer, zu lang oder ungültige Zeichen).",
    "invalidDescription": "YouTube lehnt die Beschreibung ab (zu lang oder ungültige Zeichen).",
    "invalidPublishAt": "YouTube lehnt den geplanten Zeitpunkt (publishAt) ab – muss in der Zukunft liegen.",
    "invalidCategoryId": "Ungültige YouTube-Kategorie (publish.youtube.category_id).",
    "invalidTags": "YouTube lehnt die Tags ab.",
    "mediaBodyRequired": "Videodatei fehlt im Upload.",
}

_RETRIABLE_EXC: tuple[type[BaseException], ...] = (OSError, http.client.HTTPException, TimeoutError)
try:  # httplib2 kommt mit google-api-python-client
    import httplib2

    _RETRIABLE_EXC = (*_RETRIABLE_EXC, httplib2.HttpLib2Error)
except ImportError:  # pragma: no cover
    pass


def http_error_reason(e: Any) -> tuple[int, str, str]:
    """(HTTP-Status, reason, message) aus einem googleapiclient HttpError."""
    status = int(getattr(getattr(e, "resp", None), "status", 0) or 0)
    reason, message = "", ""
    content = getattr(e, "content", b"") or b""
    try:
        data = json.loads(content.decode("utf-8") if isinstance(content, bytes) else content)
        err = data.get("error", {}) if isinstance(data, dict) else {}
        if isinstance(err, dict):
            message = str(err.get("message") or "")
            errors = err.get("errors") or []
            if errors and isinstance(errors[0], dict):
                reason = str(errors[0].get("reason") or "")
            if not reason:
                for d in err.get("details") or []:
                    if isinstance(d, dict) and d.get("reason"):
                        reason = str(d["reason"])
                        break
        elif isinstance(err, str):  # OAuth-Fehlerformat
            reason = err
    except (ValueError, AttributeError):
        pass
    if not reason:
        details = getattr(e, "error_details", None)
        if isinstance(details, list) and details and isinstance(details[0], dict):
            reason = str(details[0].get("reason") or "")
    if not message:
        message = str(getattr(e, "reason", "") or "")
    return status, reason, message


def map_http_error(e: Any) -> str:
    """HttpError -> verständliche deutsche Meldung."""
    status, reason, message = http_error_reason(e)
    msg = _REASONS.get(reason)
    if msg is None:
        if status == 401:
            msg = _REASONS["unauthorized"]
        elif status == 403:
            msg = _REASONS["forbidden"]
        else:
            what = f"HTTP {status}, {reason}" if reason else f"HTTP {status}"
            msg = f"YouTube-API-Fehler ({what}): {message or 'unbekannt'}"
    if reason in ("forbidden", "uploadLimitExceeded") or (status == 403 and not reason):
        msg += " " + AUDIT_HINT
    return redact(msg)


def _is_http_error(e: BaseException) -> bool:
    try:
        from googleapiclient.errors import HttpError
    except ImportError:  # pragma: no cover
        return False
    return isinstance(e, HttpError)


def _clean_tag(raw: object) -> str:
    """Keyword für snippet.tags: ohne '#', '<', '>', Kommas, Anführungszeichen; Leerzeichen bleiben
    ("gta 6 trailer"). Reine Zahlen -> ''."""
    t = " ".join(_TAG_BAD_RE.sub(" ", str(raw or "")).split())
    if not t or t.replace(" ", "").isdigit():
        return ""
    return t[:TAG_MAX_LEN].strip()


def youtube_tags(values: list[str], *, gta: bool, max_chars: int = TAGS_MAX_CHARS) -> list[str]:
    """snippet.tags: Plan-Tags/Hashtags in Reihenfolge, dedupliziert (Groß-/Kleinschreibung und Leerzeichen
    egal), "GTA6" zuerst nur bei GTA-Inhalten, insgesamt <= max_chars (YouTube zählt Kommas mit und Tags mit
    Leerzeichen in Anführungszeichen; Limit 500)."""
    out: list[str] = []
    seen: set[str] = set()
    used = 0
    for raw in [text.REQUIRED_HASHTAG, *values] if gta else list(values):
        t = _clean_tag(raw)
        key = t.replace(" ", "").casefold()
        if not t or key in seen:
            continue
        cost = len(t) + (2 if " " in t else 0) + (1 if out else 0)
        if used + cost > max_chars:
            continue  # kürzere Tags dahinter passen evtl. noch
        seen.add(key)
        out.append(t)
        used += cost
    return out


class YouTubePublisher:
    platform = "youtube"

    def __init__(self, settings: Settings, http=None) -> None:
        """http: optional ein fertiges YouTube-API-Service-Objekt (googleapiclient-kompatibel, z. B. Fake in
        Tests). Ohne wird es aus den gespeicherten OAuth-Credentials gebaut."""
        self.settings = settings
        self.cfg = settings.config.publish.youtube
        self._service_override = http
        self._tokens = TokenStore(settings, "youtube")
        self._pending = TokenStore(settings, "youtube_pending")
        self._lock = threading.RLock()
        self._sleep = time.sleep

    # ------------------------------------------------------------------ Status / OAuth

    @property
    def client_secrets_file(self) -> Path | None:
        f = self.settings.secrets.google_client_secrets_file
        return Path(f).expanduser() if f else None

    def redirect_uri(self) -> str:
        return f"{self.settings.config.dashboard.public_base_url.strip().rstrip('/')}/oauth/youtube/callback"

    def is_configured(self) -> bool:
        f = self.client_secrets_file
        return bool(f and f.is_file())

    def _load(self) -> dict | None:
        try:
            return self._tokens.load()
        except (OSError, ValueError):
            log.warning("YouTube token file unreadable: %s", self._tokens.path)
            return None

    def is_connected(self) -> bool:
        info = self._load()
        return bool(
            info and info.get("refresh_token") and info.get("client_id") and info.get("client_secret")
        )

    def disconnect(self) -> None:
        self._tokens.clear()

    def _flow(self, state: str | None = None):
        if not self.is_configured():
            raise PublishError(
                "YouTube ist nicht konfiguriert: WDVI_GOOGLE_CLIENT_SECRETS_FILE muss auf die "
                "client_secret.json (OAuth-Client 'Webanwendung') zeigen."
            )
        from google_auth_oauthlib.flow import Flow

        kwargs: dict[str, Any] = {"scopes": SCOPES, "redirect_uri": self.redirect_uri()}
        if state:
            kwargs["state"] = state
        try:
            return Flow.from_client_secrets_file(str(self.client_secrets_file), **kwargs)
        except (ValueError, KeyError, OSError) as e:
            raise PublishError(
                f"client_secret.json ist ungültig (Web- oder Desktop-Client erwartet): {e}"
            ) from e

    def _load_pending(self) -> dict[str, dict]:
        try:
            data = self._pending.load() or {}
        except (OSError, ValueError):
            data = {}
        now = time.time()
        return {
            k: v
            for k, v in data.items()
            if isinstance(v, dict) and now - float(v.get("created", 0)) < PENDING_TTL_S
        }

    def auth_url(self, state: str) -> str:
        flow = self._flow()
        url, _ = flow.authorization_url(
            access_type="offline", prompt="consent", include_granted_scopes="true", state=state
        )
        # Neuere google-auth-oauthlib-Versionen erzeugen einen PKCE-Verifier -> pro state merken
        with self._lock:
            pending = self._load_pending()
            pending[state] = {"code_verifier": getattr(flow, "code_verifier", None), "created": time.time()}
            self._pending.save(pending)
        return url

    def handle_callback(self, params: dict[str, str]) -> None:
        if params.get("error"):
            raise PublishError(
                f"Google-Anmeldung abgebrochen: {params.get('error_description') or params['error']}"
            )
        code = (params.get("code") or "").strip()
        state = (params.get("state") or "").strip()
        if not code:
            raise PublishError("Kein 'code' im Google-Callback – bitte die komplette Redirect-URL einfügen.")
        with self._lock:
            pending = self._load_pending()
            entry = pending.pop(state, None) if state else None
            if entry is None:
                raise PublishError(
                    "Unbekannter oder abgelaufener Anmeldevorgang – bitte im Dashboard erneut "
                    "'YouTube verbinden' klicken."
                )
            self._pending.save(pending)

        # include_granted_scopes kann zusätzliche, früher erteilte Scopes zurückgeben -> keine Exception
        os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
        flow = self._flow(state=state)
        flow.code_verifier = entry.get("code_verifier")
        try:
            flow.fetch_token(code=code)
        except Exception as e:  # oauthlib/requests-Fehler
            raise PublishError(
                f"Google-Token-Austausch fehlgeschlagen: {redact(str(e)) or type(e).__name__}. "
                "Code abgelaufen? Bitte erneut verbinden."
            ) from e

        token = getattr(flow.oauth2session, "token", None) or {}
        granted = token.get("scope")
        granted_set = set(granted.split() if isinstance(granted, str) else (granted or []))
        if granted_set and UPLOAD_SCOPE not in granted_set:
            raise PublishError(
                "Google hat die Upload-Berechtigung (youtube.upload) nicht erteilt – bitte erneut "
                "verbinden und das Häkchen für 'YouTube-Videos verwalten/hochladen' setzen."
            )
        if granted_set and MANAGE_SCOPE not in granted_set:
            log.warning(
                "YouTube connected without youtube.force-ssl – canceled scheduled videos cannot be "
                "withdrawn automatically"
            )
        creds = flow.credentials
        info = json.loads(creds.to_json())
        if not info.get("refresh_token"):
            old = self._load() or {}
            if old.get("refresh_token"):
                info["refresh_token"] = old["refresh_token"]
            else:
                raise PublishError(
                    "Google hat kein Refresh-Token geliefert – App unter "
                    "myaccount.google.com/permissions entfernen und erneut verbinden."
                )
        with self._lock:
            self._tokens.save(info)
        log.info("YouTube connected")

    # ------------------------------------------------------------------ Credentials / Service

    def _credentials(self):
        from google.auth.exceptions import RefreshError, TransportError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        with self._lock:
            info = self._load()
            if not info:
                raise PublishError("YouTube ist nicht verbunden – im Dashboard unter Verbindungen anmelden.")
            try:
                creds = Credentials.from_authorized_user_info(info)
            except ValueError as e:
                raise PublishError(
                    "Gespeichertes YouTube-Token ist unvollständig – im Dashboard neu verbinden."
                ) from e
            if not creds.valid:
                try:
                    creds.refresh(Request())
                except RefreshError as e:
                    raise PublishError(
                        "YouTube-Token abgelaufen oder widerrufen – im Dashboard neu verbinden. Tipp: Steht "
                        "der OAuth-Zustimmungsbildschirm auf 'Testing', laufen Refresh-Tokens nach 7 Tagen "
                        "ab -> auf 'In Produktion' umstellen."
                    ) from e
                except TransportError as e:
                    raise PublishError(f"Google nicht erreichbar (Token-Refresh): {type(e).__name__}") from e
                new_info = json.loads(creds.to_json())
                new_info.setdefault("refresh_token", info.get("refresh_token"))
                self._tokens.save(new_info)
                log.info("YouTube access token refreshed")
            return creds

    def _service(self):
        if self._service_override is not None:
            return self._service_override
        from googleapiclient.discovery import build

        return build("youtube", "v3", credentials=self._credentials(), cache_discovery=False)

    # ------------------------------------------------------------------ Publish

    def _body(self, req: PostRequest) -> tuple[dict[str, Any], bool]:
        title = text.compose_title("youtube", req.title)
        description = text.truncate(
            text.youtube_safe(req.description or ""), text.DESCRIPTION_LIMITS["youtube"], text.utf8_len
        )
        # req.hashtags enthält bei YouTube Plan-Tags (Keywords) + Hashtags (Pipeline._post_hashtags)
        gta = text.is_gta_content(req.title, req.hashtags, req.description) or not req.hashtags
        tags = youtube_tags(list(req.hashtags), gta=gta)
        status: dict[str, Any] = {"selfDeclaredMadeForKids": bool(self.cfg.made_for_kids)}
        scheduled = False
        if (
            self.cfg.native_schedule
            and req.publish_at is not None
            and self.cfg.privacy == "public"
            and as_utc(req.publish_at) > utcnow() + SCHEDULE_MIN_LEAD
        ):
            status["privacyStatus"] = "private"
            status["publishAt"] = as_utc(req.publish_at).strftime("%Y-%m-%dT%H:%M:%SZ")
            scheduled = True
        else:
            status["privacyStatus"] = self.cfg.privacy
        body = {
            "snippet": {
                "title": title,
                "description": description,
                "tags": tags,
                "categoryId": str(self.cfg.category_id),
            },
            "status": status,
        }
        return body, scheduled

    def _upload(self, request) -> dict[str, Any]:
        """Resumable Upload: next_chunk-Schleife mit Retries bei 5xx/Verbindungsfehlern."""
        response = None
        errors = 0
        while response is None:
            try:
                _progress, response = request.next_chunk()
            except Exception as e:
                retriable = False
                if _is_http_error(e):
                    retriable = int(getattr(e.resp, "status", 0) or 0) in RETRY_STATUSES
                elif isinstance(e, _RETRIABLE_EXC):
                    retriable = True
                if not retriable or errors >= MAX_RETRIES:
                    raise
                errors += 1
                delay = min(60.0, 2.0**errors) + random.random()
                log.warning(
                    "YouTube upload chunk failed (%s), retry %d/%d in %.0fs",
                    type(e).__name__,
                    errors,
                    MAX_RETRIES,
                    delay,
                )
                self._sleep(delay)
        return response

    def publish(self, req: PostRequest) -> PostResult:
        try:
            path = Path(req.video_path)
            if not path.is_file():
                return failed(f"Videodatei nicht gefunden: {path}")
            from googleapiclient.http import MediaFileUpload

            body, scheduled = self._body(req)
            service = self._service()
            media = MediaFileUpload(str(path), mimetype="video/mp4", chunksize=CHUNK_SIZE, resumable=True)
            request = service.videos().insert(part="snippet,status", body=body, media_body=media)
            response = self._upload(request)
            video_id = str((response or {}).get("id") or "")
            if not video_id:
                return failed("YouTube hat keine Video-ID zurückgegeben.")
            st = (response or {}).get("status") or {}
            if st.get("uploadStatus") in ("rejected", "failed"):
                why = st.get("rejectionReason") or st.get("failureReason") or "unbekannt"
                return failed(f"YouTube hat das Video abgelehnt ({why}).")
            note = None
            if not scheduled and self.cfg.privacy != "private" and st.get("privacyStatus") == "private":
                note = AUDIT_HINT
                log.warning(
                    "YouTube video %s is private although %s was requested – API project not audited?",
                    video_id,
                    self.cfg.privacy,
                )
            log.info(
                "YouTube upload done: %s (%s)",
                video_id,
                "scheduled" if scheduled else body["status"]["privacyStatus"],
            )
            return PostResult(
                ok=True,
                remote_id=video_id,
                remote_url=f"https://youtube.com/shorts/{video_id}",
                status="scheduled" if scheduled else "posted",
                error=note,
            )
        except PublishError as e:
            log.warning("YouTube publish failed: %s", e)
            return failed(str(e))
        except Exception as e:  # noqa: BLE001 - Worker darf nicht abstürzen
            if _is_http_error(e):
                msg = map_http_error(e)
                log.warning("YouTube API error: %s", msg)
                return failed(msg)
            if isinstance(e, _RETRIABLE_EXC):
                return failed(
                    f"Netzwerkfehler beim YouTube-Upload ({type(e).__name__}) – wird erneut versucht."
                )
            log.exception("YouTube publish crashed")
            return failed(f"Unerwarteter Fehler beim YouTube-Upload: {redact(str(e)) or type(e).__name__}")

    # ------------------------------------------------------------------ Status / Zurückziehen

    def _video_status(self, remote_id: str) -> dict[str, Any]:
        """status-Teil des Videos; {} wenn es nicht (mehr) existiert. Wirft bei API-/Netzfehlern."""
        resp = self._service().videos().list(part="status", id=remote_id).execute()
        items = (resp or {}).get("items") or []
        if not items:
            return {}
        st = items[0].get("status")
        return st if isinstance(st, dict) else {}

    def check_status(self, remote_id: str) -> str | None:
        """Aktuelle Sichtbarkeit bei YouTube: "public"/"unlisted"/"private", "missing" (gelöscht/nicht
        gefunden) oder None (nicht abrufbar: Netz, Quota, nicht verbunden). Ist ein nativ geplantes Video
        nach publishAt noch "private", ist es fast sicher wegen des fehlenden API-Audits gesperrt
        (LOCKED_PRIVATE_HINT)."""
        if not remote_id:
            return None
        try:
            st = self._video_status(remote_id)
        except PublishError as e:
            log.warning("YouTube status unavailable: %s", e)
            return None
        except Exception as e:  # noqa: BLE001
            msg = map_http_error(e) if _is_http_error(e) else type(e).__name__
            log.warning("YouTube status for %s failed: %s", remote_id, msg)
            return None
        if not st:
            return "missing"
        return str(st.get("privacyStatus") or "") or None

    def unschedule(self, remote_id: str) -> str:
        """Nativ geplantes Video zurückziehen, damit es nicht zum publishAt öffentlich wird: privat ohne
        publishAt (nicht gelöscht). Gibt eine Meldung fürs Dashboard zurück; PublishError, wenn es nicht
        geht – dann bleibt das Video geplant und der Slot sollte belegt bleiben."""
        if not remote_id:
            raise PublishError("Keine YouTube-Video-ID – nichts zurückzuziehen.")
        try:
            st = self._video_status(remote_id)
            if not st:
                return "Video ist bei YouTube nicht (mehr) vorhanden – nichts zurückzuziehen."
            privacy = str(st.get("privacyStatus") or "")
            if privacy != "private":  # schon sichtbar -> nicht still verstecken, Nutzer entscheidet
                raise PublishError(
                    f"Video ist bei YouTube bereits veröffentlicht ({privacy or 'unbekannt'}) – bitte im "
                    "YouTube Studio löschen oder auf privat stellen."
                )
            if not st.get("publishAt"):
                return "Video ist bei YouTube privat und nicht geplant – nichts zurückzuziehen."
            status: dict[str, Any] = {
                "privacyStatus": "private",
                "selfDeclaredMadeForKids": bool(
                    st.get("selfDeclaredMadeForKids", st.get("madeForKids", self.cfg.made_for_kids))
                ),
            }
            # videos.update setzt nicht mitgeschickte Felder zurück -> vorhandene Werte übernehmen
            for key in ("embeddable", "license", "publicStatsViewable", "containsSyntheticMedia"):
                if key in st:
                    status[key] = st[key]
            self._service().videos().update(part="status", body={"id": remote_id, "status": status}).execute()
        except PublishError:
            raise
        except Exception as e:  # noqa: BLE001
            if _is_http_error(e):
                code, reason, _ = http_error_reason(e)
                if reason in ("insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT") or (
                    code == 403 and reason in ("", "forbidden")
                ):
                    raise PublishError(
                        "YouTube erlaubt das Zurückziehen nicht (Berechtigung youtube.force-ssl fehlt – "
                        "im Dashboard YouTube neu verbinden). Bis dahin das Video im YouTube Studio auf "
                        "privat stellen oder löschen."
                    ) from e
                raise PublishError(
                    map_http_error(e) + " Video bitte im YouTube Studio auf privat stellen oder löschen."
                ) from e
            raise PublishError(
                f"YouTube nicht erreichbar ({type(e).__name__}) – Video bitte im YouTube Studio auf privat "
                "stellen oder löschen."
            ) from e
        log.info("YouTube video %s unscheduled (private, publishAt removed)", remote_id)
        return "Bei YouTube zurückgezogen: Das Video ist jetzt privat ohne Veröffentlichungstermin (nicht gelöscht)."

    # ------------------------------------------------------------------ Metriken

    def fetch_metrics(self, remote_id: str) -> PostMetrics | None:
        if not remote_id:
            return None
        try:
            resp = self._service().videos().list(part="statistics,status", id=remote_id).execute()
        except PublishError as e:
            log.warning("YouTube metrics unavailable: %s", e)
            return None
        except Exception as e:  # noqa: BLE001
            msg = map_http_error(e) if _is_http_error(e) else type(e).__name__
            log.warning("YouTube metrics for %s failed: %s", remote_id, msg)
            return None
        items = (resp or {}).get("items") or []
        if not items:
            return None
        privacy = (items[0].get("status") or {}).get("privacyStatus")
        if privacy == "private" and self.cfg.privacy != "private":
            # gesperrt (API-Audit) oder zurückgezogen: ~0 Views wären falsches Feedback für Claude
            log.warning("YouTube video %s is still private – no metrics stored. %s", remote_id, AUDIT_HINT)
            return None
        stats = items[0].get("statistics") or {}

        def num(key: str) -> int | None:
            try:
                return int(stats[key]) if stats.get(key) is not None else None
            except (TypeError, ValueError):
                return None

        return PostMetrics(views=num("viewCount"), likes=num("likeCount"), comments=num("commentCount"))
