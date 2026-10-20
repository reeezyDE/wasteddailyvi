"""ApiLLM mit einem Fake-Client (kein Netzwerk): Request-Form, Stop-Reasons, Reparatur-Turn, Fehler."""

from __future__ import annotations

import json
from types import SimpleNamespace

import anthropic
import httpx2
import pytest
from PIL import Image

from wdvi.brain import claude
from wdvi.brain.claude import (
    ApiLLM,
    CliLLM,
    LLMError,
    LLMRefusal,
    RateLimiter,
    SpendTracker,
    build_llm,
    estimate_cost_usd,
    strict_schema,
)
from wdvi.schemas import EditPlan, MomentPicks

PICKS = {"moments": [{"start": 10.0, "end": 40.0, "score": 80, "why": "Huge crash."}]}


def _resp(text: str | None = None, *, stop: str = "end_turn", content: list | None = None, details=None,
          model: str = "claude-opus-5-5", usage=None):
    if content is None:
        content = [SimpleNamespace(type="thinking", thinking="", signature="sig")]
        if text is not None:
            content.append(SimpleNamespace(type="text", text=text))
    if usage is None:
        usage = SimpleNamespace(input_tokens=1200, output_tokens=300, cache_read_input_tokens=1000,
                                cache_creation_input_tokens=0)
    return SimpleNamespace(stop_reason=stop, stop_details=details, content=content, model=model, usage=usage)


class _MidStream:
    """Fehler, der erst beim Lesen des Streams auftritt (Request selbst war HTTP 200)."""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc


class _FakeStream:
    def __init__(self, item) -> None:
        self.item = item

    def __enter__(self):
        if isinstance(self.item, BaseException):  # wie das SDK: Fehler beim Öffnen des Streams
            raise self.item
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def get_final_message(self):
        if isinstance(self.item, _MidStream):
            raise self.item.exc
        return self.item


class _FakeMessages:
    def __init__(self, owner: _FakeClient, ns: str) -> None:
        self.owner, self.ns = owner, ns

    def stream(self, **kwargs):
        self.owner.calls.append((self.ns, kwargs))
        return _FakeStream(self.owner.queue.pop(0))

    def create(self, **kwargs):  # pragma: no cover - der Code streamt immer
        raise AssertionError("non-streaming create() must not be used")


class _FakeClient:
    def __init__(self, *responses) -> None:
        self.queue = list(responses)
        self.calls: list[tuple[str, dict]] = []
        self.messages = _FakeMessages(self, "messages")
        self.beta = SimpleNamespace(messages=_FakeMessages(self, "beta"))


def _llm(settings, *responses, limiter: RateLimiter | None = None,
         spend: SpendTracker | None = None) -> tuple[ApiLLM, _FakeClient]:
    client = _FakeClient(*responses)
    return ApiLLM(settings, client=client, rate_limiter=limiter or RateLimiter(1000),
                  spend_tracker=spend or SpendTracker()), client


def _http_error(cls, status: int, msg: str = "boom"):
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls(msg, response=httpx2.Response(status, request=req), body=None)


def test_request_shape_with_fallbacks(settings):
    llm, client = _llm(settings, _resp(json.dumps(PICKS)))
    out = llm.complete_json("SYSTEM PROMPT", [{"type": "text", "text": "hello"}], MomentPicks, max_tokens=12000)
    assert isinstance(out, MomentPicks) and out.moments[0].score == 80
    assert len(client.calls) == 1
    ns, kw = client.calls[0]
    assert ns == "beta"
    assert kw["model"] == settings.config.llm.model == "claude-opus-5-5"
    assert kw["max_tokens"] == 12000
    assert kw["thinking"] == {"type": "adaptive"}
    assert kw["output_config"]["effort"] == settings.config.llm.effort
    assert kw["output_config"]["format"] == {"type": "json_schema", "schema": strict_schema(MomentPicks)}
    assert kw["betas"] == ["server-side-fallback-2026-07-01"]
    assert kw["fallbacks"] == "default"
    assert kw["system"] == [{"type": "text", "text": "SYSTEM PROMPT", "cache_control": {"type": "ephemeral"}}]
    assert kw["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]
    for forbidden in ("temperature", "tool_choice", "tools", "top_p"):
        assert forbidden not in kw
    assert "budget_tokens" not in json.dumps(kw["thinking"])


def test_request_without_fallbacks_uses_plain_messages(settings):
    settings.config.llm.use_fallbacks = False
    settings.config.llm.effort = "xhigh"
    llm, client = _llm(settings, _resp(json.dumps(PICKS)))
    llm.complete_json("S", [{"type": "text", "text": "x"}], MomentPicks)
    ns, kw = client.calls[0]
    assert ns == "messages"
    assert "betas" not in kw and "fallbacks" not in kw
    assert kw["output_config"]["effort"] == "xhigh"
    assert kw["max_tokens"] == 16000


def test_images_are_base64_and_missing_files_skipped(settings, tmp_path):
    jpg = tmp_path / "frame.jpg"
    Image.new("RGB", (64, 36), (200, 30, 90)).save(jpg, "JPEG")
    png = tmp_path / "frame.png"
    Image.new("RGB", (8, 8)).save(png, "PNG")
    llm, client = _llm(settings, _resp(json.dumps(PICKS)))
    content = [
        {"type": "text", "text": "Frame t=2.00s (faces: 0)"},
        {"type": "image", "path": str(jpg), "t": 2.0},
        {"type": "image", "path": str(tmp_path / "missing.jpg")},
        {"type": "image", "path": str(png)},
    ]
    llm.complete_json("S", content, MomentPicks)
    blocks = client.calls[0][1]["messages"][0]["content"]
    assert [b["type"] for b in blocks] == ["text", "image", "image"]
    src = blocks[1]["source"]
    assert src["type"] == "base64" and src["media_type"] == "image/jpeg"
    import base64
    assert base64.standard_b64decode(src["data"]) == jpg.read_bytes()
    assert blocks[2]["source"]["media_type"] == "image/png"


def test_refusal_raises_with_category(settings):
    details = SimpleNamespace(type="refusal", category="cyber", explanation="Declined by classifier")
    llm, _ = _llm(settings, _resp(None, stop="refusal", content=[], details=details))
    with pytest.raises(LLMRefusal) as ei:
        llm.complete_json("S", [{"type": "text", "text": "x"}], MomentPicks)
    assert ei.value.category == "cyber"
    assert "cyber" in str(ei.value) and "Declined by classifier" in str(ei.value)
    assert ei.value.retryable is False


def test_refusal_without_details(settings):
    llm, _ = _llm(settings, _resp(None, stop="refusal", content=[], details=None))
    with pytest.raises(LLMRefusal) as ei:
        llm.complete_json("S", [{"type": "text", "text": "x"}], MomentPicks)
    assert ei.value.category is None


def test_max_tokens_is_an_error_not_a_refusal(settings):
    llm, _ = _llm(settings, _resp('{"moments": [', stop="max_tokens"))
    with pytest.raises(LLMError) as ei:
        llm.complete_json("S", [{"type": "text", "text": "x"}], MomentPicks)
    assert not isinstance(ei.value, LLMRefusal)
    assert "max_tokens" in str(ei.value)


def test_repair_turn_appends_unchanged_content(settings):
    bad = _resp('{"moments": [{"start": 1.0}]}')
    good = _resp(json.dumps(PICKS))
    llm, client = _llm(settings, bad, good)
    out = llm.complete_json("S", [{"type": "text", "text": "x"}], MomentPicks)
    assert out.moments[0].why == "Huge crash."
    assert len(client.calls) == 2
    msgs = client.calls[1][1]["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert msgs[1]["content"] is bad.content  # Thinking-Blöcke unverändert zurück
    assert "moments.0.end" in msgs[2]["content"][0]["text"]
    # der erste Request wurde nicht nachträglich verändert
    assert len(client.calls[0][1]["messages"]) == 1


def test_repair_only_once(settings):
    llm, client = _llm(settings, _resp("not json"), _resp('{"moments": "nope"}'))
    with pytest.raises(LLMError) as ei:
        llm.complete_json("S", [{"type": "text", "text": "x"}], MomentPicks)
    assert len(client.calls) == 2
    assert "Reparatur" in str(ei.value)


def test_fallback_block_and_text_after_it(settings):
    content = [
        SimpleNamespace(type="fallback", **{"from_": SimpleNamespace(model="claude-opus-5-5")}),
        SimpleNamespace(type="thinking", thinking=""),
        SimpleNamespace(type="text", text=json.dumps(PICKS)),
    ]
    llm, _ = _llm(settings, _resp(None, content=content, model="claude-opus-5"))
    assert llm.complete_json("S", [{"type": "text", "text": "x"}], MomentPicks).moments[0].start == 10.0


def test_no_text_block_is_an_error(settings):
    llm, _ = _llm(settings, _resp(None))
    with pytest.raises(LLMError):
        llm.complete_json("S", [{"type": "text", "text": "x"}], MomentPicks)


@pytest.mark.parametrize("exc, retryable, needle", [
    (lambda: _http_error(anthropic.RateLimitError, 429), True, "429"),
    (lambda: _http_error(anthropic.BadRequestError, 400, "image too large"), False, "image too large"),
    (lambda: _http_error(anthropic.AuthenticationError, 401), False, "ANTHROPIC_API_KEY"),
    (lambda: _http_error(anthropic.NotFoundError, 404), False, "claude-opus-5-5"),
    (lambda: _http_error(anthropic.InternalServerError, 500), True, "500"),
    (lambda: _http_error(anthropic.APIStatusError, 529, "overloaded"), True, "529"),
    (lambda: anthropic.APIConnectionError(request=httpx2.Request("POST", "https://x")), True, "Verbindung"),
    (lambda: anthropic.APITimeoutError(request=httpx2.Request("POST", "https://x")), True, "Verbindung"),
])
def test_sdk_errors_become_llm_errors(settings, exc, retryable, needle):
    llm, _ = _llm(settings, exc())
    with pytest.raises(LLMError) as ei:
        llm.complete_json("S", [{"type": "text", "text": "x"}], MomentPicks)
    assert ei.value.retryable is retryable
    assert needle in str(ei.value)


def test_empty_content_rejected(settings):
    llm, client = _llm(settings)
    with pytest.raises(LLMError):
        llm.complete_json("S", [{"type": "image", "path": "/nope.jpg"}], MomentPicks)
    assert client.calls == []


def test_rate_limiter_blocks_before_calling(settings):
    llm, client = _llm(settings, _resp(json.dumps(PICKS)), _resp(json.dumps(PICKS)), limiter=RateLimiter(1))
    llm.complete_json("S", [{"type": "text", "text": "x"}], MomentPicks)
    with pytest.raises(LLMError) as ei:
        llm.complete_json("S", [{"type": "text", "text": "x"}], MomentPicks)
    assert "Stundenlimit" in str(ei.value) and ei.value.retryable
    assert len(client.calls) == 1


def test_rate_limiter_sliding_window():
    now = [1000.0]
    rl = RateLimiter(2, clock=lambda: now[0])
    rl.acquire()
    now[0] += 10
    rl.acquire()
    assert rl.remaining() == 0
    with pytest.raises(LLMError, match="Stundenlimit"):
        rl.acquire()
    now[0] += 3591  # erster Aufruf fällt aus dem Fenster
    assert rl.remaining() == 1
    rl.acquire()
    unlimited = RateLimiter(0)
    for _ in range(100):
        unlimited.acquire()


def test_backends_share_one_rate_limiter(settings, monkeypatch):
    monkeypatch.setattr(claude, "_shared_limiter", None)
    settings.config.llm.max_calls_per_hour = 7
    api = ApiLLM(settings, client=_FakeClient())
    cli = CliLLM(settings, runner=lambda *a, **k: None)
    assert api.rate_limiter is cli.rate_limiter
    assert api.rate_limiter.max_calls == 7


def test_default_client_construction(settings, monkeypatch):
    created = {}

    class FakeAnthropic:
        def __init__(self, **kwargs):
            created.update(kwargs)

    monkeypatch.setattr(anthropic, "Anthropic", FakeAnthropic)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    settings.secrets.anthropic_api_key = "sk-from-dotenv"
    llm = ApiLLM(settings, rate_limiter=RateLimiter(10))
    assert isinstance(llm.client, FakeAnthropic)
    assert created == {"max_retries": 4, "api_key": "sk-from-dotenv"}

    created.clear()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env")
    assert isinstance(ApiLLM(settings, rate_limiter=RateLimiter(10)).client, FakeAnthropic)
    assert created == {"max_retries": 4}  # SDK liest die Umgebung selbst


def test_build_llm_selects_backend(settings):
    assert isinstance(build_llm(settings), ApiLLM)
    settings.config.llm.backend = "cli"
    assert isinstance(build_llm(settings), CliLLM)


def test_edit_plan_roundtrip_through_api(settings):
    plan = {
        "keep": True, "virality_score": 88, "reasoning": "Insane jump.", "reject_reason": None,
        "is_leak_or_unreleased": False, "is_unsafe": False, "content_language": "en",
        "trim_start": 0.5, "trim_end": 7.5, "remove_ranges": [[2.0, 2.5]], "layout": None,
        "hook_text": "He did NOT see this coming", "highlight_words": ["insane"],
        "zooms": [{"start": 5.0, "end": 6.0, "scale": 1.2, "focus": "center"}],
        "emphasis": [{"t": 5.0, "emoji": "💀", "sfx": "boom"}],
        "titles": {"youtube": "a", "tiktok": "b", "instagram": "c"},
        "descriptions": {"youtube": "a", "tiktok": "b", "instagram": "c"}, "hashtags": ["gta6"],
    }
    llm, _ = _llm(settings, _resp(json.dumps(plan, ensure_ascii=False)))
    out = llm.complete_json("S", [{"type": "text", "text": "x"}], EditPlan)
    assert isinstance(out, EditPlan)
    assert out.remove_ranges == [(2.0, 2.5)]
    assert out.emphasis[0].emoji == "💀"


def test_missing_credentials_message(settings):
    err = TypeError("Could not resolve authentication method. Expected one of api_key, auth_token, or credentials")
    llm, _ = _llm(settings, err)
    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY"):
        llm.complete_json("S", [{"type": "text", "text": "x"}], MomentPicks)
    llm, _ = _llm(settings, TypeError("unrelated bug"))
    with pytest.raises(TypeError):
        llm.complete_json("S", [{"type": "text", "text": "x"}], MomentPicks)


# --------------------------------------------------------------------------- Streaming-Fehler, Kosten, Budget


def test_mid_stream_overloaded_event_is_retryable(settings):
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    body = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
    err = anthropic.APIStatusError(str(body), response=httpx2.Response(200, request=req), body=body)
    llm, _ = _llm(settings, _MidStream(err))
    with pytest.raises(LLMError) as ei:
        llm.complete_json("S", [{"type": "text", "text": "x"}], MomentPicks)
    assert ei.value.retryable and "overload" in str(ei.value).lower() and "Serverfehler" in str(ei.value)


def test_mid_stream_connection_drop_is_retryable(settings):
    llm, _ = _llm(settings, _MidStream(httpx2.RemoteProtocolError("peer closed connection")))
    with pytest.raises(LLMError) as ei:
        llm.complete_json("S", [{"type": "text", "text": "x"}], MomentPicks)
    assert ei.value.retryable and "abgebrochen" in str(ei.value)


def test_repair_turn_counts_in_rate_limiter(settings):
    limiter = RateLimiter(10)
    llm, _ = _llm(settings, _resp('{"moments": [{"start": 1.0}]}'), _resp(json.dumps(PICKS)), limiter=limiter)
    llm.complete_json("S", [{"type": "text", "text": "x"}], MomentPicks)
    assert limiter.remaining() == 8


def test_repair_after_mid_output_fallback_drops_declined_thinking(settings):
    thinking = SimpleNamespace(type="thinking", thinking="", signature="sig")
    partial = SimpleNamespace(type="text", text='{"moments": [')
    fb = SimpleNamespace(type="fallback")
    bad_text = SimpleNamespace(type="text", text='{"moments": [{"start": 1.0}]}')
    bad = _resp(None, content=[thinking, partial, fb, bad_text], model="claude-opus-5")
    llm, client = _llm(settings, bad, _resp(json.dumps(PICKS)))
    llm.complete_json("S", [{"type": "text", "text": "x"}], MomentPicks)
    echoed = client.calls[1][1]["messages"][1]["content"]
    assert echoed == [partial, fb, bad_text]


def test_cost_estimate_and_spend_tracking(settings):
    usage = SimpleNamespace(input_tokens=10_000, output_tokens=5_000, cache_read_input_tokens=2_000,
                            cache_creation_input_tokens=1_000)
    # Opus 5.5: 10k*4 + 5k*20 + 2k*0.2 + 1k*5 = 40000+100000+400+5000 = 145400 µ$ -> 0.1454 $
    assert estimate_cost_usd(usage, "claude-opus-5-5", "claude-opus-5-5") == pytest.approx(0.1454)
    # Fallback-Versuche aus usage.iterations, je mit eigenem Preis
    it_usage = SimpleNamespace(input_tokens=10_000, output_tokens=0, iterations=[
        SimpleNamespace(type="message", input_tokens=10_000, output_tokens=0),
        SimpleNamespace(type="fallback_message", input_tokens=10_000, output_tokens=1_000),
    ])
    assert estimate_cost_usd(it_usage, "claude-opus-5", "claude-opus-5-5") == pytest.approx(0.04 + 0.05 + 0.025)
    assert estimate_cost_usd(None, None, "claude-opus-5-5") == 0.0

    spend = SpendTracker()
    llm, _ = _llm(settings, _resp(json.dumps(PICKS), usage=usage), spend=spend)
    llm.complete_json("S", [{"type": "text", "text": "x"}], MomentPicks)
    assert spend.total() == pytest.approx(0.1454)
    assert llm.spend_last_24h_usd() == pytest.approx(0.1454)


def test_failed_call_still_counts_cost(settings):
    spend = SpendTracker()
    llm, _ = _llm(settings, _resp('{"moments": [', stop="max_tokens"), spend=spend)
    with pytest.raises(LLMError):
        llm.complete_json("S", [{"type": "text", "text": "x"}], MomentPicks)
    assert spend.total() > 0


def test_daily_budget_blocks_before_calling(settings):
    spend = SpendTracker()
    spend.add(5.0)
    llm, client = _llm(settings, _resp(json.dumps(PICKS)), spend=spend)
    llm.cfg = settings.config.llm.model_copy(update={"max_usd_per_day": 4.0})  # echtes Config-Feld
    with pytest.raises(LLMError) as ei:
        llm.complete_json("S", [{"type": "text", "text": "x"}], MomentPicks)
    assert ei.value.retryable and "usage limit" in str(ei.value) and client.calls == []
    llm.cfg = settings.config.llm.model_copy(update={"max_usd_per_day": 10.0})
    assert llm.complete_json("S", [{"type": "text", "text": "x"}], MomentPicks).moments
    # Standard llm.max_usd_per_day = 0: keine Grenze
    llm2, _ = _llm(settings, _resp(json.dumps(PICKS)), spend=spend)
    assert llm2.complete_json("S", [{"type": "text", "text": "x"}], MomentPicks).moments


def test_spend_tracker_window_and_persistence(tmp_path):
    now = [1_000_000.0]
    path = tmp_path / "spend.json"
    tr = SpendTracker(path, clock=lambda: now[0])
    tr.add(1.5)
    tr.add(float("nan"))
    tr.add(-3)
    now[0] += 3600
    tr.add(0.5)
    assert tr.total() == pytest.approx(2.0)
    # Neustart: Ledger wird geladen
    assert SpendTracker(path, clock=lambda: now[0]).total() == pytest.approx(2.0)
    now[0] += 86400 - 3600  # erster Eintrag fällt aus dem 24-h-Fenster
    assert tr.total() == pytest.approx(0.5)
    assert SpendTracker(path, clock=lambda: now[0]).total() == pytest.approx(0.5)
    path.write_text("not json")
    assert SpendTracker(path).total() == 0.0


def test_default_spend_tracker_lives_in_data_dir(settings):
    llm = ApiLLM(settings, client=_FakeClient(_resp(json.dumps(PICKS))), rate_limiter=RateLimiter(10))
    llm.complete_json("S", [{"type": "text", "text": "x"}], MomentPicks)
    ledger = settings.config.paths.data_dir / "llm_spend.json"
    assert ledger.is_file() and json.loads(ledger.read_text())[0][1] > 0
    assert CliLLM(settings, runner=lambda *a, **k: None).spend is llm.spend
