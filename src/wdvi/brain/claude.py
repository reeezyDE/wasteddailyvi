"""Claude-Anbindung mit zwei Backends:

- ApiLLM: Anthropic-SDK (`claude-opus-5-5`, adaptive thinking, effort aus config, structured outputs
  via output_config.format json_schema, server-side refusal fallbacks "default", prompt caching
  des System-Prompts). Bilder als base64. Gestreamt (`.get_final_message()`), damit auch große
  max_tokens (hoher effort) nicht in HTTP-Timeouts laufen.
- CliLLM: lokales `claude -p` (Claude-Code-Login / Abo) mit eigenem System-Prompt, nur dem Read-Tool
  (auf den Frame-Ordner beschränkt), `--json-schema` (structured output) und ohne Session-Ablage.
  Bilder werden als Dateipfade übergeben und über das Read-Tool gelesen; Antwort wird validiert,
  1 Reparatur-Retry.

Interne Content-Blöcke (backend-neutral):
  {"type": "text", "text": "..."}
  {"type": "image", "path": "/abs/pfad.jpg"}            (optional "t": Quellzeit in s, nur für Beschriftung)

Beide Backends teilen sich einen prozessweiten RateLimiter (llm.max_calls_per_hour; jeder Request zählt,
auch der Reparatur-Turn) und einen SpendTracker (geschätzte USD-Kosten der letzten 24 h, in
data_dir/llm_spend.json gespeichert; optionales Tageslimit `llm.max_usd_per_day`, 0 = keine Grenze).
Fehler kommen immer als LLMError/LLMRefusal mit deutscher Meldung; `LLMError.retryable`
sagt, ob ein späterer neuer Versuch sinnvoll ist (Rate-Limit, 5xx, Netz, Tagesbudget).
"""

from __future__ import annotations

import base64
import copy
import functools
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, TypeVar, get_args, get_origin

import anthropic
from pydantic import BaseModel, ValidationError

from wdvi.config import Settings

try:  # Transportfehler mitten im Stream wrappt das SDK nicht in APIConnectionError
    import httpx2 as _httpx
except ImportError:  # pragma: no cover - ältere SDKs nutzen httpx
    import httpx as _httpx

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
CLI_MAX_TURNS = 6  # Minimum; mit Bildern max(6, Bilder + 3), falls Claude die Frames einzeln liest
_MAX_IMAGE_B64 = 5 * 1024 * 1024  # API-Limit pro Bild (base64)
_REPAIR_PREVIEW_CHARS = 4000
SPEND_FILE = "llm_spend.json"  # in paths.data_dir
_RETRYABLE_ERROR_TYPES = {"overloaded_error", "api_error", "rate_limit_error", "timeout_error"}

# USD pro 1 Mio. Tokens: (input, output, cache_read, cache_write 5 min) – nur für die Kostenschätzung
_PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-opus-5-5": (4.0, 20.0, 0.20, 5.0),
    "claude-opus-5": (5.0, 25.0, 0.50, 6.25),
    "claude-opus-4": (5.0, 25.0, 0.50, 6.25),
    "claude-sonnet-5": (2.0, 10.0, 0.20, 2.50),
    "claude-sonnet-4": (3.0, 15.0, 0.30, 3.75),
    "claude-haiku-5": (0.10, 0.50, 0.01, 0.125),
    "claude-haiku-4": (1.0, 5.0, 0.10, 1.25),
    "claude-fable-5": (10.0, 50.0, 0.25, 12.5),
}
_DEFAULT_PRICE = _PRICES["claude-opus-5-5"]


class LLMError(RuntimeError):
    """Fehler beim LLM-Aufruf. `retryable=True`: ein späterer neuer Versuch kann klappen."""

    def __init__(self, message: str = "", *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class LLMRefusal(LLMError):
    """Claude (bzw. die ganze Fallback-Kette) hat abgelehnt (stop_reason "refusal")."""

    def __init__(self, message: str = "", *, category: str | None = None,
                 explanation: str | None = None) -> None:
        super().__init__(message, retryable=False)
        self.category = category
        self.explanation = explanation


class LLMClient(Protocol):
    def complete_json(self, system: str, content: list[dict[str, Any]], schema_model: type[BaseModel],
                      *, max_tokens: int = 16000) -> BaseModel:
        """Ein Aufruf, Ergebnis als validierte Instanz von schema_model. Wirft LLMError/LLMRefusal."""
        ...


# --------------------------------------------------------------------------- Rate-Limit


class RateLimiter:
    """Gleitendes 1-Stunden-Fenster, thread-safe. max_calls_per_hour <= 0 = unbegrenzt."""

    def __init__(self, max_calls_per_hour: int, *, window_s: float = 3600.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.max_calls = int(max_calls_per_hour)
        self.window_s = float(window_s)
        self._clock = clock
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        while self._calls and now - self._calls[0] >= self.window_s:
            self._calls.popleft()

    def remaining(self) -> int:
        if self.max_calls <= 0:
            return sys.maxsize
        with self._lock:
            self._prune(self._clock())
            return max(0, self.max_calls - len(self._calls))

    def acquire(self) -> None:
        """Zählt einen Aufruf oder wirft LLMError (retryable), wenn das Stundenlimit erreicht ist."""
        with self._lock:
            now = self._clock()
            self._prune(now)
            if self.max_calls > 0 and len(self._calls) >= self.max_calls:
                wait_min = max(1, math.ceil((self.window_s - (now - self._calls[0])) / 60.0))
                raise LLMError(
                    f"Stundenlimit für Claude-Aufrufe erreicht ({self.max_calls}/h, llm.max_calls_per_hour) – "
                    f"nächster Aufruf in ca. {wait_min} min möglich.", retryable=True)
            self._calls.append(now)

    def record(self) -> None:
        """Zählt einen zusätzlichen Request (z. B. Reparatur-Turn), ohne zu blockieren – der erste,
        schon bezahlte Aufruf soll nicht am Limit scheitern."""
        with self._lock:
            now = self._clock()
            self._prune(now)
            self._calls.append(now)

    def reset(self) -> None:
        with self._lock:
            self._calls.clear()


_shared_lock = threading.Lock()
_shared_limiter: RateLimiter | None = None


def shared_rate_limiter(max_calls_per_hour: int) -> RateLimiter:
    """Prozessweiter Limiter für alle Backends (Limit wird bei Bedarf aktualisiert)."""
    global _shared_limiter
    with _shared_lock:
        if _shared_limiter is None:
            _shared_limiter = RateLimiter(max_calls_per_hour)
        else:
            _shared_limiter.max_calls = int(max_calls_per_hour)
        return _shared_limiter


# --------------------------------------------------------------------------- Kosten


class SpendTracker:
    """Geschätzte Claude-Kosten (USD) im gleitenden 24-h-Fenster, thread-safe. Mit `path` wird das Fenster
    als JSON gespeichert, damit ein Neustart ein Tagesbudget nicht zurücksetzt."""

    def __init__(self, path: Path | None = None, *, window_s: float = 86400.0,
                 clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path) if path is not None else None
        self.window_s = float(window_s)
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: deque[tuple[float, float]] = deque()
        self._load()

    def _load(self) -> None:
        if self.path is None:
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        now = self._clock()
        entries = []
        for item in raw if isinstance(raw, list) else []:
            try:
                ts, usd = float(item[0]), float(item[1])
            except (TypeError, ValueError, IndexError, KeyError):
                continue
            if math.isfinite(ts) and math.isfinite(usd) and usd > 0 and 0 <= now - ts < self.window_s:
                entries.append((ts, usd))
        self._entries = deque(sorted(entries))

    def _prune(self, now: float) -> None:
        while self._entries and now - self._entries[0][0] >= self.window_s:
            self._entries.popleft()

    def add(self, usd: float) -> None:
        if not isinstance(usd, int | float) or not math.isfinite(usd) or usd <= 0:
            return
        with self._lock:
            now = self._clock()
            self._prune(now)
            self._entries.append((now, float(usd)))
            self._save()

    def total(self) -> float:
        """Summe der letzten 24 h in USD."""
        with self._lock:
            self._prune(self._clock())
            return sum(u for _, u in self._entries)

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps([[round(t, 3), round(u, 6)] for t, u in self._entries]), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("claude: spend ledger %s not writable (%s)", self.path, e)


_spend_trackers: dict[str, SpendTracker] = {}


def shared_spend_tracker(path: Path | None) -> SpendTracker:
    """Ein SpendTracker pro Ledger-Datei und Prozess (für alle Backends)."""
    key = str(Path(path).resolve()) if path is not None else ""
    with _shared_lock:
        tracker = _spend_trackers.get(key)
        if tracker is None:
            tracker = _spend_trackers[key] = SpendTracker(Path(key) if key else None)
        return tracker


def _spend_path(settings: Settings) -> Path:
    return Path(settings.config.paths.data_dir) / SPEND_FILE


def _price(model: str | None) -> tuple[float, float, float, float]:
    m = (model or "").lower().removeprefix("anthropic.")
    best = max((k for k in _PRICES if m.startswith(k)), key=len, default=None)
    return _PRICES[best] if best else _DEFAULT_PRICE


def _tokens(v: Any) -> int:
    if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v) or v <= 0:
        return 0
    return int(v)


def estimate_cost_usd(usage: Any, served_model: str | None, requested_model: str) -> float:
    """Geschätzte Kosten eines API-Requests in USD. `usage.iterations` (je Fallback-Versuch) hat Vorrang vor
    dem Top-Level-usage, das nur den letzten Versuch enthält."""
    if usage is None:
        return 0.0
    iters = _get(usage, "iterations")
    entries = list(iters) if isinstance(iters, list | tuple) and iters else [usage]
    total = 0.0
    for it in entries:
        model = _get(it, "model")
        if not isinstance(model, str) or not model:
            fallback = str(_get(it, "type") or "").startswith("fallback")
            model = (served_model if fallback or it is usage else None) or requested_model
        p_in, p_out, p_read, p_write = _price(model)
        total += (_tokens(_get(it, "input_tokens")) * p_in + _tokens(_get(it, "output_tokens")) * p_out
                  + _tokens(_get(it, "cache_read_input_tokens")) * p_read
                  + _tokens(_get(it, "cache_creation_input_tokens")) * p_write) / 1e6
    return total


def _daily_budget(cfg: Any) -> float:
    """llm.max_usd_per_day (Standard 0 = unbegrenzt; <= 0, ungültig oder fehlend ebenfalls unbegrenzt)."""
    v = getattr(cfg, "max_usd_per_day", None)
    try:
        f = float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0
    return f if math.isfinite(f) and f > 0 else 0.0


def _check_budget(cfg: Any, tracker: SpendTracker) -> None:
    cap = _daily_budget(cfg)
    if cap <= 0:
        return
    spent = tracker.total()
    if spent >= cap:
        raise LLMError(f"Claude-Tagesbudget erreicht (usage limit): ca. {spent:.2f} $ von {cap:.2f} $ in den "
                       "letzten 24 h (llm.max_usd_per_day) – weitere Aufrufe später.", retryable=True)


# --------------------------------------------------------------------------- Schema

_KEEP_FORMATS = {"date-time", "date", "uri"}
_COPY_KEYS = ("type", "enum", "const", "description")


def strict_schema(model: type[BaseModel]) -> dict:
    """JSON-Schema für structured outputs: $defs inline, additionalProperties: false überall,
    alle Properties required (optionale als anyOf [T, null]), nicht unterstützte Keywords
    (minimum, maximum, minLength, maxLength, pattern, default, title, ...) entfernt; tuple-Felder
    (prefixItems) zu array mit items vereinfacht (Länge steht dann in der description). Modell-Docstrings
    (deutsch, für Entwickler) landen nicht im Schema; Feld-descriptions bleiben."""
    return copy.deepcopy(_strict_schema_cached(model))


@functools.lru_cache(maxsize=64)
def _strict_schema_cached(model: type[BaseModel]) -> dict:
    raw = dict(model.model_json_schema())
    raw.pop("description", None)
    defs = dict(raw.get("$defs") or raw.get("definitions") or {})
    return _strictify(raw, defs, ())


def _ref_name(ref: str) -> str:
    for prefix in ("#/$defs/", "#/definitions/"):
        if ref.startswith(prefix):
            return ref[len(prefix):]
    raise ValueError(f"Nicht unterstützte Schema-Referenz: {ref}")


def _dedupe_schemas(items: list[Any]) -> list[Any]:
    seen: set[str] = set()
    out = []
    for it in items:
        key = json.dumps(it, sort_keys=True)
        if key not in seen:
            seen.add(key)
            out.append(it)
    return out


def _strictify(node: Any, defs: dict[str, Any], stack: tuple[str, ...]) -> Any:
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        name = _ref_name(node["$ref"])
        if name in stack:
            raise ValueError(f"Rekursives Schema ({name}) wird von structured outputs nicht unterstützt")
        if name not in defs:
            raise ValueError(f"Schema-Definition fehlt: {name}")
        # description der Definition = Klassen-Docstring -> weglassen; die des Feldes (node) bleibt
        base = {k: v for k, v in defs[name].items() if k != "description"}
        merged = {**base, **{k: v for k, v in node.items() if k != "$ref"}}
        return _strictify(merged, defs, stack + (name,))
    if isinstance(node.get("allOf"), list) and len(node["allOf"]) == 1:
        merged = {**node["allOf"][0], **{k: v for k, v in node.items() if k != "allOf"}}
        return _strictify(merged, defs, stack)

    out: dict[str, Any] = {}
    for key in _COPY_KEYS:
        if key in node:
            out[key] = copy.deepcopy(node[key])
    if node.get("format") in _KEEP_FORMATS:
        out["format"] = node["format"]
    for key in ("anyOf", "oneOf"):  # oneOf ist nicht unterstützt -> anyOf
        if isinstance(node.get(key), list):
            subs = [_strictify(s, defs, stack) for s in node[key]]
            out["anyOf"] = _dedupe_schemas(out.get("anyOf", []) + subs)
    if isinstance(node.get("allOf"), list):
        out["allOf"] = [_strictify(s, defs, stack) for s in node["allOf"]]

    # Arrays (Tupel -> array mit gemeinsamem items-Schema)
    if isinstance(node.get("prefixItems"), list):
        prefix = node["prefixItems"]
        subs = _dedupe_schemas([_strictify(s, defs, stack) for s in prefix])
        out["type"] = "array"
        if len(subs) == 1:
            out["items"] = subs[0]
        elif subs:
            out["items"] = {"anyOf": subs}
        # minItems/maxItems > 1 sind nicht erlaubt -> feste Länge wenigstens beschreiben
        kinds = [str(s.get("type", "value")) if isinstance(s, dict) else "value" for s in prefix]
        out.setdefault("description", f"Exactly {len(prefix)} items: [{', '.join(kinds)}]")
    elif isinstance(node.get("items"), dict):
        out["items"] = _strictify(node["items"], defs, stack)
    if node.get("minItems") in (0, 1) and out.get("type") == "array":
        out["minItems"] = node["minItems"]

    # Objekte: geschlossen, alles required
    if node.get("type") == "object" or "properties" in node:
        props = node.get("properties") or {}
        out["type"] = "object"
        out["properties"] = {name: _strictify(sub, defs, stack) for name, sub in props.items()}
        out["required"] = list(props)
        out["additionalProperties"] = False
    return out


# --------------------------------------------------------------------------- JSON-Parsing


class _ParseError(Exception):
    pass


def _strip_fences(text: str) -> str:
    m = re.search(r"```(?:json|JSON)?\s*\n?(.*?)```", text, re.S)
    return m.group(1).strip() if m else text


def _extract_json_object(text: str) -> str:
    """Erstes balanciertes {...}-Objekt (Strings/Escapes werden beachtet)."""
    s = _strip_fences(text.strip())
    start = s.find("{")
    if start < 0:
        raise _ParseError("Kein JSON-Objekt in der Antwort gefunden")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return s[start:i + 1]
    raise _ParseError("JSON-Objekt ist unvollständig (abgeschnitten?)")


def _format_validation_error(e: ValidationError) -> str:
    parts = []
    for err in e.errors()[:12]:
        loc = ".".join(str(x) for x in err.get("loc", ())) or "(root)"
        parts.append(f"{loc}: {err.get('msg', 'invalid')}")
    more = len(e.errors()) - len(parts)
    if more > 0:
        parts.append(f"... (+{more} weitere)")
    return "; ".join(parts)


def _tuple_list_spec(annotation: Any) -> tuple[int, bool] | None:
    """list[tuple[A, B, ...]] -> (Länge, nur Zahlen?); sonst None."""
    if get_origin(annotation) is not list:
        return None
    args = get_args(annotation)
    if len(args) != 1 or get_origin(args[0]) is not tuple:
        return None
    items = get_args(args[0])
    if not items or Ellipsis in items:
        return None
    return len(items), all(t in (int, float) for t in items)


def _coerce_tuple_lists(data: dict, schema_model: type[BaseModel]) -> dict:
    """structured outputs kennt keine feste Array-Länge: bei list[tuple]-Feldern überzählige Werte
    abschneiden und zu kurze/kaputte Einträge verwerfen, statt dafür einen Reparatur-Turn zu bezahlen."""
    for name, field in schema_model.model_fields.items():
        key = field.alias or name
        val = data.get(key)
        spec = _tuple_list_spec(field.annotation)
        if spec is None or not isinstance(val, list):
            continue
        n, numeric = spec
        out = []
        for entry in val:
            if not isinstance(entry, list) or len(entry) < n:
                continue
            head = entry[:n]
            if numeric and not all(isinstance(x, int | float) and not isinstance(x, bool) for x in head):
                continue
            out.append(head)
        data[key] = out
    return data


def _parse_json_model(text: str, schema_model: type[T]) -> T:
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        try:
            data = json.loads(_extract_json_object(text or ""))
        except json.JSONDecodeError as e:
            raise _ParseError(f"Ungültiges JSON: {e}") from e
    if not isinstance(data, dict):
        raise _ParseError("Antwort ist kein JSON-Objekt")
    try:
        return schema_model.model_validate(_coerce_tuple_lists(data, schema_model))
    except ValidationError as e:
        raise _ParseError(_format_validation_error(e)) from e


def _tail(text: str | None, n: int = 600) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else "…" + text[-n:]


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _error_type(body: Any) -> str | None:
    """API-Fehlertyp aus dem Body ({"type": "error", "error": {"type": "overloaded_error", ...}})."""
    err = _get(body, "error") if body is not None else None
    kind = _get(err, "type") if err is not None else None
    return kind if isinstance(kind, str) else None


def _echo_content(blocks: Any) -> Any:
    """Assistant-Content für den Reparatur-Turn: nach einem Fallback mitten in der Antwort nur die Text-Blöcke
    vor dem letzten fallback-Block zurückgeben (Thinking/Tool-Blöcke des abgelehnten Versuchs nicht)."""
    items = list(blocks or [])
    last_fb = max((i for i, b in enumerate(items) if _get(b, "type") == "fallback"), default=-1)
    if last_fb < 0:
        return blocks
    return [b for b in items[:last_fb] if _get(b, "type") == "text"] + items[last_fb:]


def _sniff_media_type(data: bytes, path: str) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:4] == b"GIF8":
        return "image/gif"
    suffix = Path(path).suffix.lower()
    return {".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}.get(suffix, "image/jpeg")


# --------------------------------------------------------------------------- API-Backend


class ApiLLM:
    """Anthropic-API. `client` injizierbar (Tests); sonst lazy anthropic.Anthropic(max_retries=4)."""

    def __init__(self, settings: Settings, client: Any = None, *, rate_limiter: RateLimiter | None = None,
                 spend_tracker: SpendTracker | None = None) -> None:
        self.settings = settings
        self.cfg = settings.config.llm
        self._client = client
        self.rate_limiter = rate_limiter or shared_rate_limiter(self.cfg.max_calls_per_hour)
        self.spend = spend_tracker or shared_spend_tracker(_spend_path(settings))

    def spend_last_24h_usd(self) -> float:
        """Geschätzte Claude-Kosten der letzten 24 h (für Status/Dashboard)."""
        return self.spend.total()

    @property
    def client(self) -> Any:
        if self._client is None:
            kwargs: dict[str, Any] = {"max_retries": 4}
            # Key nur aus .env (pydantic-settings) -> explizit übergeben; das SDK liest nur os.environ
            key = self.settings.secrets.anthropic_api_key
            if key and not os.environ.get("ANTHROPIC_API_KEY"):
                kwargs["api_key"] = key
            self._client = anthropic.Anthropic(**kwargs)
        return self._client

    def complete_json(self, system: str, content: list[dict[str, Any]], schema_model: type[T],
                      *, max_tokens: int = 16000) -> T:
        _check_budget(self.cfg, self.spend)
        self.rate_limiter.acquire()
        schema = strict_schema(schema_model)
        messages: list[dict[str, Any]] = [{"role": "user", "content": self._api_blocks(content)}]
        resp = self._create(system, messages, schema, max_tokens)
        text = self._response_text(resp, max_tokens)
        try:
            return _parse_json_model(text, schema_model)
        except _ParseError as e:
            log.warning("claude api: answer failed validation (%s), sending one repair turn", e)
            first_error = e
        # Reparatur: Antwort unverändert zurückgeben (Thinking-Blöcke sind opak), dann Fehlerbeschreibung
        messages = messages + [
            {"role": "assistant", "content": _echo_content(resp.content)},
            {"role": "user", "content": [{"type": "text", "text": (
                f"Your JSON answer did not pass validation: {first_error}\n"
                "Return the complete, corrected JSON object that matches the schema exactly.")}]},
        ]
        self.rate_limiter.record()  # der Reparatur-Request kostet extra und zählt mit
        resp2 = self._create(system, messages, schema, max_tokens)
        text2 = self._response_text(resp2, max_tokens)
        try:
            return _parse_json_model(text2, schema_model)
        except _ParseError as e:
            raise LLMError(f"Claude-Antwort passt nicht zum Schema {schema_model.__name__}, "
                           f"auch nach einem Reparaturversuch: {e}") from e

    # -- intern

    def _api_blocks(self, content: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for b in content:
            kind = b.get("type")
            if kind == "text":
                if b.get("text"):
                    out.append({"type": "text", "text": str(b["text"])})
            elif kind == "image":
                if "source" in b:  # schon im API-Format
                    out.append({"type": "image", "source": b["source"]})
                    continue
                path = str(b.get("path") or "")
                try:
                    data = Path(path).read_bytes()
                except OSError as e:
                    log.warning("claude api: image %s not readable (%s) – skipped", path, e)
                    continue
                b64 = base64.standard_b64encode(data).decode("ascii")
                if len(b64) > _MAX_IMAGE_B64:
                    log.warning("claude api: image %s too large for the API (%d bytes) – skipped", path, len(data))
                    continue
                out.append({"type": "image", "source": {"type": "base64",
                                                        "media_type": _sniff_media_type(data, path),
                                                        "data": b64}})
            else:
                raise LLMError(f"Unbekannter Content-Block-Typ für Claude: {kind!r}")
        if not out:
            raise LLMError("Leerer Prompt – keine Inhalte für Claude.")
        return out

    def _create(self, system: str, messages: list[dict[str, Any]], schema: dict, max_tokens: int) -> Any:
        kwargs: dict[str, Any] = {
            "model": self.cfg.model,
            "max_tokens": max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": messages,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": self.cfg.effort, "format": {"type": "json_schema", "schema": schema}},
        }
        model = self.cfg.model
        t0 = time.monotonic()
        try:
            # gestreamt: hoher effort + großes max_tokens würde sonst in HTTP-Timeouts laufen
            if self.cfg.use_fallbacks:
                manager = self.client.beta.messages.stream(**kwargs, betas=[FALLBACK_BETA], fallbacks="default")
            else:
                manager = self.client.messages.stream(**kwargs)
            with manager as stream:
                resp = stream.get_final_message()
        except anthropic.RateLimitError as e:
            raise LLMError("Anthropic-Rate-Limit (429) trotz automatischer Wiederholungen – später erneut "
                           "versuchen.", retryable=True) from e
        except anthropic.BadRequestError as e:
            raise LLMError(f"Anthropic hat die Anfrage abgelehnt (400): {_tail(e.message, 500)}") from e
        except anthropic.AuthenticationError as e:
            raise LLMError("Anthropic-API-Key fehlt oder ist ungültig (401) – ANTHROPIC_API_KEY prüfen "
                           "oder llm.backend: cli verwenden.") from e
        except anthropic.PermissionDeniedError as e:
            raise LLMError(f"Keine Berechtigung für das Modell {model} (403) – Konto/Workspace prüfen.") from e
        except anthropic.NotFoundError as e:
            raise LLMError(f"Modell {model} nicht gefunden (404) – llm.model in config.yaml prüfen.") from e
        except anthropic.APIStatusError as e:
            code = e.status_code
            kind = _error_type(e.body)
            if kind == "rate_limit_error":  # Fehler-Event mitten im Stream (HTTP-Status war 200)
                raise LLMError("Anthropic-Rate-Limit (429) während der Antwort – später erneut versuchen.",
                               retryable=True) from e
            if code >= 500 or kind in _RETRYABLE_ERROR_TYPES:
                label = code if code >= 400 else kind
                raise LLMError(f"Anthropic-Serverfehler ({label}) – später erneut versuchen.",
                               retryable=True) from e
            raise LLMError(f"Anthropic-API-Fehler {code}: {_tail(e.message, 500)}", retryable=code in (408, 409)) from e
        except anthropic.APIConnectionError as e:
            raise LLMError("Keine Verbindung zur Anthropic-API (Netzwerk/Timeout) – später erneut versuchen.",
                           retryable=True) from e
        except _httpx.TransportError as e:  # Abbruch mitten im Stream
            raise LLMError(f"Verbindung zur Anthropic-API während der Antwort abgebrochen ({type(e).__name__}) – "
                           "später erneut versuchen.", retryable=True) from e
        except TypeError as e:
            # SDK: "Could not resolve authentication method ..." – kein Key / Login vorhanden
            if "authentication" in str(e).lower() or "api_key" in str(e).lower():
                raise LLMError("Kein Anthropic-API-Key gefunden – ANTHROPIC_API_KEY setzen (z. B. in .env) "
                               "oder llm.backend: cli verwenden.") from e
            raise
        self._log_usage(resp, time.monotonic() - t0)
        return resp

    def _log_usage(self, resp: Any, seconds: float) -> None:
        usage = _get(resp, "usage")
        served = _get(resp, "model")
        served = served if isinstance(served, str) and served else None
        usd = estimate_cost_usd(usage, served, self.cfg.model)
        self.spend.add(usd)
        log.info("claude api: model=%s stop=%s in=%s out=%s cache_read=%s cache_write=%s cost≈$%.4f "
                 "(24h≈$%.2f) (%.1fs)", served or self.cfg.model, _get(resp, "stop_reason"),
                 _get(usage, "input_tokens"), _get(usage, "output_tokens"), _get(usage, "cache_read_input_tokens"),
                 _get(usage, "cache_creation_input_tokens"), usd, self.spend.total(), seconds)
        if served and served != self.cfg.model:
            log.warning("claude api: answer served by fallback model %s (requested %s)", served, self.cfg.model)

    @staticmethod
    def _response_text(resp: Any, max_tokens: int) -> str:
        stop = _get(resp, "stop_reason")
        if stop == "refusal":
            details = _get(resp, "stop_details")
            category = _get(details, "category") if details is not None else None
            explanation = _get(details, "explanation") if details is not None else None
            msg = "Claude hat die Anfrage abgelehnt (refusal)"
            if category:
                msg += f", Kategorie: {category}"
            if explanation:
                msg += f" – {explanation}"
            raise LLMRefusal(msg, category=category, explanation=explanation)
        if stop == "max_tokens":
            raise LLMError(f"Claude-Antwort abgeschnitten (max_tokens={max_tokens} erreicht) – "
                           "llm.effort senken oder weniger Frames schicken.")
        if stop == "model_context_window_exceeded":
            raise LLMError("Prompt zu groß für das Kontextfenster des Modells.")
        if stop not in (None, "end_turn", "stop_sequence"):
            raise LLMError(f"Unerwarteter stop_reason von Claude: {stop}")
        blocks = list(_get(resp, "content") or [])
        # nach einem Fallback-Block zählt nur, was das Fallback-Modell geschrieben hat
        last_fb = max((i for i, b in enumerate(blocks) if _get(b, "type") == "fallback"), default=-1)
        texts = [str(_get(b, "text") or "") for b in blocks[last_fb + 1:] if _get(b, "type") == "text"]
        text = "".join(texts).strip()
        if not text:
            raise LLMError("Claude hat keinen Text (JSON) geliefert.")
        return text


# --------------------------------------------------------------------------- CLI-Backend

_JSON_ONLY = ("Return your answer as one JSON object that matches the schema exactly "
              "(via the StructuredOutput tool).")
# argv-Grenzen: Windows-Kommandozeile max. 32767 Zeichen (inkl. System-Prompt und Schema),
# Linux max. 128 KiB pro Argument
_MAX_ARG_CHARS = 12000 if os.name == "nt" else 100_000


class CliLLM:
    """Lokales `claude -p` (Claude-Code-Login/Abo statt API-Key).

    Härtung: eigener System-Prompt statt Claude-Code-Persona, nur das Read-Tool (nur mit Bildern, Lesen nur
    im Frame-Ordner = cwd, Claude-Login/Tokens/.env explizit gesperrt), keine MCP-Server, keine
    Session-Ablage (sonst sammeln sich Transkripte mit Bildern in ~/.claude), `--json-schema` für
    validierte Antworten (`structured_output`), `--effort` aus llm.effort."""

    def __init__(self, settings: Settings, runner: Callable[..., Any] | None = None, *,
                 rate_limiter: RateLimiter | None = None, spend_tracker: SpendTracker | None = None) -> None:
        """runner: injizierbar für Tests (Signatur wie subprocess.run)."""
        self.settings = settings
        self.cfg = settings.config.llm
        self._runner = runner or subprocess.run
        self.rate_limiter = rate_limiter or shared_rate_limiter(self.cfg.max_calls_per_hour)
        self.spend = spend_tracker or shared_spend_tracker(_spend_path(settings))

    def spend_last_24h_usd(self) -> float:
        """Von der CLI gemeldete Kosten der letzten 24 h (beim Abo nur ein Richtwert)."""
        return self.spend.total()

    def complete_json(self, system: str, content: list[dict[str, Any]], schema_model: type[T],
                      *, max_tokens: int = 16000) -> T:
        # max_tokens hat beim CLI keine Entsprechung
        _check_budget(self.cfg, self.spend)
        self.rate_limiter.acquire()
        schema = strict_schema(schema_model)
        prompt, cwd, n_images = self._build_prompt(content)
        result = self._run(prompt, system, cwd, schema, n_images)
        try:
            return _parse_json_model(result, schema_model)
        except _ParseError as e:
            log.warning("claude cli: answer failed validation (%s), retrying once", e)
            first_error = e
        retry_prompt = (
            f"{prompt}\n\nYour previous answer was invalid: {first_error}\n"
            f"Previous answer (truncated):\n{result[:_REPAIR_PREVIEW_CHARS]}\n\n"
            f"Fix it. {_JSON_ONLY}")
        self.rate_limiter.record()  # zweiter Aufruf zählt mit
        result2 = self._run(retry_prompt, system, cwd, schema, n_images)
        try:
            return _parse_json_model(result2, schema_model)
        except _ParseError as e:
            raise LLMError(f"Antwort der Claude-CLI passt nicht zum Schema {schema_model.__name__}, "
                           f"auch nach einem zweiten Versuch: {e}") from e

    # -- intern

    def _build_prompt(self, content: list[dict[str, Any]]) -> tuple[str, str | None, int]:
        """Prompt-Text, Arbeitsordner (gemeinsamer Ordner der Bilder) und Anzahl der Bilder."""
        parts: list[str] = []
        image_dirs: list[str] = []
        for b in content:
            kind = b.get("type")
            if kind == "text":
                if b.get("text"):
                    parts.append(str(b["text"]))
            elif kind == "image":
                path = b.get("path")
                if not path:
                    log.warning("claude cli: image block without path – skipped")
                    continue
                p = Path(path).resolve()
                if not p.is_file():
                    log.warning("claude cli: image %s missing – skipped", p)
                    continue
                t = b.get("t")
                label = f"Image (t={float(t):.1f}s)" if isinstance(t, int | float) else "Image"
                parts.append(f"{label}: {p} - read it with the Read tool")
                image_dirs.append(str(p.parent))
            else:
                raise LLMError(f"Unbekannter Content-Block-Typ für Claude: {kind!r}")
        if image_dirs:
            parts.append("Read ALL images listed above with the Read tool before answering "
                         "(issue the Read calls in parallel in a single step).")
        parts.append(_JSON_ONLY)
        cwd = None
        if image_dirs:
            try:
                cwd = os.path.commonpath(image_dirs)
            except ValueError:  # z. B. verschiedene Laufwerke unter Windows
                cwd = image_dirs[0]
        return "\n\n".join(parts), cwd, len(image_dirs)

    def _exe(self) -> str:
        return shutil.which(self.cfg.cli_path) or self.cfg.cli_path

    def _deny_rules(self) -> list[str]:
        """Read-Sperren zusätzlich zur cwd-Beschränkung (Titel/Transkripte sind fremder Input)."""
        rules = ["Read(~/.claude/**)", "Read(~/.config/**)"]
        for p, pattern in ((Path(self.settings.config.paths.tokens_dir), "{}/**"), (Path.cwd() / ".env", "{}")):
            try:
                posix = p.resolve().as_posix()
            except OSError:
                continue
            if posix.startswith("/"):  # "//abs/pfad" = absoluter Pfad in Claude-Code-Regeln (nicht Windows)
                rules.append(f"Read(/{pattern.format(posix)})")
        return rules

    def _command(self, prompt: str | None, system: str, schema: dict, n_images: int) -> list[str]:
        cmd = [self._exe(), "-p"]
        if prompt is not None:
            cmd.append(prompt)  # direkt nach -p: variadische Optionen (--tools ...) dürfen ihn nicht schlucken
        cmd += ["--output-format", "json", "--model", self.cfg.model, "--effort", self.cfg.effort,
                "--system-prompt", system, "--tools", "Read" if n_images else ""]
        if n_images:
            cmd += ["--allowedTools", "Read(./**)", "--disallowedTools", *self._deny_rules()]
        cmd += ["--json-schema", json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
                "--no-session-persistence", "--strict-mcp-config",
                "--max-turns", str(max(CLI_MAX_TURNS, n_images + 3))]
        return cmd

    def _run(self, prompt: str, system: str, cwd: str | None, schema: dict, n_images: int) -> str:
        via_stdin = len(prompt) > _MAX_ARG_CHARS
        cmd = self._command(None if via_stdin else prompt, system, schema, n_images)
        # ohne API-Key in der Umgebung nutzt die CLI den Claude-Code-Login (Abo) – das ist der Sinn dieses Backends
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        timeout = self.cfg.cli_timeout_s
        t0 = time.monotonic()
        try:
            proc = self._runner(cmd, input=prompt if via_stdin else "", capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=timeout, cwd=cwd, env=env,
                                check=False)
        except FileNotFoundError as e:
            raise LLMError(f"Claude-CLI nicht gefunden ({self.cfg.cli_path}) – Claude Code installieren und "
                           "einloggen oder llm.backend: api verwenden.") from e
        except subprocess.TimeoutExpired as e:
            raise LLMError(f"Claude-CLI hat nach {timeout} s nicht geantwortet (llm.cli_timeout_s).",
                           retryable=True) from e
        except OSError as e:
            raise LLMError(f"Claude-CLI konnte nicht gestartet werden: {e}") from e

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", "replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        data = _parse_cli_stdout(stdout)
        cost = _get(data, "total_cost_usd")
        if isinstance(cost, int | float) and not isinstance(cost, bool):
            self.spend.add(float(cost))  # auch fehlgeschlagene Läufe kosten
        if proc.returncode != 0:
            detail = _tail(stderr) or _tail(str(_get(data, "result") or "")) or _tail(stdout)
            raise LLMError(f"Claude-CLI beendet mit Code {proc.returncode}: {detail or '(keine Ausgabe)'}",
                           retryable=_looks_like_limit(detail))
        if data is None:
            raise LLMError(f"Claude-CLI lieferte kein JSON: {_tail(stdout) or '(leer)'}")
        if data.get("is_error"):
            subtype = data.get("subtype") or "error"
            detail = _tail(str(data.get("result") or data.get("error") or ""))
            hint = " (zu viele Schritte – max-turns)" if "max_turns" in str(subtype) else ""
            raise LLMError(f"Claude-CLI meldet einen Fehler ({subtype}){hint}: {detail or '(ohne Details)'}",
                           retryable=_looks_like_limit(detail))
        log.info("claude cli: model=%s turns=%s cost_usd=%s (%.1fs)", self.cfg.model, data.get("num_turns"),
                 cost, time.monotonic() - t0)
        structured = data.get("structured_output")  # mit --json-schema: schon validiertes Objekt
        if isinstance(structured, dict):
            return json.dumps(structured, ensure_ascii=False)
        result = data.get("result")
        if not isinstance(result, str) or not result.strip():
            raise LLMError("Claude-CLI lieferte ein leeres Ergebnis.")
        return result


def _looks_like_limit(text: str) -> bool:
    t = (text or "").lower()
    return any(s in t for s in ("rate limit", "usage limit", "limit reached", "overloaded", "529", "429"))


def _parse_cli_stdout(stdout: str) -> dict | None:
    """`--output-format json` liefert ein Objekt; evtl. Warnzeilen davor werden übersprungen."""
    s = (stdout or "").strip()
    if not s:
        return None
    try:
        data = json.loads(s)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    for line in reversed(s.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                return data
    return None


def build_llm(settings: Settings) -> LLMClient:
    return ApiLLM(settings) if settings.config.llm.backend == "api" else CliLLM(settings)
