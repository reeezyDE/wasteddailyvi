"""Instagram Graph API (Business/Creator-Account): Reels per Resumable Upload
(POST /{ig-user-id}/media media_type=REELS upload_type=resumable -> rupload.facebook.com) -> Status
pollen (status_code FINISHED) -> /{ig-user-id}/media_publish. Long-lived Token (60 Tage) automatisch
verlängern. Alternativ: Token im Dashboard einfügen.

Einrichtung ("Instagram API with Instagram Login"): Meta-App anlegen, Produkt Instagram -> "API setup with
Instagram login". WDVI_META_APP_ID / WDVI_META_APP_SECRET = dort angezeigte *Instagram* App-ID / App-Secret
(nicht die Facebook-App-ID). Unter "Business login settings" die Redirect-URI
https://<public_base_url ohne http>/oauth/instagram/callback eintragen (Meta verlangt https; eigene URI per
WDVI_INSTAGRAM_REDIRECT_URI). Läuft das Dashboard nur per http: Fehlerseite nach dem Login -> URL aus der
Adresszeile im Dashboard einfügen. Alternativ im App-Dashboard "Generate token" (long-lived) nutzen und
Token + Instagram-User-ID einfügen -> handle_callback({"access_token": ..., "user_id": ...}).
Instagram hat hier kein natives Scheduling: gepostet wird zur Slot-Zeit.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from wdvi.config import Settings
from wdvi.publish import text
from wdvi.publish._common import UPLOAD_TIMEOUT, default_client, failed, https_redirect_uri, json_body, redact
from wdvi.publish.base import PublishError, TokenStore
from wdvi.schemas import PostMetrics, PostRequest, PostResult

log = logging.getLogger(__name__)

AUTHORIZE_URL = "https://www.instagram.com/oauth/authorize"
TOKEN_URL = "https://api.instagram.com/oauth/access_token"
GRAPH = "https://graph.instagram.com"
RUPLOAD = "https://rupload.facebook.com/ig-api-upload"
SCOPES = "instagram_business_basic,instagram_business_content_publish,instagram_business_manage_insights"

LONG_LIVED_S = 60 * 86400
REFRESH_WHEN_LEFT_S = 7 * 86400  # < 7 Tage Restlaufzeit -> verlängern
MANUAL_DEFAULT_S = 55 * 86400  # eingefügtes Token ohne expires_in: vorsichtig annehmen
INSIGHT_METRICS = "views,likes,comments,shares"
# media_publish kann dauern; ein Timeout dort ist unklar (Meta hat evtl. trotzdem veröffentlicht)
PUBLISH_TIMEOUT = httpx.Timeout(180.0, connect=20.0)
PUBLISH_ATTEMPTS = 3
_UNSENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)  # Request kam nie bei Meta an


class InstagramApiError(PublishError):
    def __init__(
        self, message: str, code: int | None = None, subcode: int | None = None, status: int = 0
    ) -> None:
        super().__init__(message)
        self.code = code
        self.subcode = subcode
        self.status = status

    @property
    def unclear(self) -> bool:
        """Server-/Unbekannt-Fehler (HTTP 5xx, Code 1/2): sagt nicht, ob die Aktion ausgeführt wurde."""
        return self.status >= 500 or self.code in (1, 2)


def _api_error(body: dict[str, Any], status: int) -> InstagramApiError:
    err = body.get("error") if isinstance(body.get("error"), dict) else {}
    if not err and ("error_message" in body or "error_type" in body):  # api.instagram.com-Format
        err = {"message": body.get("error_message"), "code": body.get("code"), "type": body.get("error_type")}
    code = _int(err.get("code"))
    sub = _int(err.get("error_subcode"))
    detail = str(err.get("error_user_msg") or err.get("message") or f"HTTP {status}")
    if code == 190:
        msg = (
            "Instagram-Token ungültig oder abgelaufen – im Dashboard neu verbinden oder neues Token einfügen."
        )
    elif code in (4, 17, 32, 613) or sub == 2207051:
        msg = "Instagram-Rate-Limit erreicht – später erneut versuchen."
    elif sub == 2207042:
        msg = "Instagram: Tageslimit für API-Posts (100 pro 24 h) erreicht."
    elif code in (10, 200) or (code is not None and 200 <= code < 300):
        msg = (
            "Instagram: Berechtigung fehlt (instagram_business_content_publish) oder das Konto ist kein "
            "Business-/Creator-Konto – neu verbinden."
        )
    else:
        msg = "Instagram-API-Fehler"
    extra = f" ({detail}; Code {code}" + (f"/{sub}" if sub else "") + ")"
    return InstagramApiError(redact(msg + extra), code, sub, status)


def _int(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _insight_value(item: dict[str, Any]) -> int | None:
    values = item.get("values")
    v = None
    if isinstance(values, list) and values:
        v = values[0].get("value") if isinstance(values[0], dict) else None
    if v is None and isinstance(item.get("total_value"), dict):
        v = item["total_value"].get("value")
    return _int(v)


class InstagramPublisher:
    platform = "instagram"
    poll_interval_s = 5.0
    poll_timeout_s = 600.0

    def __init__(self, settings: Settings, http=None) -> None:
        """http: optionaler httpx.Client (Tests/respx); sonst wird bei Bedarf einer erzeugt."""
        self.settings = settings
        self.cfg = settings.config.publish.instagram
        self._http: httpx.Client | None = http
        self._tokens = TokenStore(settings, "instagram")
        self._lock = threading.RLock()
        self._sleep = time.sleep
        self._clock = time.monotonic
        self._now = time.time

    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = default_client()
        return self._http

    @property
    def _base(self) -> str:
        return f"{GRAPH}/{self.cfg.graph_version.strip('/')}"

    def redirect_uri(self) -> str:
        return https_redirect_uri(self.settings, "instagram")

    # ------------------------------------------------------------------ Status / OAuth

    def is_configured(self) -> bool:
        s = self.settings.secrets
        return bool(s.meta_app_id and s.meta_app_secret)

    def _load(self) -> dict | None:
        try:
            return self._tokens.load()
        except (OSError, ValueError):
            log.warning("Instagram token file unreadable: %s", self._tokens.path)
            return None

    def is_connected(self) -> bool:
        tok = self._load()
        if not tok or not tok.get("access_token"):
            return False
        exp = float(tok.get("expires_at") or 0)
        return exp == 0 or exp > self._now()

    def disconnect(self) -> None:
        self._tokens.clear()

    def auth_url(self, state: str) -> str:
        if not self.is_configured():
            raise PublishError(
                "Instagram ist nicht konfiguriert: WDVI_META_APP_ID und WDVI_META_APP_SECRET "
                "(Instagram-App-ID/-Secret aus 'API setup with Instagram login') setzen."
            )
        params = {
            "client_id": self.settings.secrets.meta_app_id,
            "redirect_uri": self.redirect_uri(),
            "response_type": "code",
            "scope": SCOPES,
            "state": state,
        }
        return f"{AUTHORIZE_URL}?{urlencode(params, safe=',')}"

    def handle_callback(self, params: dict[str, str]) -> None:
        if params.get("error"):
            detail = params.get("error_description") or params.get("error_reason") or params["error"]
            raise PublishError(f"Instagram-Anmeldung abgebrochen: {detail}")
        if (params.get("access_token") or "").strip():
            self._store_manual(params)
            return
        code = (params.get("code") or "").strip().removesuffix("#_")
        if not code:
            raise PublishError(
                "Kein 'code' im Instagram-Callback – bitte die komplette Redirect-URL oder ein "
                "Token einfügen."
            )
        if not self.is_configured():
            raise PublishError(
                "Instagram ist nicht konfiguriert (WDVI_META_APP_ID/WDVI_META_APP_SECRET fehlen)."
            )
        s = self.settings.secrets
        resp = self.http.post(
            TOKEN_URL,
            data={
                "client_id": s.meta_app_id,
                "client_secret": s.meta_app_secret,
                "grant_type": "authorization_code",
                "redirect_uri": self.redirect_uri(),
                "code": code,
            },
        )
        body = json_body(resp)
        entry = body["data"][0] if isinstance(body.get("data"), list) and body["data"] else body
        if resp.status_code >= 400 or not isinstance(entry, dict) or not entry.get("access_token"):
            raise PublishError(
                "Instagram-Code-Tausch fehlgeschlagen: " + str(_api_error(body, resp.status_code))
            )
        short_token = str(entry["access_token"])
        user_id = str(entry.get("user_id") or "")

        resp = self.http.get(
            f"{GRAPH}/access_token",
            params={
                "grant_type": "ig_exchange_token",
                "client_secret": s.meta_app_secret,
                "access_token": short_token,
            },
        )
        body = json_body(resp)
        if resp.status_code >= 400 or not body.get("access_token"):
            raise PublishError(
                "Instagram: Long-lived-Token-Tausch fehlgeschlagen: "
                + str(_api_error(body, resp.status_code))
            )
        token = str(body["access_token"])
        expires_in = _int(body.get("expires_in")) or LONG_LIVED_S

        me = self._me(token, strict=False)
        self._save(
            token,
            str(me.get("user_id") or me.get("id") or user_id),
            me.get("username"),
            expires_in,
            source="oauth",
        )
        log.info("Instagram connected (@%s)", me.get("username") or "?")

    def _store_manual(self, params: dict[str, str]) -> None:
        token = params["access_token"].strip()
        given_id = (params.get("user_id") or "").strip()
        expires_in = _int(params.get("expires_in")) or MANUAL_DEFAULT_S
        try:
            me = self._me(token, strict=True)
        except InstagramApiError as e:
            raise PublishError(f"Eingefügtes Instagram-Token wird nicht akzeptiert: {e}") from e
        except httpx.HTTPError as e:
            if not given_id:
                raise PublishError("Instagram nicht erreichbar – Token konnte nicht geprüft werden.") from e
            log.warning("Instagram /me check skipped (network): %s", type(e).__name__)
            me = {}
        user_id = str(me.get("user_id") or given_id or me.get("id") or "")
        if not user_id:
            raise PublishError("Instagram-User-ID fehlt – bitte zusammen mit dem Token einfügen.")
        if given_id and me.get("user_id") and str(me["user_id"]) != given_id:
            log.warning("Pasted Instagram user_id differs from /me user_id; using /me value")
        self._save(token, user_id, me.get("username"), expires_in, source="manual")
        log.info("Instagram token stored manually (@%s)", me.get("username") or "?")

    def _me(self, token: str, strict: bool) -> dict[str, Any]:
        try:
            return self._graph("GET", "me", params={"fields": "user_id,username", "access_token": token})
        except (InstagramApiError, httpx.HTTPError):
            if strict:
                raise
            log.warning("Instagram /me lookup failed")
            return {}

    def _save(self, token: str, user_id: str, username: str | None, expires_in: int, source: str) -> dict:
        now = self._now()
        tok = {
            "access_token": token,
            "user_id": user_id,
            "username": username,
            "expires_at": now + expires_in,
            "obtained_at": now,
            "source": source,
        }
        with self._lock:
            self._tokens.save(tok)
        return tok

    def _token(self) -> tuple[str, str]:
        """(access_token, ig_user_id); verlängert automatisch bei < 7 Tagen Restlaufzeit."""
        with self._lock:
            tok = self._load()
            if not tok or not tok.get("access_token"):
                raise PublishError(
                    "Instagram ist nicht verbunden – im Dashboard unter Verbindungen anmelden "
                    "oder ein Token einfügen."
                )
            now = self._now()
            exp = float(tok.get("expires_at") or 0)
            if exp and exp <= now:
                raise PublishError(
                    "Instagram-Token abgelaufen (60 Tage) – im Dashboard neu verbinden oder neues "
                    "Token einfügen."
                )
            if exp and exp - now < REFRESH_WHEN_LEFT_S:
                try:
                    tok = self._refresh(tok)
                except (PublishError, httpx.HTTPError) as e:
                    log.warning(
                        "Instagram token refresh failed (still valid until %s): %s",
                        time.strftime("%Y-%m-%d", time.gmtime(exp)),
                        redact(str(e)),
                    )
            if not tok.get("user_id"):
                me = self._me(tok["access_token"], strict=True)
                tok["user_id"] = str(me.get("user_id") or me.get("id") or "")
                tok["username"] = me.get("username")
                self._tokens.save(tok)
            return str(tok["access_token"]), str(tok["user_id"])

    def _refresh(self, tok: dict) -> dict:
        resp = self.http.get(
            f"{GRAPH}/refresh_access_token",
            params={"grant_type": "ig_refresh_token", "access_token": tok["access_token"]},
        )
        body = json_body(resp)
        if resp.status_code >= 400 or not body.get("access_token"):
            raise _api_error(body, resp.status_code)
        now = self._now()
        tok = {
            **tok,
            "access_token": str(body["access_token"]),
            "expires_at": now + (_int(body.get("expires_in")) or LONG_LIVED_S),
            "refreshed_at": now,
        }
        self._tokens.save(tok)
        log.info("Instagram token refreshed")
        return tok

    # ------------------------------------------------------------------ API-Helfer

    def _graph(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> dict[str, Any]:
        extra: dict[str, Any] = {"timeout": timeout} if timeout is not None else {}
        resp = self.http.request(
            method, f"{self._base}/{path.lstrip('/')}", params=params, data=data, **extra
        )
        body = json_body(resp)
        if resp.status_code >= 400 or isinstance(body.get("error"), dict):
            raise _api_error(body, resp.status_code)
        return body

    def _upload(self, uri: str, token: str, path: Path, size: int) -> None:
        data = path.read_bytes()
        headers = {
            "Authorization": f"OAuth {token}",
            "offset": "0",
            "file_size": str(size),
            "Content-Type": "application/octet-stream",
        }
        for attempt in range(1, 4):
            try:
                resp = self.http.post(uri, content=data, headers=headers, timeout=UPLOAD_TIMEOUT)
            except httpx.TransportError as e:
                if attempt == 3:
                    raise PublishError(f"Instagram-Upload: Netzwerkfehler ({type(e).__name__}).") from e
                self._sleep(3.0 * attempt)
                continue
            body = json_body(resp)
            if resp.status_code < 400 and body.get("success", True) and "debug_info" not in body:
                return
            debug = body.get("debug_info") if isinstance(body.get("debug_info"), dict) else {}
            retriable = bool(debug.get("retriable")) or resp.status_code >= 500
            if retriable and attempt < 3:
                self._sleep(3.0 * attempt)
                continue
            detail = debug.get("message")
            if not detail and isinstance(body.get("error"), dict):
                detail = body["error"].get("message")
            raise PublishError(
                f"Instagram-Upload fehlgeschlagen (HTTP {resp.status_code}"
                + (f": {redact(str(detail))}" if detail else "")
                + ")."
            )

    def _wait_finished(self, container_id: str, token: str) -> None:
        deadline = self._clock() + self.poll_timeout_s
        while True:
            st: dict[str, Any] = {}
            try:
                st = self._graph(
                    "GET", container_id, params={"fields": "status_code,status", "access_token": token}
                )
            except httpx.TransportError as e:
                log.warning("Instagram status poll network error: %s", type(e).__name__)
            code = str(st.get("status_code") or "")
            if code in ("FINISHED", "PUBLISHED"):
                return
            if code in ("ERROR", "EXPIRED"):
                raise PublishError(
                    f"Instagram konnte das Video nicht verarbeiten ({code}): "
                    f"{st.get('status') or 'keine Details'}"
                )
            if self._clock() >= deadline:
                raise PublishError(
                    f"Instagram-Verarbeitung dauert länger als {int(self.poll_timeout_s / 60)} "
                    f"Minuten (Status {code or 'unbekannt'}) – abgebrochen."
                )
            self._sleep(self.poll_interval_s)

    def _publish_container(
        self, user_id: str, container_id: str, token: str, caption: str
    ) -> tuple[str | None, str | None]:
        """media_publish -> (media_id, Hinweis). Sobald ein Request abgeschickt ist, darf nur eine
        ausdrückliche Absage von Meta zu 'failed' führen: Ein Container lässt sich nur einmal
        veröffentlichen, erneutes media_publish mit derselben creation_id ist also unkritisch – ein
        Pipeline-Retry mit neuem Container würde das Reel dagegen doppelt posten."""
        unclear: str | None = None  # warum offen ist, ob Meta schon veröffentlicht hat
        for attempt in range(1, PUBLISH_ATTEMPTS + 1):
            if attempt > 1:
                self._sleep(10.0)
            try:
                body = self._graph(
                    "POST",
                    f"{user_id}/media_publish",
                    data={"creation_id": container_id, "access_token": token},
                    timeout=PUBLISH_TIMEOUT,
                )
            except InstagramApiError as e:
                if e.unclear:
                    unclear = str(e)
                    log.warning("Instagram media_publish unclear (attempt %d): %s", attempt, e)
                    continue
                if unclear is not None:
                    break  # Absage nach unklarem Versuch (z. B. schon veröffentlicht) -> Status entscheidet
                # 9007 / 2207027: Medium noch nicht bereit -> kurz warten
                if (e.code == 9007 or e.subcode == 2207027) and attempt < PUBLISH_ATTEMPTS:
                    continue
                raise
            except _UNSENT as e:
                log.warning("Instagram media_publish not sent (%s), attempt %d", type(e).__name__, attempt)
                if attempt < PUBLISH_ATTEMPTS:
                    continue
                if unclear is None:
                    raise
                break
            except httpx.HTTPError as e:  # Timeout/Abbruch nach dem Senden
                unclear = f"Netzwerkfehler ({type(e).__name__})"
                log.warning("Instagram media_publish unclear (attempt %d): %s", attempt, type(e).__name__)
                continue
            if body.get("id"):
                return str(body["id"]), None
            unclear = "keine Media-ID in der Antwort"
            break
        return self._resolve_unclear(user_id, container_id, token, caption, unclear or "unbekannt")

    def _resolve_unclear(
        self, user_id: str, container_id: str, token: str, caption: str, reason: str
    ) -> tuple[str | None, str | None]:
        """Nach unklarem media_publish den Container fragen: PUBLISHED -> Media-ID suchen, ERROR/EXPIRED ->
        Fehlschlag, sonst ok mit Prüfhinweis statt 'failed' (kein automatischer Neu-Upload)."""
        code = ""
        for attempt in range(3):
            if attempt:
                self._sleep(self.poll_interval_s)
            try:
                st = self._graph(
                    "GET", container_id, params={"fields": "status_code,status", "access_token": token}
                )
            except (PublishError, httpx.HTTPError) as e:
                log.warning("Instagram container status after media_publish failed: %s", redact(str(e)))
                continue
            code = str(st.get("status_code") or "")
            if code == "PUBLISHED":
                media_id = self._find_media(user_id, token, caption)
                if media_id:
                    return media_id, None
                return None, (
                    "Hinweis: Reel ist laut Instagram veröffentlicht, die Media-ID ließ sich aber nicht "
                    "ermitteln (keine Statistiken/kein Link) – bitte in der Instagram-App prüfen."
                )
            if code in ("ERROR", "EXPIRED"):
                raise PublishError(
                    f"Instagram konnte das Reel nicht veröffentlichen ({code}): "
                    f"{st.get('status') or 'keine Details'}"
                )
        log.warning("Instagram publish state unknown (container %s, %s)", code or "?", reason)
        return None, redact(
            f"Hinweis: Instagram-Status unbekannt ({reason}; Container-Status {code or 'unbekannt'}) – "
            "Reel bitte in der Instagram-App prüfen. Es wird nicht automatisch erneut hochgeladen, damit "
            "es nicht doppelt erscheint."
        )

    def _find_media(self, user_id: str, token: str, caption: str) -> str | None:
        """Media-ID des gerade veröffentlichten Reels über die neuesten Posts (gleiche Caption) suchen."""
        try:
            body = self._graph(
                "GET",
                f"{user_id}/media",
                params={"fields": "id,caption,timestamp", "limit": "10", "access_token": token},
            )
        except (PublishError, httpx.HTTPError) as e:
            log.warning("Instagram media lookup failed: %s", redact(str(e)))
            return None
        want = caption.strip()
        for item in body.get("data") or []:
            if isinstance(item, dict) and item.get("id") and str(item.get("caption") or "").strip() == want:
                return str(item["id"])
        return None

    # ------------------------------------------------------------------ Publish

    def publish(self, req: PostRequest) -> PostResult:
        try:
            path = Path(req.video_path)
            if not path.is_file():
                return failed(f"Videodatei nicht gefunden: {path}")
            size = path.stat().st_size
            if size == 0:
                return failed("Videodatei ist leer.")
            token, user_id = self._token()
            caption = text.build_caption("instagram", req.title, req.description)
            container = self._graph(
                "POST",
                f"{user_id}/media",
                data={
                    "media_type": "REELS",
                    "upload_type": "resumable",
                    "caption": caption,
                    "share_to_feed": "true" if self.cfg.share_to_feed else "false",
                    "access_token": token,
                },
            )
            container_id = str(container.get("id") or "")
            if not container_id:
                raise PublishError("Instagram hat keinen Container angelegt (keine ID).")
            uri = str(container.get("uri") or f"{RUPLOAD}/{self.cfg.graph_version.strip('/')}/{container_id}")
            self._upload(uri, token, path, size)
            self._wait_finished(container_id, token)
            # ab hier ist 'failed' nur bei ausdrücklicher Absage erlaubt (sonst Doppelpost durch Retry)
            media_id, note = self._publish_container(user_id, container_id, token, caption)
            permalink = None
            if media_id:
                try:
                    permalink = self._graph(
                        "GET", media_id, params={"fields": "permalink", "access_token": token}
                    ).get("permalink")
                except (PublishError, httpx.HTTPError):
                    log.warning("Instagram permalink lookup failed for %s", media_id)
            log.info("Instagram reel published (media_id=%s)", media_id or "unbekannt")
            return PostResult(ok=True, remote_id=media_id, remote_url=permalink, status="posted", error=note)
        except PublishError as e:
            log.warning("Instagram publish failed: %s", e)
            return failed(str(e))
        except httpx.HTTPError as e:
            log.warning("Instagram network error: %s", type(e).__name__)
            return failed(f"Netzwerkfehler bei Instagram ({type(e).__name__}): {redact(str(e))}")
        except Exception as e:  # noqa: BLE001 - Worker darf nicht abstürzen
            log.exception("Instagram publish crashed")
            return failed(f"Unerwarteter Fehler beim Instagram-Upload: {redact(str(e))}")

    # ------------------------------------------------------------------ Metriken

    def fetch_metrics(self, remote_id: str) -> PostMetrics | None:
        if not remote_id:
            return None
        try:
            token, _ = self._token()
        except (PublishError, httpx.HTTPError) as e:
            log.warning("Instagram metrics unavailable: %s", redact(str(e)))
            return None
        try:
            body = self._graph(
                "GET", f"{remote_id}/insights", params={"metric": INSIGHT_METRICS, "access_token": token}
            )
            vals = {
                str(i.get("name")): _insight_value(i) for i in body.get("data") or [] if isinstance(i, dict)
            }
            return PostMetrics(
                views=vals.get("views"),
                likes=vals.get("likes"),
                comments=vals.get("comments"),
                shares=vals.get("shares"),
            )
        except InstagramApiError as e:
            log.info("Instagram insights failed (%s) – falling back to like/comment counts", e.code)
        except httpx.HTTPError as e:
            log.warning("Instagram insights network error: %s", type(e).__name__)
            return None
        try:
            body = self._graph(
                "GET", remote_id, params={"fields": "like_count,comments_count", "access_token": token}
            )
            return PostMetrics(likes=_int(body.get("like_count")), comments=_int(body.get("comments_count")))
        except (PublishError, httpx.HTTPError) as e:
            log.warning("Instagram metrics for %s failed: %s", remote_id, redact(str(e)))
            return None
