"""Claude-Anbindung mit zwei Backends:

- ApiLLM: Anthropic-SDK (`claude-opus-5-5`, adaptive thinking, effort aus config, structured outputs
  via output_config.format json_schema, server-side refusal fallbacks "default", prompt caching
  des System-Prompts). Bilder als base64.
- CliLLM: lokales `claude -p` (Claude-Code-Login / Abo). Bilder werden als Dateipfade übergeben und
  über das Read-Tool gelesen; Antwort muss reines JSON sein, wird validiert, 1 Reparatur-Retry.

Interne Content-Blöcke (backend-neutral):
  {"type": "text", "text": "..."}
  {"type": "image", "path": "/abs/pfad.jpg"}
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel

from wdvi.config import Settings


class LLMError(RuntimeError):
    pass


class LLMRefusal(LLMError):
    pass


class LLMClient(Protocol):
    def complete_json(self, system: str, content: list[dict[str, Any]], schema_model: type[BaseModel],
                      *, max_tokens: int = 16000) -> BaseModel:
        """Ein Aufruf, Ergebnis als validierte Instanz von schema_model. Wirft LLMError/LLMRefusal."""
        ...


def strict_schema(model: type[BaseModel]) -> dict:
    """JSON-Schema für structured outputs: $defs inline, additionalProperties: false überall,
    alle Properties required (optionale als anyOf [T, null]), nicht unterstützte Keywords
    (minimum, maximum, minLength, maxLength, pattern, default, title, ...) entfernt; tuple-Felder
    (prefixItems) zu array mit items vereinfacht."""
    raise NotImplementedError


class ApiLLM:
    def __init__(self, settings: Settings, client=None) -> None:
        raise NotImplementedError

    def complete_json(self, system, content, schema_model, *, max_tokens=16000):
        raise NotImplementedError


class CliLLM:
    def __init__(self, settings: Settings, runner=None) -> None:
        """runner: injizierbar für Tests (Signatur wie subprocess.run)."""
        raise NotImplementedError

    def complete_json(self, system, content, schema_model, *, max_tokens=16000):
        raise NotImplementedError


def build_llm(settings: Settings) -> LLMClient:
    return ApiLLM(settings) if settings.config.llm.backend == "api" else CliLLM(settings)
