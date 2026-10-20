"""CliLLM mit Fake-Runner (kein echtes `claude`): Kommando, Prompt, Parsing, Fehler, Retry."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from wdvi.brain.claude import CliLLM, LLMError, RateLimiter, SpendTracker, strict_schema
from wdvi.schemas import MomentPicks

PICKS = {"moments": [{"start": 10.0, "end": 40.0, "score": 80, "why": "Huge crash."}]}


def _out(result: str | None = None, *, is_error: bool = False, subtype: str = "success", rc: int = 0,
         stderr: str = "", raw: str | None = None, structured: dict | None = None):
    payload = {"type": "result", "subtype": subtype, "is_error": is_error, "result": result,
               "num_turns": 3, "total_cost_usd": 0.12, "session_id": "abc"}
    if structured is not None:
        payload["structured_output"] = structured
    return SimpleNamespace(returncode=rc, stdout=raw if raw is not None else json.dumps(payload), stderr=stderr)


class _Runner:
    def __init__(self, *outputs) -> None:
        self.outputs = list(outputs)
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        item = self.outputs.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _cli(settings, *outputs, limiter: RateLimiter | None = None) -> tuple[CliLLM, _Runner]:
    runner = _Runner(*outputs)
    return CliLLM(settings, runner=runner, rate_limiter=limiter or RateLimiter(1000),
                  spend_tracker=SpendTracker()), runner


def _opt(cmd: list[str], flag: str) -> list[str]:
    """Werte einer (variadischen) Option bis zur nächsten Option."""
    i = cmd.index(flag) + 1
    out = []
    while i < len(cmd) and not cmd[i].startswith("--"):
        out.append(cmd[i])
        i += 1
    return out


def _frame(tmp_path: Path, name: str = "f1.jpg") -> Path:
    p = tmp_path / "frames" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (32, 18)).save(p, "JPEG")
    return p


def test_command_prompt_and_fenced_json(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak-to-cli")
    img = _frame(tmp_path)
    fenced = "Here is the plan:\n```json\n" + json.dumps(PICKS, indent=2) + "\n```\nGood luck!"
    llm, runner = _cli(settings, _out(fenced))
    content = [
        {"type": "text", "text": "VIDEO CONTEXT"},
        {"type": "text", "text": "Frame t=2.00s (faces: 1)"},
        {"type": "image", "path": str(img), "t": 2.0},
        {"type": "image", "path": str(tmp_path / "missing.jpg"), "t": 3.0},
    ]
    out = llm.complete_json("SYSTEM PROMPT", content, MomentPicks)
    assert out.moments[0].end == 40.0

    cmd, kw = runner.calls[0]
    assert cmd[1] == "-p"
    prompt = cmd[2]
    assert cmd[3] == "--output-format" and cmd[4] == "json"  # Prompt direkt nach -p (vor variadischen Optionen)
    assert _opt(cmd, "--model") == ["claude-opus-5-5"]
    assert _opt(cmd, "--effort") == [settings.config.llm.effort]
    assert _opt(cmd, "--system-prompt") == ["SYSTEM PROMPT"]  # ersetzt die Claude-Code-Persona
    assert "--append-system-prompt" not in cmd
    assert _opt(cmd, "--tools") == ["Read"]
    assert _opt(cmd, "--allowedTools") == ["Read(./**)"]
    deny = _opt(cmd, "--disallowedTools")
    assert "Read(~/.claude/**)" in deny
    assert f"Read(/{settings.config.paths.tokens_dir.resolve().as_posix()}/**)" in deny
    assert json.loads(_opt(cmd, "--json-schema")[0]) == strict_schema(MomentPicks)
    assert "--no-session-persistence" in cmd and "--strict-mcp-config" in cmd
    assert _opt(cmd, "--max-turns") == ["6"]
    assert prompt.startswith("VIDEO CONTEXT")
    assert f"Image (t=2.0s): {img.resolve()} - read it with the Read tool" in prompt
    assert "missing.jpg" not in prompt
    assert '"additionalProperties"' not in prompt  # Schema kommt über --json-schema
    assert "StructuredOutput" in prompt
    assert not any(w in prompt for w in ("Antworte", "lies es", "Bild"))  # nur englische Anweisungen
    assert kw["timeout"] == settings.config.llm.cli_timeout_s
    assert kw["capture_output"] is True and kw["text"] is True
    assert kw["cwd"] == str(img.resolve().parent)
    assert "ANTHROPIC_API_KEY" not in kw["env"]


def test_structured_output_is_preferred(settings):
    llm, runner = _cli(settings, _out("Done - see structured output.", structured=PICKS))
    out = llm.complete_json("S", [{"type": "text", "text": "x"}], MomentPicks)
    assert out.moments[0].why == "Huge crash."
    assert llm.spend_last_24h_usd() == pytest.approx(0.12)


def test_no_images_means_no_tools_and_many_images_more_turns(settings, tmp_path):
    llm, runner = _cli(settings, _out(json.dumps(PICKS)), _out(json.dumps(PICKS)))
    llm.complete_json("S", [{"type": "text", "text": "x"}], MomentPicks)
    cmd, kw = runner.calls[0]
    assert _opt(cmd, "--tools") == [""] and "--allowedTools" not in cmd and "--disallowedTools" not in cmd
    assert kw["cwd"] is None

    content = [{"type": "text", "text": "frames"}]
    for i in range(8):
        content.append({"type": "image", "path": str(_frame(tmp_path, f"f{i}.jpg")), "t": float(i)})
    llm.complete_json("S", content, MomentPicks)
    cmd, kw = runner.calls[1]
    assert _opt(cmd, "--max-turns") == ["11"]
    assert kw["cwd"] == str((tmp_path / "frames").resolve())


def test_is_error_raises(settings):
    llm, _ = _cli(settings, _out("Credit balance is too low", is_error=True, subtype="error_during_execution"))
    with pytest.raises(LLMError) as ei:
        llm.complete_json("S", [{"type": "text", "text": "x"}], MomentPicks)
    assert "Credit balance is too low" in str(ei.value)
    assert llm.spend.total() == pytest.approx(0.12)  # auch fehlgeschlagene Läufe kosten


def test_max_turns_error_has_hint(settings):
    llm, _ = _cli(settings, _out(None, is_error=True, subtype="error_max_turns"))
    with pytest.raises(LLMError, match="max-turns"):
        llm.complete_json("S", [{"type": "text", "text": "x"}], MomentPicks)


def test_nonzero_exit_reports_stderr_tail(settings):
    stderr = "x" * 2000 + "\nError: Invalid API key · Please run /login"
    llm, _ = _cli(settings, _out(raw="", rc=1, stderr=stderr))
    with pytest.raises(LLMError) as ei:
        llm.complete_json("S", [{"type": "text", "text": "x"}], MomentPicks)
    msg = str(ei.value)
    assert "Code 1" in msg and "Please run /login" in msg
    assert len(msg) < 800


def test_usage_limit_is_retryable(settings):
    llm, _ = _cli(settings, _out(raw="", rc=1, stderr="Claude AI usage limit reached|1760000000"))
    with pytest.raises(LLMError) as ei:
        llm.complete_json("S", [{"type": "text", "text": "x"}], MomentPicks)
    assert ei.value.retryable


def test_invalid_then_valid_retries_once(settings):
    limiter = RateLimiter(10)
    llm, runner = _cli(settings, _out('{"moments": [{"start": 5}]}'), _out(json.dumps(PICKS)), limiter=limiter)
    out = llm.complete_json("S", [{"type": "text", "text": "x"}], MomentPicks)
    assert out.moments[0].score == 80
    assert len(runner.calls) == 2
    assert limiter.remaining() == 8  # der zweite Aufruf zählt mit
    retry_prompt = runner.calls[1][0][2]
    assert "previous answer was invalid" in retry_prompt
    assert "moments.0.end" in retry_prompt
    assert '{"moments": [{"start": 5}]}' in retry_prompt


def test_invalid_twice_raises(settings):
    llm, runner = _cli(settings, _out("I cannot do that."), _out("still no json"))
    with pytest.raises(LLMError, match="Schema"):
        llm.complete_json("S", [{"type": "text", "text": "x"}], MomentPicks)
    assert len(runner.calls) == 2


def test_stdout_with_warning_lines(settings):
    raw = "Warning: something noisy\n" + json.dumps({"type": "result", "is_error": False,
                                                    "result": json.dumps(PICKS)})
    llm, _ = _cli(settings, _out(raw=raw))
    assert llm.complete_json("S", [{"type": "text", "text": "x"}], MomentPicks).moments


def test_garbage_stdout(settings):
    llm, _ = _cli(settings, _out(raw="segfault?"))
    with pytest.raises(LLMError, match="kein JSON"):
        llm.complete_json("S", [{"type": "text", "text": "x"}], MomentPicks)


def test_timeout_and_missing_binary(settings):
    llm, _ = _cli(settings, subprocess.TimeoutExpired(cmd="claude", timeout=600))
    with pytest.raises(LLMError) as ei:
        llm.complete_json("S", [{"type": "text", "text": "x"}], MomentPicks)
    assert ei.value.retryable and "600" in str(ei.value)

    llm, _ = _cli(settings, FileNotFoundError("claude"))
    with pytest.raises(LLMError, match="nicht gefunden"):
        llm.complete_json("S", [{"type": "text", "text": "x"}], MomentPicks)


def test_very_long_prompt_goes_via_stdin(settings, monkeypatch):
    from wdvi.brain import claude

    monkeypatch.setattr(claude, "_MAX_ARG_CHARS", 1000)
    llm, runner = _cli(settings, _out(json.dumps(PICKS)))
    llm.complete_json("S", [{"type": "text", "text": "x" * 5000}], MomentPicks)
    cmd, kw = runner.calls[0]
    assert cmd[1] == "-p" and cmd[2] == "--output-format"
    assert kw["input"].startswith("x" * 5000)
    assert not any(c.startswith("x" * 100) for c in cmd)


def test_rate_limit_shared_semantics(settings):
    runner = _Runner(_out(json.dumps(PICKS)))
    llm = CliLLM(settings, runner=runner, rate_limiter=RateLimiter(1))
    llm.complete_json("S", [{"type": "text", "text": "x"}], MomentPicks)
    with pytest.raises(LLMError, match="Stundenlimit"):
        llm.complete_json("S", [{"type": "text", "text": "x"}], MomentPicks)
    assert len(runner.calls) == 1
