"""Zugriffsschutz für das Dashboard (reine ASGI-Middleware, deckt Routen UND Mounts wie /media ab).

- HTTP Basic: Benutzername beliebig, Passwort = WDVI_DASHBOARD_PASSWORD (secrets.compare_digest).
- Ohne Passwort: nur direkte Zugriffe von localhost (kein Reverse-Proxy davor), sonst 403 mit Hinweis.
- POST & Co.: Origin- bzw. Referer-Host muss zum Host der Anfrage passen (einfacher CSRF-Schutz).
- Zu viele VERSCHIEDENE falsche Passwörter von einer Adresse -> kurz 429. Dasselbe falsche Passwort
  (z. B. veraltet im Handy gespeichert) zählt nur einmal: In Docker kommen alle Geräte über dieselbe
  Gateway-Adresse, ein Gerät mit altem Passwort soll nicht alle anderen aussperren.
- Sicherheits-Header (CSP, nosniff, kein Framing) für alle Antworten.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import math
import secrets
import time
from collections.abc import Callable, Iterable
from urllib.parse import urlsplit

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import HTMLResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = logging.getLogger(__name__)

LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient", "::ffff:127.0.0.1"})
# Diese Header setzen Reverse-Proxys/Tunnel -> dann ist "127.0.0.1" nicht wirklich lokal
PROXY_HEADERS = ("x-forwarded-for", "forwarded", "x-real-ip", "cf-connecting-ip", "tailscale-user-login")
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
PUBLIC_PREFIXES = ("/static/", "/fonts/")  # nur CSS/JS/Icons/Schriften – nichts Geheimes

CSP = (
    "default-src 'self'; img-src 'self' data: https:; media-src 'self' blob:; "
    "style-src 'self' 'unsafe-inline'; script-src 'self'; font-src 'self'; connect-src 'self'; "
    "manifest-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
SECURITY_HEADERS = (
    ("x-content-type-options", "nosniff"),
    ("x-frame-options", "DENY"),
    ("referrer-policy", "same-origin"),
    ("content-security-policy", CSP),
)

_PAGE = """<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title>
<link rel="stylesheet" href="/static/app.css"></head><body class="plain"><main class="container narrow">
<div class="card notice"><h1 class="page-title">{title}</h1>{body}</div></main></body></html>"""

NO_PASSWORD_HTML = _PAGE.format(
    title="Zugriff gesperrt",
    body=(
        "<p>Das Dashboard hat noch <strong>kein Passwort</strong>. Ohne Passwort ist es nur direkt auf "
        "dem Rechner erreichbar, auf dem WastedDailyVI läuft (<code>http://localhost:8420</code>).</p>"
        "<p>Für den Zugriff vom Handy, aus dem Netzwerk oder aus Docker heraus:</p><ol>"
        "<li>In der Datei <code>.env</code> die Zeile <code>WDVI_DASHBOARD_PASSWORD=ein-langes-passwort"
        "</code> eintragen.</li><li>WastedDailyVI neu starten (<code>docker compose up -d</code>).</li>"
        "<li>Beim Login: Benutzername beliebig, Passwort wie in der <code>.env</code>.</li></ol>"
    ),
)
LOGIN_HTML = _PAGE.format(
    title="Anmeldung erforderlich",
    body=(
        "<p>Bitte mit dem Dashboard-Passwort anmelden (Benutzername beliebig, Passwort = "
        "<code>WDVI_DASHBOARD_PASSWORD</code> aus der <code>.env</code>).</p>"
        "<p><a class=\"btn primary\" href=\"/\">Erneut versuchen</a></p>"
    ),
)
TOO_MANY_HTML = _PAGE.format(
    title="Zu viele Fehlversuche",
    body=(
        "<p>Von dieser Adresse kamen zu viele verschiedene falsche Passwörter. Anmelden ist deshalb "
        "für etwa <strong>{minutes} Minuten</strong> gesperrt – auch mit dem richtigen Passwort.</p>"
        "<p>Hinweis: Läuft WastedDailyVI in Docker, kommen oft alle Geräte (PC, Handy über Tailscale) "
        "über dieselbe Adresse und sind gemeinsam gesperrt. Danach auf allen Geräten das gespeicherte "
        "Passwort prüfen (<code>WDVI_DASHBOARD_PASSWORD</code> aus der <code>.env</code>).</p>"
    ),
)  # {minutes} wird pro Antwort ersetzt
CSRF_HTML = _PAGE.format(
    title="Anfrage blockiert",
    body=(
        "<p>Diese Aktion kam nicht von einer Dashboard-Seite (Origin/Referer passt nicht) und wurde "
        "zum Schutz blockiert. Bitte die Seite neu laden und es noch einmal versuchen.</p>"
        "<p><a class=\"btn primary\" href=\"/\">Zum Dashboard</a></p>"
    ),
)


def _split_hostport(hostport: str) -> tuple[str, int | None] | None:
    """'example.com:8420' / '[::1]:8420' -> (host, port|None)."""
    try:
        p = urlsplit("//" + hostport.strip())
        host = (p.hostname or "").lower()
        return (host, p.port) if host else None
    except ValueError:
        return None


def _origin_parts(value: str) -> tuple[str, int] | None:
    """'https://example.com' -> ('example.com', 443)."""
    try:
        p = urlsplit(value.strip())
        if p.scheme not in ("http", "https") or not p.hostname:
            return None
        return p.hostname.lower(), p.port or (443 if p.scheme == "https" else 80)
    except ValueError:
        return None


def host_matches(origin: tuple[str, int], hostport: str) -> bool:
    hp = _split_hostport(hostport)
    if hp is None:
        return False
    host, port = hp
    if host != origin[0]:
        return False
    if port is None:  # Host-Header ohne Port: Standardport (80/443, auch hinter TLS-Proxy)
        return origin[1] in (80, 443)
    return port == origin[1]


def origin_allowed(headers: Headers, extra_hosts: Iterable[str] = ()) -> bool:
    """Origin (oder ersatzweise Referer) muss zum Host der Anfrage passen."""
    src = headers.get("origin")
    if not src or src.strip().lower() == "null":
        src = headers.get("referer")
    if not src:
        return False
    origin = _origin_parts(src)
    if origin is None:
        return False
    candidates = [headers.get("host"), headers.get("x-forwarded-host"), *extra_hosts]
    for cand in candidates:
        if not cand:
            continue
        for part in cand.split(","):
            if host_matches(origin, part):
                return True
    return False


class _FailureTracker:
    """Merkt sich falsche Passwörter je Client-Adresse (nur im Speicher und nur als HMAC, nie im Klartext).
    Gezählt werden verschiedene Passwörter: Wiederholt dasselbe falsche ist kein Rateversuch."""

    def __init__(self, max_failures: int, window_s: float, clock: Callable[[], float]) -> None:
        self.max_failures = max_failures
        self.window_s = window_s
        self.clock = clock
        self._key = secrets.token_bytes(32)
        self._fails: dict[str, dict[str, float]] = {}

    def _digest(self, password: str) -> str:
        return hmac.new(self._key, password.encode("utf-8", "replace"), hashlib.sha256).hexdigest()

    def _prune(self, host: str, now: float) -> dict[str, float]:
        entries = {d: t for d, t in self._fails.get(host, {}).items() if now - t < self.window_s}
        if entries:
            self._fails[host] = entries
        else:
            self._fails.pop(host, None)
        return entries

    def blocked(self, host: str) -> bool:
        return len(self._prune(host, self.clock())) >= self.max_failures

    def retry_after(self, host: str) -> int:
        """Sekunden, bis wieder ein Versuch frei ist (0 = nicht gesperrt)."""
        now = self.clock()
        times = sorted(self._prune(host, now).values())
        if len(times) < self.max_failures:
            return 0
        oldest_needed = times[len(times) - self.max_failures]
        return max(1, math.ceil(self.window_s - (now - oldest_needed)))

    def record(self, host: str, password: str) -> None:
        now = self.clock()
        if len(self._fails) > 1000:  # Speicher begrenzen
            for h in list(self._fails)[:500]:
                self._fails.pop(h, None)
        self._fails.setdefault(host, {})[self._digest(password)] = now

    def clear(self, host: str) -> None:
        self._fails.pop(host, None)


class SecurityMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        password: Callable[[], str | None],
        extra_hosts: Iterable[str] = (),
        max_failures: int = 10,
        window_s: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.app = app
        self.password = password
        self.extra_hosts = tuple(h for h in extra_hosts if h)
        self.failures = _FailureTracker(max_failures, window_s, clock)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                hdrs = MutableHeaders(scope=message)
                for k, v in SECURITY_HEADERS:
                    hdrs.setdefault(k, v)
                ctype = hdrs.get("content-type", "")
                if ctype.startswith(("text/html", "application/json")):
                    hdrs.setdefault("cache-control", "no-store")
            await send(message)

        method = str(scope.get("method", "GET")).upper()
        path = str(scope.get("path", ""))
        headers = Headers(scope=scope)
        public = method in ("GET", "HEAD") and path.startswith(PUBLIC_PREFIXES) and ".." not in path
        if not public:
            denied = self._check_auth(scope, headers)
            if denied is not None:
                await denied(scope, receive, send_with_headers)
                return
        if method not in SAFE_METHODS and not origin_allowed(headers, self.extra_hosts):
            log.warning("Blocked %s %s: Origin/Referer does not match host", method, path)
            await HTMLResponse(CSRF_HTML, status_code=403)(scope, receive, send_with_headers)
            return
        await self.app(scope, receive, send_with_headers)

    def _check_auth(self, scope: Scope, headers: Headers) -> Response | None:
        client = scope.get("client")
        host = str(client[0]) if client else ""
        pw = self.password()
        if not pw:
            proxied = any(h in headers for h in PROXY_HEADERS)
            if host in LOCAL_HOSTS and not proxied:
                return None
            log.warning("Dashboard request from %s refused: no WDVI_DASHBOARD_PASSWORD set", host or "?")
            return HTMLResponse(NO_PASSWORD_HTML, status_code=403)

        if self.failures.blocked(host):
            wait = self.failures.retry_after(host) or int(self.failures.window_s)
            return HTMLResponse(
                TOO_MANY_HTML.replace("{minutes}", str(max(1, math.ceil(wait / 60)))),
                status_code=429,
                headers={"Retry-After": str(wait)},
            )
        given = _basic_password(headers.get("authorization"))
        if given is None:
            return self._unauthorized()
        if secrets.compare_digest(given.encode("utf-8"), pw.encode("utf-8")):
            self.failures.clear(host)
            return None
        self.failures.record(host, given)
        log.warning("Dashboard login failed from %s", host or "?")
        return self._unauthorized()

    @staticmethod
    def _unauthorized() -> Response:
        return HTMLResponse(
            LOGIN_HTML,
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="WastedDailyVI", charset="UTF-8"'},
        )


def _basic_password(header: str | None) -> str | None:
    """Passwort aus 'Authorization: Basic ...' (Benutzername wird ignoriert)."""
    if not header:
        return None
    scheme, _, param = header.partition(" ")
    if scheme.lower() != "basic" or not param.strip():
        return None
    try:
        raw = base64.b64decode(param.strip(), validate=True)
    except (binascii.Error, ValueError):
        return None
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError:
        decoded = raw.decode("latin-1")
    _, sep, password = decoded.partition(":")
    return password if sep else None
