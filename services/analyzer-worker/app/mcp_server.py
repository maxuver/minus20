"""SentinelOps as an MCP server: the same read-only tools, for any agent.

Gemini CLI, Claude Code, Cursor and the rest all speak the Model Context
Protocol. Rather than compete with them for the terminal, SentinelOps offers
them what they lack: memory of this cluster's incidents, the engineer's real
resolutions, and the same bounded observations the built-in agent uses.

Nothing new is exposed. Every tool here is the registry in `app/agent/tools.py`,
called through `tools.run`, so redaction, truncation and error handling are the
same code path, and there is still no tool that can change anything. Each tool
is annotated read-only/non-destructive so a client can show that to its user.

    python -m app.mcp_server            # stdio, for a local agent CLI
    SENTINELOPS_MCP_TRANSPORT=http ...   # streamable HTTP, in-cluster
"""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from .agent import tools
from .agent.memory import Memory, OllamaEmbedder
from .agent.tools import ToolContext
from .config import Settings, settings

logger = logging.getLogger("sentinelops.mcp")

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)

INSTRUCTIONS = (
    "Read-only observations about one Kubernetes cluster and its incident history. "
    "Nothing here can change the cluster. Check search_memory before explaining a "
    "failure: the engineer's recorded resolution of a past incident beats any guess."
)


@dataclass
class _State:
    """Holds the ToolContext; filled by the lifespan (prod) or directly (tests)."""

    ctx: ToolContext | None = None

    def get(self) -> ToolContext:
        if self.ctx is None:
            raise RuntimeError("SentinelOps MCP server is not initialised")
        return self.ctx


async def build_context(cfg: Settings = settings) -> ToolContext:
    """Same wiring as the Telegram agent: optional Postgres, optional memory."""
    pool = memory = None
    if cfg.store.lower() == "postgres":
        import asyncpg

        pool = await asyncpg.create_pool(cfg.postgres_dsn)
        memory = Memory(pool, OllamaEmbedder(cfg), cfg)
        if await memory.ensure_schema():
            try:
                await memory.index_incidents()
            except Exception as exc:  # noqa: BLE001 - memory is optional
                logger.warning("initial indexing failed: %s", exc)
    return ToolContext(cfg=cfg, pool=pool, memory=memory)


def build_server(ctx: ToolContext | None = None, cfg: Settings = settings) -> MCPServer:
    """Wrap the closed tool registry as MCP tools.

    Pass a ToolContext to bind one directly (tests); otherwise it is built
    inside the server's own event loop, which is where an asyncpg pool must live.
    """
    state = _State(ctx)

    @asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[None]:
        owned = state.ctx is None
        if owned:
            state.ctx = await build_context(cfg)
        try:
            yield
        finally:
            if owned and state.ctx is not None:
                await state.ctx.close()

    server = MCPServer(name="sentinelops", instructions=INSTRUCTIONS, lifespan=lifespan)

    async def recent_incidents(hours: int = 24, namespace: str | None = None, limit: int = 10) -> str:
        return await tools.run(state.get(), "recent_incidents", {"hours": hours, "namespace": namespace, "limit": limit})

    async def incident_details(incident_id: str) -> str:
        return await tools.run(state.get(), "incident_details", {"incident_id": incident_id})

    async def search_memory(query: str, limit: int = 5) -> str:
        return await tools.run(state.get(), "search_memory", {"query": query, "limit": limit})

    async def k8s_events(namespace: str, pod: str | None = None) -> str:
        return await tools.run(state.get(), "k8s_events", {"namespace": namespace, "pod": pod})

    async def pod_metrics(namespace: str, pod: str) -> str:
        return await tools.run(state.get(), "pod_metrics", {"namespace": namespace, "pod": pod})

    async def pod_logs(namespace: str, pod: str | None = None, minutes: int = 15) -> str:
        return await tools.run(state.get(), "pod_logs", {"namespace": namespace, "pod": pod, "minutes": minutes})

    async def deploy_history(namespace: str, hours: int = 24) -> str:
        return await tools.run(state.get(), "deploy_history", {"namespace": namespace, "hours": hours})

    async def node_status() -> str:
        return await tools.run(state.get(), "node_status", {})

    wrappers = {
        "recent_incidents": recent_incidents,
        "incident_details": incident_details,
        "search_memory": search_memory,
        "k8s_events": k8s_events,
        "pod_metrics": pod_metrics,
        "pod_logs": pod_logs,
        "deploy_history": deploy_history,
        "node_status": node_status,
    }
    # The registry is the source of truth: a tool without a wrapper is a bug,
    # and a wrapper without a registry entry would be a tool nobody reviewed.
    missing = set(wrappers) ^ set(tools.TOOLS)
    if missing:
        raise RuntimeError(f"MCP wrappers and tool registry disagree: {missing}")
    for name, fn in wrappers.items():
        server.add_tool(fn, name=name, description=tools.TOOLS[name].description, annotations=READ_ONLY)
    return server


def main() -> None:  # pragma: no cover - entrypoint
    from .logsafe import configure_logging

    # stdio carries the protocol on stdout, so logs must go to stderr.
    configure_logging(settings.log_level, stream=sys.stderr)
    server = build_server()
    if settings.mcp_transport.lower() == "http":
        # In-cluster only: the chart exposes it as a ClusterIP Service, never outside.
        server.run(transport="streamable-http", host="0.0.0.0", port=settings.mcp_port)  # nosec B104
    else:
        server.run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover
    main()
