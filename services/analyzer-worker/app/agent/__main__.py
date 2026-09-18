"""Entrypoint: `python -m app.agent`.

Wires the pieces and polls Telegram forever. Every optional dependency
degrades rather than blocks (ADR-0003): no Postgres means no history and no
memory, no pgvector means no memory, and the agent still answers from live
tools. The only hard requirements are a bot token and an allow-listed chat.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from ..budget import InMemoryBudget
from ..config import settings
from .chat import get_chat_backend
from .loop import Agent
from .memory import Memory, OllamaEmbedder
from .telegram import TelegramBot
from .tools import ToolContext

logger = logging.getLogger("minus20.agent")


async def build() -> TelegramBot:
    cfg = settings
    pool = None
    memory = None
    if cfg.store.lower() == "postgres":
        from ..stores import connect_pool

        pool = await connect_pool(cfg.postgres_dsn)
        memory = Memory(pool, OllamaEmbedder(cfg), cfg)
        if await memory.ensure_schema():
            try:
                docs = await memory.index_documents(cfg.runbooks_dir)
                incidents = await memory.index_incidents()
                logger.info("memory ready: indexed %d runbook chunks, %d incidents", docs, incidents)
            except Exception as exc:  # noqa: BLE001 - e.g. embedding model not pulled yet
                logger.warning("initial indexing failed, memory stays on for later: %s", exc)
    else:
        logger.warning("MINUS20_STORE is not postgres: no incident history, no memory")

    from ..backends import get_backend

    chat = get_chat_backend(cfg)
    ctx = ToolContext(cfg=cfg, pool=pool, memory=memory)
    agent = Agent(chat, ctx, InMemoryBudget(cfg.agent_daily_budget_usd), cfg)
    return TelegramBot(cfg, agent, chat, memory, pool, runbooks_dir=cfg.runbooks_dir, reflex=get_backend(cfg))


def main() -> None:
    from ..logsafe import configure_logging

    configure_logging(settings.log_level)
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        logger.error("MINUS20_TELEGRAM_BOT_TOKEN and MINUS20_TELEGRAM_CHAT_ID are required")
        sys.exit(2)

    async def _run() -> None:
        bot = await build()
        await asyncio.gather(bot.run(), bot.report_loop())

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:  # pragma: no cover
        pass


if __name__ == "__main__":  # pragma: no cover
    main()
