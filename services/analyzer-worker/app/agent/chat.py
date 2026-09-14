"""Multi-turn, tool-calling chat behind one port (ADR-0002 applied to the agent).

The worker's `LLMBackend.analyze(prompt)` is a single structured call. The
agent needs a conversation the model can extend with tool calls, so it gets
its own port, `ChatBackend.chat(messages, tools)`, with two adapters:

- ollama  — local, zero egress, $0. Ollama's /api/chat supports tools.
- openai  — any OpenAI-compatible endpoint (DeepSeek, Groq, vLLM, ...).

Messages are kept in the OpenAI shape internally; the Ollama adapter
translates on the way out. The model never sees a tool it could use to change
the cluster, because the registry (tools.py) contains none.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..config import Settings, settings
from ..errors import describe, post_with_retry
from ..ports import BackendError


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]
    id: str = field(default_factory=lambda: "call_" + uuid.uuid4().hex[:12])


@dataclass
class ChatTurn:
    """One assistant turn: either text, or a request to run tools, or both."""

    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


@runtime_checkable
class ChatBackend(Protocol):
    name: str

    async def chat(self, messages: list[dict], tools: list[dict] | None) -> ChatTurn: ...

    async def describe_image(self, image_b64: str, prompt: str) -> str: ...


_JSON_OBJECT = re.compile(r"\{(?:[^{}]|\{(?:[^{}]|\{[^{}]*\})*\})*\}", re.DOTALL)


def salvage_tool_calls(content: str, known: set[str]) -> list[ToolCall]:
    """Recover tool calls a small model *narrated* instead of *made*.

    Seen live on qwen2.5:7b: with tools offered, it answered in prose and put
    ```json {"name": "k8s_events", "arguments": {...}} ``` blocks in the text,
    with an empty tool_calls list. The engineer got a plan instead of an
    investigation. Any JSON object in the content with a known tool name and
    an object of arguments is treated as the call it was meant to be; anything
    else is left alone.
    """
    calls: list[ToolCall] = []
    for match in _JSON_OBJECT.finditer(content or ""):
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        name = data.get("name") or (data.get("function") or {}).get("name")
        args = data.get("arguments")
        if args is None and isinstance(data.get("function"), dict):
            args = data["function"].get("arguments")
        args = _parse_arguments(args)
        if isinstance(name, str) and name in known:
            calls.append(ToolCall(name=name, arguments=args))
    return calls


def assistant_message(turn: ChatTurn) -> dict:
    """Serialise a turn back into the message list (OpenAI shape)."""
    msg: dict[str, Any] = {"role": "assistant", "content": turn.content}
    if turn.tool_calls:
        msg["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
            }
            for tc in turn.tool_calls
        ]
    return msg


def tool_message(call: ToolCall, result: str) -> dict:
    return {"role": "tool", "tool_call_id": call.id, "name": call.name, "content": result}


def _parse_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


class OllamaChat:
    """Local tool-calling chat via Ollama's /api/chat."""

    name = "ollama"

    def __init__(self, cfg: Settings = settings, client=None) -> None:
        self._cfg = cfg
        self._client = client  # inject an httpx.AsyncClient in tests

    @staticmethod
    def _to_ollama(messages: list[dict]) -> list[dict]:
        out = []
        for m in messages:
            if m["role"] == "tool":
                # Ollama has no tool_call_id; it matches by order and tool_name.
                out.append({"role": "tool", "content": m["content"], "tool_name": m.get("name", "")})
            elif m["role"] == "assistant" and m.get("tool_calls"):
                out.append(
                    {
                        "role": "assistant",
                        "content": m.get("content") or "",
                        "tool_calls": [
                            {
                                "function": {
                                    "name": tc["function"]["name"],
                                    "arguments": _parse_arguments(tc["function"]["arguments"]),
                                }
                            }
                            for tc in m["tool_calls"]
                        ],
                    }
                )
            else:
                out.append({"role": m["role"], "content": m.get("content") or ""})
        return out

    async def chat(self, messages: list[dict], tools: list[dict] | None) -> ChatTurn:
        import httpx

        client = self._client or httpx.AsyncClient(
            base_url=self._cfg.ollama_url, timeout=self._cfg.agent_timeout_seconds
        )
        payload: dict[str, Any] = {
            "model": self._cfg.ollama_model,
            "stream": False,
            "options": {"temperature": 0},
            "messages": self._to_ollama(messages),
        }
        if tools:
            payload["tools"] = tools
        try:
            resp = await client.post("/api/chat", json=payload)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            raise BackendError(f"ollama chat failed: {describe(exc)}") from exc
        finally:
            if self._client is None:
                await client.aclose()

        msg = data.get("message") or {}
        calls = [
            ToolCall(
                name=str((tc.get("function") or {}).get("name", "")),
                arguments=_parse_arguments((tc.get("function") or {}).get("arguments")),
            )
            for tc in msg.get("tool_calls") or []
        ]
        return ChatTurn(
            content=msg.get("content") or "",
            tool_calls=calls,
            input_tokens=int(data.get("prompt_eval_count", 0)),
            output_tokens=int(data.get("eval_count", 0)),
            cost_usd=0.0,
        )

    async def describe_image(self, image_b64: str, prompt: str) -> str:
        """One call to a local vision model (qwen2.5vl, gemma3, llava, ...)."""
        import httpx

        client = self._client or httpx.AsyncClient(
            base_url=self._cfg.ollama_url, timeout=self._cfg.agent_timeout_seconds
        )
        payload = {
            "model": self._cfg.vision_model,
            "stream": False,
            "options": {"temperature": 0},
            "messages": [{"role": "user", "content": prompt, "images": [image_b64]}],
        }
        try:
            resp = await client.post("/api/chat", json=payload)
            resp.raise_for_status()
            return (resp.json().get("message") or {}).get("content") or ""
        except Exception as exc:
            raise BackendError(f"ollama vision call failed: {describe(exc)}") from exc
        finally:
            if self._client is None:
                await client.aclose()


class OpenAIChat:
    """Tool-calling chat against any OpenAI-compatible /chat/completions."""

    name = "openai"

    def __init__(self, cfg: Settings = settings, client=None) -> None:
        self._cfg = cfg
        self._client = client

    async def chat(self, messages: list[dict], tools: list[dict] | None) -> ChatTurn:
        import httpx

        headers = {}
        if self._cfg.openai_api_key:
            headers["Authorization"] = f"Bearer {self._cfg.openai_api_key}"
        client = self._client or httpx.AsyncClient(
            base_url=self._cfg.openai_base_url,
            timeout=self._cfg.agent_timeout_seconds,
            headers=headers,
        )
        payload: dict[str, Any] = {
            "model": self._cfg.openai_model,
            "temperature": 0,
            "messages": messages,
        }
        if tools:
            payload["tools"] = tools
        try:
            resp = await post_with_retry(client, "/chat/completions", payload)
            data = resp.json()
        except Exception as exc:
            raise BackendError(f"openai-compatible chat failed: {describe(exc)}") from exc
        finally:
            if self._client is None:
                await client.aclose()

        choices = data.get("choices") or []
        if not choices:
            raise BackendError("openai-compatible chat returned no choices")
        msg = choices[0].get("message") or {}
        calls = [
            ToolCall(
                id=str(tc.get("id") or "call_" + uuid.uuid4().hex[:12]),
                name=str((tc.get("function") or {}).get("name", "")),
                arguments=_parse_arguments((tc.get("function") or {}).get("arguments")),
            )
            for tc in msg.get("tool_calls") or []
        ]
        usage = data.get("usage") or {}
        in_tok = int(usage.get("prompt_tokens", 0))
        out_tok = int(usage.get("completion_tokens", 0))
        return ChatTurn(
            content=msg.get("content") or "",
            tool_calls=calls,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=round(
                in_tok / 1_000_000 * self._cfg.openai_price_in_per_mtok
                + out_tok / 1_000_000 * self._cfg.openai_price_out_per_mtok,
                6,
            ),
        )

    async def describe_image(self, image_b64: str, prompt: str) -> str:
        """Vision through the OpenAI dialect (Gemini, GPT-class; DeepSeek-chat has none)."""
        import httpx

        headers = {}
        if self._cfg.openai_api_key:
            headers["Authorization"] = f"Bearer {self._cfg.openai_api_key}"
        client = self._client or httpx.AsyncClient(
            base_url=self._cfg.openai_base_url,
            timeout=self._cfg.agent_timeout_seconds,
            headers=headers,
        )
        payload = {
            "model": self._cfg.openai_vision_model or self._cfg.openai_model,
            "temperature": 0,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                    ],
                }
            ],
        }
        try:
            resp = await client.post("/chat/completions", json=payload)
            resp.raise_for_status()
            choices = resp.json().get("choices") or []
        except Exception as exc:
            raise BackendError(f"openai-compatible vision call failed: {describe(exc)}") from exc
        finally:
            if self._client is None:
                await client.aclose()
        if not choices:
            raise BackendError("openai-compatible vision call returned no choices")
        return (choices[0].get("message") or {}).get("content") or ""


def get_chat_backend(cfg: Settings = settings) -> ChatBackend:
    """The agent shares the worker's provider setting; stub/anthropic fall back
    to Ollama for chat because the agent needs tool calling, which the stub
    cannot do and the Anthropic path here does not implement yet."""
    provider = cfg.llm_provider.lower()
    if provider == "openai":
        return OpenAIChat(cfg)
    return OllamaChat(cfg)
