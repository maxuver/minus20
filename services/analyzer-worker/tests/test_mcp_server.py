"""Correctness checks for the MCP server.

CC-40  The MCP tool list is exactly the agent's closed registry, and every
       tool is annotated read-only and non-destructive.
CC-41  A tool call over the protocol goes through tools.run: output is
       redacted, errors come back as text, never as a crash.
CC-42  Bad arguments are rejected by the protocol layer, not executed.
"""

from __future__ import annotations

from mcp.client import Client

from app.agent import tools
from app.agent.tools import ToolContext
from app.config import Settings
from app.mcp_server import build_server
from tests.test_agent import FakePool, _incident


def _ctx(**over) -> ToolContext:
    return ToolContext(cfg=Settings(collectors="k8s-events"), **over)


async def test_mcp_exposes_exactly_the_closed_registry_as_read_only():
    async with Client(build_server(_ctx())) as client:
        listed = await client.list_tools()
    by_name = {t.name: t for t in listed.tools}
    assert set(by_name) == set(tools.TOOLS)
    for tool in by_name.values():
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False
        assert tool.description == tools.TOOLS[tool.name].description


async def test_mcp_call_is_redacted_and_carries_incident_ids():
    pool = FakePool(incidents=[_incident(root_cause="db auth failed for admin@corp.example")])
    async with Client(build_server(_ctx(pool=pool))) as client:
        result = await client.call_tool("recent_incidents", {"hours": 6})
    text = "".join(getattr(c, "text", "") for c in result.content)
    assert "#abcdef12" in text
    assert "admin@corp.example" not in text


async def test_mcp_tool_failure_is_text_not_exception():
    class BrokenApi:
        async def list_namespaced_event(self, ns):
            raise RuntimeError("apiserver unreachable")

    async with Client(build_server(_ctx(k8s_api=BrokenApi()))) as client:
        result = await client.call_tool("k8s_events", {"namespace": "payments"})
    text = "".join(getattr(c, "text", "") for c in result.content)
    assert "error: k8s_events failed" in text


async def test_mcp_rejects_missing_required_argument():
    async with Client(build_server(_ctx())) as client:
        result = await client.call_tool("incident_details", {})
    assert result.is_error


async def test_mcp_pod_logs_reads_previous_container_via_k8s_api():
    class Core:
        async def read_namespaced_pod_log(self, pod, ns, tail_lines, previous):
            return "ERROR could not connect to postgres:5432" if previous else ""

    async with Client(build_server(_ctx(k8s_api=Core()))) as client:
        result = await client.call_tool("pod_logs", {"namespace": "minus20", "pod": "billing-api"})
    text = "".join(getattr(c, "text", "") for c in result.content)
    assert "previous container" in text and "postgres:5432" in text


def test_registry_and_wrappers_cannot_drift(monkeypatch):
    fake = tools.Tool("exec_shell", "no", {"type": "object"}, lambda *a, **k: None)
    monkeypatch.setitem(tools.TOOLS, "exec_shell", fake)
    try:
        build_server(_ctx())
    except RuntimeError as exc:
        assert "exec_shell" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("a tool nobody wrapped was exposed silently")


async def test_mcp_server_context_from_lifespan(monkeypatch):
    """Without an injected context the lifespan builds one (store=memory → no pool)."""
    import app.mcp_server as m

    built = {}

    async def fake_build(cfg):
        built["cfg"] = cfg
        return _ctx()

    monkeypatch.setattr(m, "build_context", fake_build)
    async with Client(build_server(cfg=Settings(store="memory"))) as client:
        result = await client.call_tool("recent_incidents", {})
    text = "".join(getattr(c, "text", "") for c in result.content)
    assert "unavailable" in text
    assert built["cfg"].store == "memory"
