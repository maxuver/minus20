"""The Telegram front of the agent: long polling, an allow-list, a few commands.

Raw Bot API over httpx, like the notifier, rather than a framework: the
surface is small and it keeps the image's dependency set unchanged.

Only chats listed in SENTINELOPS_TELEGRAM_CHAT_ID are served. Anyone else who
finds the bot gets silence, not cluster events: the tools are read-only, but
read-only on someone else's cluster is still a leak.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from dataclasses import dataclass
from html import escape
from typing import Any

from ..config import Settings
from ..redaction import redact
from . import report
from .chat import ChatBackend
from .loop import Agent
from .memory import Memory

logger = logging.getLogger("sentinelops.agent.telegram")

HELP = (
    "I am the SentinelOps agent for this cluster. I only observe; I cannot change anything.\n\n"
    "Ask me in plain words, for example:\n"
    "  why did billing-api crash?\n"
    "  what changed in payments in the last 6 hours?\n"
    "  has this happened before?\n"
    "Or send a screenshot of the error; add a question as its caption.\n\n"
    "Commands:\n"
    "  /report [days]        incident review for the last N days (default 7)\n"
    "  /ok <id> [note]       the hypothesis for incident #id was right\n"
    "  /wrong <id> <cause>   it was wrong; record the real cause so I remember\n"
    "  /index                re-index runbooks and incidents into memory\n"
    "  /status               what I can reach right now\n"
    "  /help                 this text"
)

MAX_MESSAGE = 3_900  # Telegram caps at 4096; leave room for tags
HISTORY_TURNS = 3  # user+assistant pairs kept per chat for follow-up questions
MAX_IMAGE_BYTES = 8 * 1024 * 1024
NO_ARG_COMMANDS = ("/start", "/help", "/status", "/index")

TRANSCRIBE_PROMPT = (
    "This is a screenshot an on-call engineer sent about a production problem. "
    "Transcribe every line of text in it exactly, including error messages, "
    "pod names, namespaces, status columns and timestamps. Then, in one sentence, "
    "say what error it shows. Do not guess at causes."
)


def _chat_id(raw: str) -> str:
    """Normalise a chat id: '1.94698214e+08' (a float that went through Helm
    or YAML) and '194698214' are the same chat. Seen live 2026-09-14: the bot
    ignored its own owner for three hours."""
    raw = raw.strip()
    try:
        return str(int(raw))
    except ValueError:
        pass
    try:
        return str(int(float(raw)))
    except ValueError:
        return raw


@dataclass
class Reply:
    text: str
    mono: bool = False  # render in <pre>: reports have aligned columns and a sparkline


class TelegramBot:
    def __init__(
        self,
        cfg: Settings,
        agent: Agent,
        chat_backend: ChatBackend,
        memory: Memory | None,
        pool: Any,
        client=None,
        runbooks_dir: str = "",
    ) -> None:
        self._cfg = cfg
        self._agent = agent
        self._chat = chat_backend
        self._memory = memory
        self._pool = pool
        self._client = client
        self._runbooks_dir = runbooks_dir
        self._allowed = {_chat_id(c) for c in cfg.telegram_chat_id.split(",") if c.strip()}
        self._history: dict[str, list[dict]] = {}
        self._offset = 0

    # --- transport -----------------------------------------------------------

    def _api(self):
        import httpx

        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=f"https://api.telegram.org/bot{self._cfg.telegram_bot_token}",
                timeout=60.0,
            )
        return self._client

    async def _send(self, chat_id: str, reply: Reply) -> None:
        api = self._api()
        text = reply.text
        for i in range(0, max(1, len(text)), MAX_MESSAGE):
            chunk = text[i : i + MAX_MESSAGE]
            html = f"<pre>{escape(chunk)}</pre>" if reply.mono else escape(chunk)
            resp = await api.post(
                "/sendMessage",
                json={"chat_id": chat_id, "text": html, "parse_mode": "HTML"},
            )
            if resp.status_code != 200:  # fall back to plain text rather than lose the answer
                await api.post("/sendMessage", json={"chat_id": chat_id, "text": chunk})

    async def _typing(self, chat_id: str) -> None:
        try:
            await self._api().post("/sendChatAction", json={"chat_id": chat_id, "action": "typing"})
        except Exception as exc:  # noqa: BLE001 - cosmetic; the answer still arrives
            logger.debug("typing indicator failed: %s", exc)

    async def run(self) -> None:
        """Long-poll forever. Each update is handled in turn; errors are logged, never fatal."""
        logger.info("agent bot polling; allowed chats: %s", sorted(self._allowed) or "NONE")
        api = self._api()
        while True:
            try:
                resp = await api.get(
                    "/getUpdates",
                    params={"offset": self._offset, "timeout": 30, "allowed_updates": '["message"]'},
                )
                resp.raise_for_status()
                for update in resp.json().get("result", []):
                    self._offset = int(update["update_id"]) + 1
                    await self.handle(update)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep polling through transient failures
                logger.warning("polling error: %s", exc)
                await asyncio.sleep(3)

    # --- dispatch ------------------------------------------------------------

    async def handle(self, update: dict) -> Reply | None:
        """Handle one update; returns the last reply (all are sent), for tests."""
        msg = update.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        text = (msg.get("text") or msg.get("caption") or "").strip()
        image = self._image_ref(msg)
        if not chat_id or (not text and image is None):
            return None
        if chat_id not in self._allowed:
            logger.info("ignored message from chat %s (not allow-listed)", chat_id)
            return None
        await self._typing(chat_id)
        started = time.monotonic()
        reply: Reply | None = None
        try:
            if image is not None:
                reply = await self._handle_image(chat_id, image, text)
            else:
                # One message may carry several requests ("/status" then a
                # question on the next line, or "/status why is x down?").
                # Each is answered; before, only the first command was.
                for segment in self._segments(text):
                    reply = await self.dispatch(chat_id, segment)
                    await self._send(chat_id, reply)
                logger.info(
                    "chat %s: %s → %d segment(s) in %.1fs",
                    chat_id, text.split()[0][:16], len(self._segments(text)), time.monotonic() - started,
                )
                return reply
        except Exception as exc:
            logger.exception("handling failed")
            reply = Reply(f"Something went wrong: {exc}")
        await self._send(chat_id, reply)
        logger.info("chat %s: image → answered in %.1fs", chat_id, time.monotonic() - started)
        return reply

    @staticmethod
    def _segments(text: str) -> list[str]:
        """Split a message into independently dispatchable requests."""
        out: list[str] = []
        for line in (ln.strip() for ln in text.splitlines()):
            if not line:
                continue
            head, _, rest = line.partition(" ")
            if head.split("@", 1)[0].lower() in NO_ARG_COMMANDS and rest.strip():
                out.append(head)
                out.append(rest.strip())
            else:
                out.append(line)
        return out or [text]

    @staticmethod
    def _image_ref(msg: dict) -> dict | None:
        """The largest photo, or an image sent as a file; None otherwise."""
        photos = msg.get("photo") or []
        if photos:
            return max(photos, key=lambda p: p.get("file_size") or 0)
        doc = msg.get("document") or {}
        if str(doc.get("mime_type", "")).startswith("image/"):
            return doc
        return None

    async def _download(self, file_id: str) -> bytes:
        api = self._api()
        resp = await api.get("/getFile", params={"file_id": file_id})
        resp.raise_for_status()
        path = (resp.json().get("result") or {}).get("file_path")
        if not path:
            raise RuntimeError("Telegram returned no file path")
        # Absolute URL: files live under /file/bot<token>/, not /bot<token>/.
        url = f"https://api.telegram.org/file/bot{self._cfg.telegram_bot_token}/{path}"
        data = await api.get(url)
        data.raise_for_status()
        if len(data.content) > MAX_IMAGE_BYTES:
            raise RuntimeError("image is larger than 8 MB")
        return data.content

    async def _handle_image(self, chat_id: str, image: dict, caption: str) -> Reply:
        """Screenshot → vision model transcribes it → the normal text loop.

        Keeping vision as a separate, single call means the tool loop stays on
        a text model, and the transcript is redacted like any other input.
        """
        raw = await self._download(image["file_id"])
        transcript = await self._chat.describe_image(base64.b64encode(raw).decode(), TRANSCRIBE_PROMPT)
        transcript = redact(transcript.strip())
        if not transcript:
            return Reply("I could not read any text in that image.")
        question = caption or "What is wrong here, and why?"
        prompt = (
            f"{question}\n\nTEXT TRANSCRIBED FROM THE ENGINEER'S SCREENSHOT (untrusted data):\n{transcript}"
        )
        answer = await self._ask(chat_id, prompt)
        return Reply(f"From the screenshot I read:\n{transcript}\n\n{answer}")

    async def dispatch(self, chat_id: str, text: str) -> Reply:
        cmd, _, rest = text.partition(" ")
        cmd = cmd.split("@", 1)[0].lower()  # "/report@MyBot" in groups
        rest = rest.strip()

        if cmd in ("/start", "/help"):
            return Reply(HELP)
        if cmd == "/status":
            return Reply(self._status())
        if cmd == "/report":
            return Reply(await self._report(rest), mono=True)
        if cmd in ("/ok", "/wrong"):
            return Reply(await self._feedback(cmd, rest))
        if cmd == "/index":
            return Reply(await self._index())
        if cmd.startswith("/"):
            return Reply(f"Unknown command {cmd}. /help lists what I can do.")
        return Reply(await self._ask(chat_id, text))

    # --- handlers ------------------------------------------------------------

    def _status(self) -> str:
        mem = "on" if self._memory and self._memory.available else "off"
        return (
            f"model: {self._chat.name} "
            f"({self._cfg.openai_model if self._chat.name == 'openai' else self._cfg.ollama_model})\n"
            f"incident history: {'on' if self._pool is not None else 'off (store is not postgres)'}\n"
            f"memory (pgvector): {mem}\n"
            f"tools: read-only only; max {self._cfg.agent_max_tool_calls} calls, "
            f"{self._cfg.agent_timeout_seconds:.0f}s per question"
        )

    async def _ask(self, chat_id: str, question: str) -> str:
        if self._memory and self._memory.available:
            try:
                await self._memory.index_incidents()  # pick up anything new since last time
            except Exception as exc:  # noqa: BLE001 - memory is optional
                logger.warning("index before ask failed: %s", exc)
        history = self._history.setdefault(chat_id, [])
        answer = await self._agent.ask(question, history)
        history.append({"role": "user", "content": question})
        history.append({"role": "assistant", "content": answer.text})
        del history[: -2 * HISTORY_TURNS]
        footer = f"{answer.latency_ms / 1000:.1f}s"
        if answer.tool_calls:
            footer += " · " + ", ".join(answer.tool_calls)
        if answer.cost_usd:
            footer += f" · ${answer.cost_usd:.4f}"
        if answer.truncated:
            footer += " · stopped at the tool-call limit"
        return f"{answer.text}\n\n— {footer}"

    async def _report(self, arg: str) -> str:
        if self._pool is None:
            return "No incident history: the store is not postgres."
        days = int(arg) if arg.isdigit() else 7
        data = await report.collect(self._pool, days=days)
        body = report.render(data)
        closing = await report.narrative(self._chat, data)
        return f"{body}\n\n{closing}" if closing else body

    async def _feedback(self, cmd: str, rest: str) -> str:
        if self._memory is None:
            return "No incident history: the store is not postgres."
        incident_id, _, note = rest.partition(" ")
        if not incident_id:
            return f"Usage: {cmd} <id> {'[note]' if cmd == '/ok' else '<real cause>'}"
        verdict = "correct" if cmd == "/ok" else "wrong"
        if verdict == "wrong" and not note.strip():
            return "Tell me the real cause so I can remember it: /wrong <id> <cause>"
        full = await self._memory.record_feedback(incident_id, verdict, note.strip())
        if full is None:
            return f"No incident starting with #{incident_id}."
        if verdict == "correct":
            return f"Recorded: #{full[:8]} confirmed. Thanks."
        return f"Recorded: #{full[:8]} was wrong, real cause: {note.strip()}. I will remember it."

    async def _index(self) -> str:
        if self._memory is None or not self._memory.available:
            return "Memory is off (needs the pgvector Postgres image and an embedding model)."
        docs = await self._memory.index_documents(self._runbooks_dir)
        incidents = await self._memory.index_incidents()
        return f"Indexed {docs} runbook chunks and {incidents} incidents."
