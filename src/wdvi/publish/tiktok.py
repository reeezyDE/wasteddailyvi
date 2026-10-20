"""TikTok Content Posting API v2: OAuth v2 (client_key, Scopes user.info.basic,video.upload,video.publish),
mode "inbox" -> /v2/post/publish/inbox/video/init/ (Entwurf in der App), mode "direct" ->
/v2/post/publish/creator_info/query/ + /v2/post/publish/video/init/ (FILE_UPLOAD, Chunks per PUT mit
Content-Range) + Status-Polling /v2/post/publish/status/fetch/. Token-Refresh automatisch.
Ohne App-Audit sind Direktposts nur SELF_ONLY.

Einrichtung (developers.tiktok.com): App mit "Login Kit" + "Content Posting API" anlegen, Redirect-URI
https://<public_base_url ohne http>/oauth/tiktok/callback registrieren (TikTok akzeptiert nur https;
eigene URI per WDVI_TIKTOK_REDIRECT_URI). Läuft das Dashboard nur per http, zeigt der Browser nach dem
Login eine Fehlerseite – deren URL (mit code + state) im Dashboard unter "Redirect-URL einfügen"
eintragen; die Query-Parameter landen in handle_callback.
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
from wdvi.publish._common import (
    UPLOAD_TIMEOUT,
    default_client,
    failed,
    https_redirect_uri,
    json_body,
    probe_duration,
    redact,
)
from wdvi.publish.base import PublishError, TokenStore
from wdvi.schemas import PostMetrics, PostRequest, PostResult

log = logging.getLogger(__name__)

AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
API_BASE = "https://open.tiktokapis.com"
TOKEN_URL = f"{API_BASE}/v2/oauth/token/"
SCOPES = "user.info.basic,video.upload,video.publish,video.list"

SINGLE_CHUNK_MAX = 64 * 1024 * 1024  # bis 64 MB in einem Stück
CHUNK_SIZE = 10 * 1024 * 1024  # sonst 10-MB-Chunks, der letzte nimmt den Rest auf
REFRESH_MARGIN_S = 10 * 60  # Access-Token (24 h) 10 min vor Ablauf erneuern
POLL_MAX_ERRORS = 5  # so viele Fehler in Folge beim Status-Abruf -> Status gilt als unbekannt
UPLOADED_STATES = {"SEND_TO_USER_INBOX", "PUBLISH_COMPLETE"}
METRIC_FIELDS = "id,view_count,like_count,comment_count,share_count"

_AUDIT_HINT = (
    "Öffentliche Direktposts brauchen das TikTok-App-Audit (Content Posting API); bis dahin "
    "publish.tiktok.mode: inbox nutzen oder als SELF_ONLY posten."
)

_API_ERRORS = {
    "access_token_invalid": "TikTok-Zugangstoken ungültig oder abgelaufen – im Dashboard neu verbinden.",
    "scope_not_authorized": (
        "TikTok-Berechtigung fehlt (video.upload/video.publish) – im Dashboard neu verbinden und alle "
        "Berechtigungen bestätigen."
    ),
    "scope_permission_missed": (
        "TikTok-Berechtigung fehlt – im Dashboard neu verbinden und alle Berechtigungen bestätigen."
    ),
    "rate_limit_exceeded": "TikTok-Rate-Limit erreicht – später erneut versuchen.",
    "spam_risk_too_many_posts": "TikTok: Tageslimit für API-Posts erreicht – später erneut versuchen.",
    "spam_risk_too_many_pending_share": (
        "TikTok: Zu viele offene Entwürfe in der Inbox (max. 5 in 24 h) – Entwürfe in der TikTok-App posten "
        "oder verwerfen."
    ),
    "spam_risk_user_banned_from_posting": "TikTok: Dieses Konto darf derzeit nicht posten.",
    "spam_risk": "TikTok hat den Post als Spam-Risiko blockiert.",
    "unaudited_client_can_only_post_to_private_accounts": (
        "TikTok-App ist noch nicht geprüft: Direktposts gehen nur auf ein PRIVATES Konto mit Sichtbarkeit "
        "SELF_ONLY. Konto auf privat stellen, publish.tiktok.mode: inbox nutzen oder das App-Audit "
        "beantragen."
    ),
    "privacy_level_option_mismatch": "TikTok: Die gewählte Sichtbarkeit ist für dieses Konto nicht erlaubt.",
    "reached_active_user_cap": "TikTok: Tageslimit aktiver Nutzer für die (ungeprüfte) App erreicht.",
    "invalid_file_upload": "TikTok: Datei-/Chunk-Angaben ungültig (Videogröße oder Chunk-Aufteilung).",
    "invalid_params": "TikTok: Ungültige Parameter in der Anfrage.",
}

_FAIL_REASONS = {
    "file_format_check_failed": "Videoformat nicht unterstützt (MP4/H.264 erwartet)",
    "duration_check_failed": "Videolänge nicht erlaubt",
    "frame_rate_check_failed": "Bildrate nicht erlaubt (23–60 fps)",
    "picture_size_check_failed": "Auflösung nicht erlaubt (min. 360 px)",
    "internal": "interner TikTok-Fehler – später erneut versuchen",
    "video_pull_failed": "TikTok konnte das Video nicht laden",
    "publish_cancelled": "Veröffentlichung abgebrochen",
    "auth_removed": "App-Zugriff wurde in TikTok entzogen – neu verbinden",
    "spam_risk_too_many_posts": "Tageslimit für Posts erreicht",
    "spam_risk_user_banned_from_posting": "Konto darf derzeit nicht posten",
    "spam_risk_text": "Caption als Spam eingestuft",
    "spam_risk": "als Spam-Risiko eingestuft",
}


class TikTokApiError(PublishError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def chunk_plan(size: int) -> tuple[int, int]:
    """(chunk_size, total_chunk_count) nach TikToks Regeln: <= 64 MB in einem Stück, sonst 10-MB-Chunks;
    total = floor(size / chunk_size), der letzte Chunk nimmt den Rest auf (< 2 x chunk_size)."""
    if size <= 0:
        raise PublishError("Videodatei ist leer.")
    if size <= SINGLE_CHUNK_MAX:
        return size, 1
    return CHUNK_SIZE, size // CHUNK_SIZE


def chunk_ranges(size: int, chunk_size: int, count: int) -> list[tuple[int, int]]:
    """Byte-Bereiche (start, end inklusive) je Chunk."""
    out = []
    for i in range(count):
        start = i * chunk_size
        end = size - 1 if i == count - 1 else start + chunk_size - 1
        out.append((start, end))
    return out


def _api_error(code: str, message: str, log_id: str | None) -> TikTokApiError:
    base = _API_ERRORS.get(code) or f"TikTok-API-Fehler: {message or code}"
    suffix = f" (Code {code}" + (f", log_id {log_id}" if log_id else "") + ")"
    return TikTokApiError(code, redact(base + suffix))


def _post_ids(status: dict[str, Any]) -> list[str]:
    """Öffentliche Post-IDs aus der Statusantwort (TikTok schreibt das Feld "publicaly_…")."""
    ids = status.get("publicaly_available_post_id") or status.get("publicly_available_post_id") or []
    if not isinstance(ids, list):
        ids = [ids]
    return [str(i) for i in ids if i not in (None, "")]


def _poll_error_text(e: BaseException) -> str:
    if isinstance(e, PublishError):
        return redact(str(e))
    return type(e).__name__


class TikTokPublisher:
    platform = "tiktok"
    poll_interval_s = 5.0
    poll_timeout_s = 300.0

    def __init__(self, settings: Settings, http=None) -> None:
        """http: optionaler httpx.Client (Tests/respx); sonst wird bei Bedarf einer erzeugt."""
        self.settings = settings
        self.cfg = settings.config.publish.tiktok
        self._http: httpx.Client | None = http
        self._tokens = TokenStore(settings, "tiktok")
        self._lock = threading.RLock()
        self._sleep = time.sleep
        self._clock = time.monotonic
        self._now = time.time  # Epoch-Sekunden (Token-Ablauf)

    # ------------------------------------------------------------------ Status / OAuth

    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = default_client()
        return self._http

    def redirect_uri(self) -> str:
        return https_redirect_uri(self.settings, "tiktok")

    def is_configured(self) -> bool:
        s = self.settings.secrets
        return bool(s.tiktok_client_key and s.tiktok_client_secret)

    def _load(self) -> dict | None:
        try:
            return self._tokens.load()
        except (OSError, ValueError):
            log.warning("TikTok token file unreadable: %s", self._tokens.path)
            return None

    def is_connected(self) -> bool:
        tok = self._load()
        if not tok or not tok.get("access_token"):
            return False
        now = self._now()
        if float(tok.get("expires_at") or 0) > now + 60:
            return True
        return bool(tok.get("refresh_token")) and float(tok.get("refresh_expires_at") or 0) > now

    def disconnect(self) -> None:
        self._tokens.clear()

    def auth_url(self, state: str) -> str:
        if not self.is_configured():
            raise PublishError(
                "TikTok ist nicht konfiguriert: WDVI_TIKTOK_CLIENT_KEY und WDVI_TIKTOK_CLIENT_SECRET setzen."
            )
        params = {
            "client_key": self.settings.secrets.tiktok_client_key,
            "scope": SCOPES,
            "response_type": "code",
            "redirect_uri": self.redirect_uri(),
            "state": state,
        }
        return f"{AUTH_URL}?{urlencode(params, safe=',')}"

    def handle_callback(self, params: dict[str, str]) -> None:
        if params.get("error"):
            detail = params.get("error_description") or params["error"]
            raise PublishError(f"TikTok-Anmeldung abgebrochen: {detail}")
        code = (params.get("code") or "").strip()
        if not code:
            raise PublishError("Kein 'code' im TikTok-Callback – bitte die komplette Redirect-URL einfügen.")
        if not self.is_configured():
            raise PublishError("TikTok ist nicht konfiguriert (Client Key/Secret fehlen).")
        body = self._token_request(
            {
                "client_key": self.settings.secrets.tiktok_client_key,
                "client_secret": self.settings.secrets.tiktok_client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": self.redirect_uri(),
            }
        )
        granted = {
            s.strip() for s in str(body.get("scope") or params.get("scopes") or "").split(",") if s.strip()
        }
        needed = {"video.upload"} | ({"video.publish"} if self.cfg.mode == "direct" else set())
        if granted and not needed <= granted:
            missing = ", ".join(sorted(needed - granted))
            raise PublishError(
                f"TikTok hat die Berechtigung {missing} nicht erteilt – bitte erneut verbinden "
                "und alle Berechtigungen bestätigen."
            )
        with self._lock:
            self._save_token(body, previous=None)
        log.info("TikTok connected (open_id=%s)", body.get("open_id"))

    # ------------------------------------------------------------------ Token

    def _token_request(self, data: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = self.http.post(TOKEN_URL, data=data, headers={"Cache-Control": "no-cache"})
        except httpx.HTTPError as e:
            raise PublishError(f"TikTok-Token-Anfrage fehlgeschlagen (Netzwerk): {redact(str(e))}") from e
        body = json_body(resp)
        err = body.get("error")
        if isinstance(err, dict):  # manche Antworten nutzen das API-Fehlerformat
            if err.get("code") not in (None, "", "ok"):
                raise PublishError(
                    f"TikTok-Token-Fehler: {redact(str(err.get('message') or err.get('code')))}"
                )
        elif err:
            detail = body.get("error_description") or err
            raise PublishError(f"TikTok-Token-Fehler: {redact(str(detail))} ({err})")
        if resp.status_code >= 400 or not body.get("access_token"):
            raise PublishError(f"TikTok-Token-Anfrage fehlgeschlagen (HTTP {resp.status_code}).")
        return body

    def _save_token(self, body: dict[str, Any], previous: dict | None) -> dict:
        now = self._now()
        prev = previous or {}
        tok = {
            "access_token": body["access_token"],
            "refresh_token": body.get("refresh_token") or prev.get("refresh_token"),
            "open_id": body.get("open_id") or prev.get("open_id"),
            "scope": body.get("scope") or prev.get("scope"),
            "token_type": body.get("token_type", "Bearer"),
            "expires_at": now + int(body.get("expires_in") or 86400),
            "refresh_expires_at": (now + int(body["refresh_expires_in"]))
            if body.get("refresh_expires_in")
            else prev.get("refresh_expires_at", now + 365 * 86400),
            "updated_at": now,
        }
        self._tokens.save(tok)
        return tok

    def _access_token(self, force_refresh: bool = False) -> str:
        with self._lock:
            tok = self._load()
            if not tok or not tok.get("access_token"):
                raise PublishError("TikTok ist nicht verbunden – im Dashboard unter Verbindungen anmelden.")
            now = self._now()
            if force_refresh or float(tok.get("expires_at") or 0) - now < REFRESH_MARGIN_S:
                if not tok.get("refresh_token") or float(tok.get("refresh_expires_at") or 0) <= now:
                    raise PublishError("TikTok-Anmeldung abgelaufen – im Dashboard neu verbinden.")
                body = self._token_request(
                    {
                        "client_key": self.settings.secrets.tiktok_client_key or "",
                        "client_secret": self.settings.secrets.tiktok_client_secret or "",
                        "grant_type": "refresh_token",
                        "refresh_token": tok["refresh_token"],
                    }
                )
                tok = self._save_token(body, previous=tok)
                log.info("TikTok access token refreshed")
            return str(tok["access_token"])

    # ------------------------------------------------------------------ API-Helfer

    def _api(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        params: dict[str, str] | None = None,
        retry_auth: bool = True,
    ) -> dict[str, Any]:
        """POST an die Content-API; prüft error.code != "ok" und gibt body["data"] zurück."""
        token = self._access_token()
        resp = self.http.post(
            f"{API_BASE}{path}",
            params=params,
            json=payload,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"},
        )
        body = json_body(resp)
        err = body.get("error") if isinstance(body.get("error"), dict) else {}
        code = str(err.get("code") or "")
        if (code and code != "ok") or resp.status_code >= 400:
            if code == "access_token_invalid" and retry_auth:
                self._access_token(force_refresh=True)
                return self._api(path, payload, params=params, retry_auth=False)
            raise _api_error(
                code or f"http_{resp.status_code}", str(err.get("message") or ""), err.get("log_id")
            )
        data = body.get("data")
        return data if isinstance(data, dict) else {}

    def _upload(self, upload_url: str, path: Path, size: int, chunk_size: int, count: int) -> None:
        ranges = chunk_ranges(size, chunk_size, count)
        with path.open("rb") as f:
            for i, (start, end) in enumerate(ranges, start=1):
                f.seek(start)
                data = f.read(end - start + 1)
                headers = {
                    "Content-Type": "video/mp4",
                    "Content-Length": str(len(data)),
                    "Content-Range": f"bytes {start}-{end}/{size}",
                }
                for attempt in range(1, 4):
                    try:
                        resp = self.http.put(
                            upload_url, content=data, headers=headers, timeout=UPLOAD_TIMEOUT
                        )
                    except httpx.TransportError as e:
                        if attempt == 3:
                            raise PublishError(
                                f"TikTok-Upload Teil {i}/{count}: Netzwerkfehler ({type(e).__name__})."
                            ) from e
                        self._sleep(2.0 * attempt)
                        continue
                    if resp.status_code in (200, 201, 206):
                        break
                    if resp.status_code >= 500 and attempt < 3:
                        self._sleep(2.0 * attempt)
                        continue
                    raise PublishError(
                        f"TikTok-Upload Teil {i}/{count} fehlgeschlagen (HTTP {resp.status_code})."
                    )
                log.debug("TikTok chunk %d/%d uploaded", i, count)

    def _poll(self, publish_id: str, ok_states: set[str]) -> tuple[dict[str, Any], str | None]:
        """Status pollen bis ok_states / FAILED / Timeout. Gibt (letzte Statusdaten, Hinweis) zurück; der
        Hinweis ist None, wenn ein ok_state erreicht wurde.

        Läuft NACH dem Upload – TikTok veröffentlicht dann ohne weiteres Zutun. Deshalb ist nur TikToks
        ausdrückliches FAILED ein Fehlschlag (PublishError). Netz-, API- oder Token-Fehler beim Abruf
        ergeben nur einen Hinweis, sonst würde die Pipeline dasselbe Video erneut hochladen (Doppelpost).
        """
        deadline = self._clock() + self.poll_timeout_s
        data: dict[str, Any] = {}
        errors = 0
        last_error = ""
        while True:
            try:
                data = self._api("/v2/post/publish/status/fetch/", {"publish_id": publish_id})
                errors = 0
            except Exception as e:  # noqa: BLE001 - siehe Docstring
                errors += 1
                last_error = _poll_error_text(e)
                log.warning("TikTok status poll failed (%d/%d): %s", errors, POLL_MAX_ERRORS, last_error)
                if errors >= POLL_MAX_ERRORS:
                    return data, f"nicht abrufbar ({last_error})"
            status = str(data.get("status") or "")
            if status in ok_states:
                return data, None
            if status == "FAILED":
                reason = str(data.get("fail_reason") or "unbekannt")
                raise PublishError(
                    f"TikTok hat das Video abgelehnt: {_FAIL_REASONS.get(reason, reason)} ({reason})."
                )
            if self._clock() >= deadline:
                note = f"nach {int(self.poll_timeout_s)} s noch '{status or 'unbekannt'}'"
                return data, note + (f" (letzter Fehler: {last_error})" if errors and last_error else "")
            self._sleep(self.poll_interval_s)

    def _upload_verified(
        self, publish_id: str, upload_url: str, path: Path, size: int, chunk_size: int, count: int
    ) -> None:
        """Upload; meldet er einen Fehler, fragt TikTok, ob trotzdem alle Bytes angekommen sind (z. B. Antwort
        auf den letzten Chunk verloren) – dann wird weitergemacht statt 'failed' (TikTok veröffentlicht)."""
        try:
            self._upload(upload_url, path, size, chunk_size, count)
        except (PublishError, httpx.HTTPError) as e:
            data: dict[str, Any] = {}
            try:
                data = self._api("/v2/post/publish/status/fetch/", {"publish_id": publish_id})
            except Exception:  # noqa: BLE001 - Status unbekannt -> ursprünglicher Fehler gilt
                log.warning("TikTok status check after upload error failed")
            uploaded = data.get("uploaded_bytes")
            complete = isinstance(uploaded, int | float) and uploaded >= size
            if str(data.get("status") or "") not in UPLOADED_STATES and not complete:
                raise
            log.warning("TikTok upload reported an error, but TikTok has the whole video – continuing: %s", e)

    # ------------------------------------------------------------------ Publish

    def publish(self, req: PostRequest) -> PostResult:
        try:
            path = Path(req.video_path)
            if not path.is_file():
                return failed(f"Videodatei nicht gefunden: {path}")
            size = path.stat().st_size
            if self.cfg.mode == "direct":
                return self._publish_direct(req, path, size)
            return self._publish_inbox(path, size)
        except PublishError as e:
            log.warning("TikTok publish failed: %s", e)
            return failed(str(e))
        except httpx.HTTPError as e:
            log.warning("TikTok network error: %s", type(e).__name__)
            return failed(f"Netzwerkfehler bei TikTok ({type(e).__name__}): {redact(str(e))}")
        except Exception as e:  # noqa: BLE001 - Worker darf nicht abstürzen
            log.exception("TikTok publish crashed")
            return failed(f"Unerwarteter Fehler beim TikTok-Upload: {redact(str(e))}")

    def _source_info(self, size: int) -> dict[str, Any]:
        chunk_size, count = chunk_plan(size)
        return {
            "source": "FILE_UPLOAD",
            "video_size": size,
            "chunk_size": chunk_size,
            "total_chunk_count": count,
        }

    def _publish_inbox(self, path: Path, size: int) -> PostResult:
        source = self._source_info(size)
        data = self._api("/v2/post/publish/inbox/video/init/", {"source_info": source})
        publish_id, upload_url = data.get("publish_id"), data.get("upload_url")
        if not publish_id or not upload_url:
            raise PublishError("TikTok hat keine Upload-URL geliefert (inbox/video/init).")
        self._upload_verified(
            publish_id, upload_url, path, size, source["chunk_size"], source["total_chunk_count"]
        )
        # ab hier liegt das Video bei TikTok: nur ein ausdrückliches FAILED ist ein Fehlschlag
        _status, unclear = self._poll(publish_id, UPLOADED_STATES)
        note = None
        if unclear:
            note = f"Hinweis: TikTok-Status {unclear} – Entwurf bitte in der TikTok-App prüfen."
        log.info("TikTok draft sent to inbox (publish_id=%s)", publish_id)
        return PostResult(ok=True, remote_id=str(publish_id), status="draft_sent", error=note)

    def _publish_direct(self, req: PostRequest, path: Path, size: int) -> PostResult:
        info = self._api("/v2/post/publish/creator_info/query/", {})
        options = [str(o) for o in (info.get("privacy_level_options") or [])]
        wanted = self.cfg.privacy_level
        if wanted in options:
            privacy = wanted
        else:
            privacy = "SELF_ONLY"
            if wanted != "SELF_ONLY":
                log.warning(
                    "TikTok erlaubt Sichtbarkeit %s nicht (Optionen: %s) – poste als SELF_ONLY. %s",
                    wanted,
                    options or "keine",
                    _AUDIT_HINT,
                )
        max_dur = info.get("max_video_post_duration_sec")
        duration = probe_duration(path)
        if max_dur and duration and duration > float(max_dur) + 0.5:
            raise PublishError(
                f"Video ist {duration:.0f} s lang, TikTok erlaubt für dieses Konto max. {int(max_dur)} s."
            )
        caption = text.build_caption("tiktok", req.title, req.description)
        source = self._source_info(size)
        post_info = {
            "title": caption,
            "privacy_level": privacy,
            "disable_duet": bool(info.get("duet_disabled", False)),
            "disable_comment": bool(info.get("comment_disabled", False)),
            "disable_stitch": bool(info.get("stitch_disabled", False)),
            "video_cover_timestamp_ms": 1000,
            "brand_content_toggle": False,
            "brand_organic_toggle": False,
        }
        data = self._api("/v2/post/publish/video/init/", {"post_info": post_info, "source_info": source})
        publish_id, upload_url = data.get("publish_id"), data.get("upload_url")
        if not publish_id or not upload_url:
            raise PublishError("TikTok hat keine Upload-URL geliefert (video/init).")
        self._upload_verified(
            publish_id, upload_url, path, size, source["chunk_size"], source["total_chunk_count"]
        )
        # ab hier liegt das Video bei TikTok: nur ein ausdrückliches FAILED ist ein Fehlschlag
        status, unclear = self._poll(publish_id, {"PUBLISH_COMPLETE"})

        ids = _post_ids(status)
        remote_id = ids[0] if ids else str(publish_id)
        username = info.get("creator_username")
        remote_url = f"https://www.tiktok.com/@{username}/video/{remote_id}" if ids and username else None
        notes = []
        if privacy == "SELF_ONLY" and wanted != "SELF_ONLY":
            notes.append("Hinweis: als SELF_ONLY (nur für dich sichtbar) gepostet. " + _AUDIT_HINT)
        if unclear:
            notes.append(f"Hinweis: TikTok-Status {unclear} – Post bitte in der TikTok-App prüfen.")
        log.info("TikTok direct post done (publish_id=%s, privacy=%s)", publish_id, privacy)
        return PostResult(
            ok=True,
            remote_id=remote_id,
            remote_url=remote_url,
            status="posted",
            error=" ".join(notes) or None,
        )

    # ------------------------------------------------------------------ Metriken

    def fetch_metrics(self, remote_id: str) -> PostMetrics | None:
        if not remote_id:
            return None
        try:
            video_id = str(remote_id)
            if (
                not video_id.isdigit()
            ):  # publish_id (v_pub_.../v_inbox_...) -> öffentliche Post-ID nachschlagen
                data = self._api("/v2/post/publish/status/fetch/", {"publish_id": video_id})
                ids = _post_ids(data)
                if not ids:
                    return None
                video_id = ids[0]
            data = self._api(
                "/v2/video/query/", {"filters": {"video_ids": [video_id]}}, params={"fields": METRIC_FIELDS}
            )
            for v in data.get("videos") or []:
                if str(v.get("id")) == video_id:
                    return PostMetrics(
                        views=v.get("view_count"),
                        likes=v.get("like_count"),
                        comments=v.get("comment_count"),
                        shares=v.get("share_count"),
                    )
            return None
        except (PublishError, httpx.HTTPError) as e:
            log.warning("TikTok metrics for %s failed: %s", remote_id, redact(str(e)))
            return None
