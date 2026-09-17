"""LLM backends behind one interface (ADR-0002).

Each backend turns a prompt into an LLMResult. Selecting one is configuration
(`MINUS20_LLM_PROVIDER`), never code:

- stub      — deterministic, offline, $0. Used in tests and for replay/demo.
- anthropic — cloud, best quality, via the official `anthropic` async SDK.
- ollama    — a local model in-cluster, zero egress, $0 per alert, via raw HTTP
              (Ollama has no SDK).
- openai    — any OpenAI-compatible chat endpoint: DeepSeek, Groq, Together,
              OpenRouter, vLLM, LM Studio. One adapter, many providers; the
              base URL and model name are configuration.

A malformed or empty model response is turned into a BackendError so the
Analyzer degrades gracefully rather than delivering garbage.
"""

from __future__ import annotations

import json
import logging
import re

from .config import Settings, settings
from .errors import describe, post_with_retry
from .models import Hypothesis, LLMResult
from .ports import BackendError

logger = logging.getLogger("analyzer-worker.backends")

_FENCE = re.compile(r"^```(?:json)?|```$", re.MULTILINE)


def parse_hypothesis(text: str) -> Hypothesis:
    """Extract a Hypothesis from a model's text response, defensively.

    Tolerates markdown code fences and leading/trailing prose by isolating the
    first balanced-looking JSON object. Raises BackendError on anything it
    cannot turn into a valid Hypothesis.
    """
    cleaned = _FENCE.sub("", text).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise BackendError(f"no JSON object in model response: {text[:200]!r}")
    try:
        data = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise BackendError(f"invalid JSON in model response: {exc}") from exc
    if not isinstance(data, dict) or not str(data.get("root_cause", "")).strip():
        raise BackendError("model response missing root_cause")
    steps = data.get("next_steps") or []
    if isinstance(steps, str):
        steps = [steps]
    evidence = data.get("evidence") or []
    if isinstance(evidence, str):
        evidence = [evidence]
    return Hypothesis(
        root_cause=str(data["root_cause"]).strip(),
        severity=str(data.get("severity", "unknown")),
        confidence=str(data.get("confidence", "medium")),
        evidence=[str(e) for e in evidence],
        disproof=str(data.get("disproof", "")),
        blast_radius=str(data.get("blast_radius", "unknown")),
        next_steps=[str(s) for s in steps],
    )


class StubBackend:
    """Deterministic backend for tests, replay and offline demo."""

    name = "stub"

    def __init__(self, hypothesis: Hypothesis | None = None) -> None:
        self._hypothesis = hypothesis or Hypothesis(
            root_cause="stub backend: deterministic placeholder hypothesis",
            severity="warning",
            confidence="low",
            evidence=["stub backend does not read the collected context"],
            disproof="enable a real LLM backend and compare the hypothesis",
            blast_radius="single-pod",
            next_steps=["Enable a real LLM backend to get a real hypothesis."],
        )

    async def analyze(self, prompt: str) -> LLMResult:
        # Rough, deterministic token estimate so cost math has something to chew on.
        in_tok = max(1, len(prompt) // 4)
        return LLMResult(
            hypothesis=self._hypothesis,
            input_tokens=in_tok,
            output_tokens=32,
            cost_usd=0.0,
            backend=self.name,
        )


class AnthropicBackend:
    """Cloud backend using the official `anthropic` async SDK."""

    name = "anthropic"

    def __init__(self, cfg: Settings = settings, client=None) -> None:
        self._cfg = cfg
        self._client = client  # inject in tests; created lazily in prod

    def _get_client(self):
        if self._client is None:  # pragma: no cover - real API path
            import anthropic

            self._client = anthropic.AsyncAnthropic()
        return self._client

    async def analyze(self, prompt: str) -> LLMResult:
        from .prompt import SYSTEM_PROMPT

        try:
            resp = await self._get_client().messages.create(
                model=self._cfg.anthropic_model,
                max_tokens=self._cfg.anthropic_max_tokens,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
                # temperature is no longer a typed kwarg in the SDK's current major;
                # pass it through the body. 0 = reproducible triage on Haiku-class models.
                extra_body={"temperature": 0},
            )
        except Exception as exc:  # network/API errors → graceful degradation
            raise BackendError(f"anthropic call failed: {describe(exc)}") from exc

        if getattr(resp, "stop_reason", None) == "refusal":
            raise BackendError("anthropic refused the request")

        text = "".join(
            block.text for block in resp.content if getattr(block, "type", None) == "text"
        )
        hypothesis = parse_hypothesis(text)
        in_tok = getattr(resp.usage, "input_tokens", 0)
        out_tok = getattr(resp.usage, "output_tokens", 0)
        return LLMResult(
            hypothesis=hypothesis,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=self._cost(in_tok, out_tok),
            backend=self.name,
        )

    def _cost(self, in_tok: int, out_tok: int) -> float:
        return round(
            in_tok / 1_000_000 * self._cfg.price_in_per_mtok
            + out_tok / 1_000_000 * self._cfg.price_out_per_mtok,
            6,
        )


class OllamaBackend:
    """Local, zero-egress backend via the Ollama HTTP API (no SDK exists)."""

    name = "ollama"

    def __init__(self, cfg: Settings = settings, client=None) -> None:
        self._cfg = cfg
        self._client = client  # inject an httpx.AsyncClient in tests

    async def analyze(self, prompt: str) -> LLMResult:
        import httpx

        from .prompt import SYSTEM_PROMPT

        client = self._client or httpx.AsyncClient(
            base_url=self._cfg.ollama_url, timeout=self._cfg.llm_timeout_seconds
        )
        payload = {
            "model": self._cfg.ollama_model,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        }
        try:
            resp = await client.post("/api/chat", json=payload)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            raise BackendError(f"ollama call failed: {describe(exc)}") from exc
        finally:
            if self._client is None:
                await client.aclose()

        text = data.get("message", {}).get("content", "")
        hypothesis = parse_hypothesis(text)
        return LLMResult(
            hypothesis=hypothesis,
            input_tokens=data.get("prompt_eval_count", 0),
            output_tokens=data.get("eval_count", 0),
            cost_usd=0.0,  # runs locally
            backend=self.name,
        )


class OpenAICompatibleBackend:
    """Any provider speaking the OpenAI chat-completions dialect.

    DeepSeek, Groq, Together, OpenRouter, vLLM and LM Studio all expose
    `POST {base_url}/chat/completions` with the same request and response
    shape, so one adapter covers them all. The API key is read from
    MINUS20_OPENAI_API_KEY and only ever sent as a bearer header; local
    servers (vLLM, LM Studio) accept an empty key.
    """

    name = "openai"

    def __init__(self, cfg: Settings = settings, client=None) -> None:
        self._cfg = cfg
        self._client = client  # inject an httpx.AsyncClient in tests

    async def analyze(self, prompt: str) -> LLMResult:
        import httpx

        from .prompt import SYSTEM_PROMPT

        headers = {}
        if self._cfg.openai_api_key:
            headers["Authorization"] = f"Bearer {self._cfg.openai_api_key}"
        client = self._client or httpx.AsyncClient(
            base_url=self._cfg.openai_base_url,
            timeout=self._cfg.llm_timeout_seconds,
            headers=headers,
        )
        payload = {
            "model": self._cfg.openai_model,
            "temperature": 0,
            "max_tokens": self._cfg.openai_max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        }
        try:
            resp = await post_with_retry(client, "/chat/completions", payload)
            data = resp.json()
        except Exception as exc:
            raise BackendError(f"openai-compatible call failed: {describe(exc)}") from exc
        finally:
            if self._client is None:
                await client.aclose()

        choices = data.get("choices") or []
        if not choices:
            raise BackendError("openai-compatible response has no choices")
        text = (choices[0].get("message") or {}).get("content") or ""
        hypothesis = parse_hypothesis(text)
        usage = data.get("usage") or {}
        in_tok = int(usage.get("prompt_tokens", 0))
        out_tok = int(usage.get("completion_tokens", 0))
        return LLMResult(
            hypothesis=hypothesis,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=round(
                in_tok / 1_000_000 * self._cfg.openai_price_in_per_mtok
                + out_tok / 1_000_000 * self._cfg.openai_price_out_per_mtok,
                6,
            ),
            backend=self.name,
        )


class FallbackBackend:
    """Try the primary; if it raises, analyse with the secondary.

    The result names the backend that actually answered, so the incident
    record, the message footer and the cost are honest about which model was
    used. Both attempts share the caller's timeout, so the reflex path stays
    bounded; a slow primary that times out leaves nothing for the fallback,
    which is the right trade: the alert still goes out, just without a
    hypothesis, exactly as before.
    """

    def __init__(self, primary, secondary) -> None:
        self._primary = primary
        self._secondary = secondary
        self.name = f"{primary.name}>{secondary.name}"

    async def analyze(self, prompt: str) -> LLMResult:
        try:
            return await self._primary.analyze(prompt)
        except BackendError as exc:
            logger.warning("primary backend %s failed (%s); trying %s", self._primary.name, exc, self._secondary.name)
            return await self._secondary.analyze(prompt)


def _single_backend(provider: str, cfg: Settings):
    if provider == "anthropic":
        return AnthropicBackend(cfg)
    if provider == "ollama":
        return OllamaBackend(cfg)
    if provider == "openai":
        return OpenAICompatibleBackend(cfg)
    if provider == "stub":
        return StubBackend()
    raise ValueError(f"unknown MINUS20_LLM_PROVIDER: {provider!r}")


def get_backend(cfg: Settings = settings):
    """Return the configured backend (ADR-0002: config, not code)."""
    primary = _single_backend(cfg.llm_provider.lower(), cfg)
    fallback = cfg.llm_fallback_provider.lower().strip()
    if fallback and fallback != cfg.llm_provider.lower():
        return FallbackBackend(primary, _single_backend(fallback, cfg))
    return primary
