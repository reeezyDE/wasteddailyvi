"""Kurzlebiger Zustand im Speicher: OAuth-`state`-Werte und Flash-Meldungen (nach Redirects)."""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable
from urllib.parse import parse_qsl, urlsplit

OAUTH_STATE_TTL_S = 15 * 60
FLASH_TTL_S = 10 * 60
MAX_PASTE_CHARS = 8000


class OAuthStates:
    """Offene OAuth-Anmeldungen: state -> (Plattform, Zeitpunkt). Einmalig gültig, 15 min."""

    def __init__(self, ttl_s: float = OAUTH_STATE_TTL_S, clock: Callable[[], float] = time.time) -> None:
        self.ttl_s = ttl_s
        self.clock = clock
        self._states: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        for k in [k for k, (_, t) in self._states.items() if now - t >= self.ttl_s]:
            del self._states[k]
        while len(self._states) > 100:  # Speicher begrenzen: älteste zuerst raus
            del self._states[min(self._states, key=lambda k: self._states[k][1])]

    def create(self, platform: str) -> str:
        state = secrets.token_urlsafe(24)
        with self._lock:
            now = self.clock()
            self._states[state] = (platform, now)
            self._prune(now)
        return state

    def discard(self, state: str) -> None:
        with self._lock:
            self._states.pop(state, None)

    def consume(self, platform: str, state: str | None) -> bool:
        """True, wenn `state` offen, nicht abgelaufen und für diese Plattform ist (danach verbraucht)."""
        if not state:
            return False
        with self._lock:
            self._prune(self.clock())
            entry = self._states.get(state)
            if entry is None or entry[0] != platform:
                return False
            del self._states[state]
            return True

    def __len__(self) -> int:
        return len(self._states)


class FlashStore:
    """Meldung für die nächste Seite: Token in der Redirect-URL, Text bleibt auf dem Server."""

    def __init__(self, ttl_s: float = FLASH_TTL_S, clock: Callable[[], float] = time.time) -> None:
        self.ttl_s = ttl_s
        self.clock = clock
        self._items: dict[str, tuple[str, str, float]] = {}
        self._lock = threading.Lock()

    def add(self, kind: str, text: str) -> str:
        token = secrets.token_urlsafe(9)
        with self._lock:
            now = self.clock()
            for k in [k for k, (_, _, t) in self._items.items() if now - t >= self.ttl_s]:
                del self._items[k]
            while len(self._items) >= 200:
                del self._items[next(iter(self._items))]
            self._items[token] = (kind, text, now)
        return token

    def pop(self, token: str | None) -> dict[str, str] | None:
        if not token:
            return None
        with self._lock:
            item = self._items.pop(token, None)
        if item is None or self.clock() - item[2] >= self.ttl_s:
            return None
        return {"kind": item[0], "text": item[1]}


def parse_pasted_redirect(text: str) -> dict[str, str]:
    """Eingefügte Redirect-URL (oder nur deren Query-String) -> Parameter-Dict.

    Liest Query und – falls dort Parameter stehen – auch das Fragment (#...). Instagram hängt '#_' an,
    das wird ignoriert.
    """
    text = (text or "").strip()[:MAX_PASTE_CHARS]
    if not text:
        return {}
    if "://" in text or text.startswith("/"):
        try:
            parts = urlsplit(text)
        except ValueError:
            return {}
        chunks = [parts.query, parts.fragment if "=" in parts.fragment else ""]
    else:
        q, _, frag = text.lstrip("?").partition("#")
        chunks = [q, frag if "=" in frag else ""]
    params: dict[str, str] = {}
    for chunk in chunks:
        if not chunk:
            continue
        for k, v in parse_qsl(chunk, keep_blank_values=True):
            k = k.strip()
            if k and k not in params:
                params[k] = v.strip()
    return params
