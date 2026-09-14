"""Correctness checks for the pluggable LLM backends (ADR-0002).

CC-16  LLM_PROVIDER selects the backend by configuration, not code.
CC-17  The Ollama (local) backend reports $0 cost per call.
CC-30  One OpenAI-compatible adapter serves DeepSeek, Groq, vLLM, ...: the
       endpoint, model and key are configuration; cost uses configured prices.
CC-19  Cost is computed from token usage and recorded (Anthropic path).
Plus:  defensive parsing turns bad model output into a BackendError so the
       pipeline can degrade instead of delivering garbage.
"""

import json
from types import SimpleNamespace

import httpx
import pytest

from app.backends import (
    AnthropicBackend,
    OllamaBackend,
    OpenAICompatibleBackend,
    StubBackend,
    get_backend,
    parse_hypothesis,
)
from app.config import Settings
from app.ports import BackendError

GOOD_JSON = (
    '{"root_cause": "container OOMKilled", "severity": "critical", '
    '"confidence": "high", "evidence": ["OOMKilling event", "memory at limit"], '
    '"disproof": "check if memory usage was below the limit at the alert time", '
    '"blast_radius": "single-pod", '
    '"next_steps": ["raise memory limit", "check for a leak"]}'
)


# ---- factory (CC-16) --------------------------------------------------------

@pytest.mark.parametrize(
    "provider,cls",
    [
        ("stub", StubBackend),
        ("anthropic", AnthropicBackend),
        ("ollama", OllamaBackend),
        ("openai", OpenAICompatibleBackend),
    ],
)
def test_get_backend_selects_by_config(provider, cls):
    backend = get_backend(Settings(llm_provider=provider))
    assert isinstance(backend, cls)
    assert backend.name == provider


def test_get_backend_rejects_unknown_provider():
    with pytest.raises(ValueError):
        get_backend(Settings(llm_provider="gpt"))


# ---- defensive parsing ------------------------------------------------------

def test_parse_hypothesis_handles_code_fences_and_prose():
    text = "Here is my answer:\n```json\n" + GOOD_JSON + "\n```\nHope that helps."
    h = parse_hypothesis(text)
    assert h.root_cause == "container OOMKilled"
    assert h.next_steps == ["raise memory limit", "check for a leak"]


def test_parse_hypothesis_extracts_evidence_disproof_blast_radius():
    h = parse_hypothesis(GOOD_JSON)
    assert h.evidence == ["OOMKilling event", "memory at limit"]
    assert "below the limit" in h.disproof
    assert h.blast_radius == "single-pod"


@pytest.mark.parametrize("bad", ["not json at all", "{}", '{"severity": "critical"}'])
def test_parse_hypothesis_rejects_bad_output(bad):
    with pytest.raises(BackendError):
        parse_hypothesis(bad)


# ---- Anthropic backend via an injected fake client (CC-19) ------------------

def _fake_anthropic(text: str, in_tok: int, out_tok: int, stop_reason="end_turn"):
    resp = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=in_tok, output_tokens=out_tok),
        stop_reason=stop_reason,
    )
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        return resp

    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    return client, calls


async def test_anthropic_backend_parses_and_prices():
    client, calls = _fake_anthropic(GOOD_JSON, in_tok=4000, out_tok=400)
    cfg = Settings(price_in_per_mtok=1.0, price_out_per_mtok=5.0)
    backend = AnthropicBackend(cfg, client=client)

    result = await backend.analyze("prompt goes here")

    assert result.hypothesis.root_cause == "container OOMKilled"
    # 4000/1e6*1.0 + 400/1e6*5.0 = 0.004 + 0.002 = 0.006  (~the whitepaper number)
    assert result.cost_usd == pytest.approx(0.006)
    assert len(calls) == 1  # exactly one call
    assert calls[0]["model"] == "claude-haiku-4-5"
    assert calls[0]["extra_body"] == {"temperature": 0}  # reproducibility


async def test_anthropic_backend_raises_on_refusal():
    client, _ = _fake_anthropic(GOOD_JSON, 10, 10, stop_reason="refusal")
    backend = AnthropicBackend(Settings(), client=client)
    with pytest.raises(BackendError):
        await backend.analyze("prompt")


# ---- Ollama backend via httpx MockTransport (CC-17) -------------------------

async def test_ollama_backend_is_local_and_free():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        return httpx.Response(
            200,
            json={
                "message": {"content": GOOD_JSON},
                "prompt_eval_count": 1234,
                "eval_count": 56,
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://ollama")
    backend = OllamaBackend(Settings(), client=client)

    result = await backend.analyze("prompt")

    assert result.cost_usd == 0.0  # zero egress, zero cost
    assert result.input_tokens == 1234
    assert result.hypothesis.severity == "critical"
    await client.aclose()


# ---- OpenAI-compatible backend (CC-30) --------------------------------------

async def test_openai_compatible_backend_sends_bearer_and_prices_usage():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.read())
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": GOOD_JSON}}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 100},
            },
        )

    cfg = Settings(
        llm_provider="openai",
        openai_model="deepseek-chat",
        openai_api_key="test-key",
        openai_price_in_per_mtok=0.28,
        openai_price_out_per_mtok=0.42,
    )
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://provider/v1",
        headers={"Authorization": f"Bearer {cfg.openai_api_key}"},
    )
    result = await OpenAICompatibleBackend(cfg, client=client).analyze("prompt")

    assert seen["path"] == "/v1/chat/completions"  # the shared dialect
    assert seen["auth"] == "Bearer test-key"
    assert seen["body"]["model"] == "deepseek-chat"
    assert seen["body"]["temperature"] == 0  # reproducible triage
    assert result.backend == "openai"
    assert result.hypothesis.root_cause == "container OOMKilled"
    assert result.input_tokens == 1000 and result.output_tokens == 100
    assert result.cost_usd == pytest.approx(0.000322)  # 1000*0.28 + 100*0.42 per M
    await client.aclose()


async def test_openai_compatible_backend_degrades_on_empty_choices():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://p")
    with pytest.raises(BackendError):
        await OpenAICompatibleBackend(Settings(), client=client).analyze("prompt")
    await client.aclose()


async def test_openai_compatible_backend_degrades_on_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "rate limited"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://p")
    with pytest.raises(BackendError):
        await OpenAICompatibleBackend(Settings(), client=client).analyze("prompt")
    await client.aclose()


async def test_stub_backend_is_deterministic_and_free():
    r1 = await StubBackend().analyze("same prompt")
    r2 = await StubBackend().analyze("same prompt")
    assert r1.cost_usd == 0.0
    assert r1.hypothesis == r2.hypothesis
    assert r1.hypothesis.root_cause  # non-empty


async def test_provider_http_error_is_described_by_status_and_body_not_url():
    """The Gemini 404 that reached a phone said nothing useful; the body did."""
    body = {"error": {"code": 404, "message": "This model models/gemini-2.5-flash is no longer available to new users. Please update your code to use models/gemini-3.6-flash"}}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://p/v1")
    with pytest.raises(BackendError) as info:
        await OpenAICompatibleBackend(Settings(openai_api_key="k"), client=client).analyze("prompt")
    msg = str(info.value)
    assert "HTTP 404 from provider" in msg
    assert "gemini-3.6-flash" in msg  # the actionable part survived
    assert "http://p" not in msg and "for url" not in msg  # the URL did not
    assert "mozilla" not in msg
    await client.aclose()


def test_describe_strips_url_clauses_and_redacts():
    from app.errors import describe

    exc = RuntimeError(
        "Client error '400 Bad Request' for url "
        "'https://api.telegram.org/bot1890788154:AAHVJEmQlOanKaS9SWyRphHOCwWMp5YsETc/sendMessage'"
        "\nFor more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/400"
    )
    out = describe(exc)
    assert "AAHVJEmQlOanKaS9SWyRphHOCwWMp5YsETc" not in out
    assert "for url" not in out and "mozilla" not in out
    assert out.startswith("RuntimeError: Client error '400 Bad Request'")


async def test_openai_backend_retries_503_then_succeeds(monkeypatch):
    import asyncio as _asyncio

    from app import errors

    slept = []

    async def fake_sleep(d):
        slept.append(d)

    monkeypatch.setattr(_asyncio, "sleep", fake_sleep)
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(503, json=[{"error": {"code": 503, "message": "high demand"}}])
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": GOOD_JSON}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://p/v1")
    result = await OpenAICompatibleBackend(Settings(), client=client).analyze("prompt")
    assert result.hypothesis.root_cause == "container OOMKilled"
    assert attempts["n"] == 3
    assert slept == list(errors.RETRY_DELAYS)
    await client.aclose()


async def test_openai_backend_gives_up_after_bounded_retries(monkeypatch):
    import asyncio as _asyncio

    async def fake_sleep(d):
        pass

    monkeypatch.setattr(_asyncio, "sleep", fake_sleep)
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(503, json=[{"error": {"message": "high demand"}}])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://p/v1")
    with pytest.raises(BackendError) as info:
        await OpenAICompatibleBackend(Settings(), client=client).analyze("prompt")
    assert attempts["n"] == 3  # one call plus two retries, never more
    assert "HTTP 503" in str(info.value)
    await client.aclose()


async def test_openai_backend_does_not_retry_other_errors():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(401, json={"error": "bad key"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://p/v1")
    with pytest.raises(BackendError):
        await OpenAICompatibleBackend(Settings(), client=client).analyze("prompt")
    assert attempts["n"] == 1
    await client.aclose()
