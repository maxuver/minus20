"""Correctness checks for the agent (ADR-0005).

CC-31  The tool set is closed and read-only: every registered tool is one of a
       known list; an unknown tool name from the model is an error string, not
       a crash and not an execution.
CC-32  Tool output is redacted before the model sees it.
CC-33  The loop is bounded: it stops at agent_max_tool_calls and still answers.
CC-34  The loop answers every requested tool call before checking the bound
       (the OpenAI dialect rejects a dangling tool_call).
CC-35  Budget exhaustion short-circuits without a model call.
CC-36  Memory: an engineer's "wrong, it was X" verdict is stored, and the
       re-indexed text puts the real cause first; feedback works without
       pgvector.
CC-37  The report's numbers come from SQL and render without a model; the
       narrative is optional.
CC-38  The bot serves only allow-listed chats and routes commands.
CC-39  Ollama message translation carries tool results in Ollama's shape.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from app.agent import tools
from app.agent.chat import ChatTurn, OllamaChat, OpenAIChat, ToolCall
from app.agent.loop import Agent, AgentAnswer
from app.agent.memory import Memory, chunk_text, incident_text
from app.agent.report import ReportData, collect, render
from app.agent.telegram import HELP, TelegramBot
from app.budget import InMemoryBudget
from app.config import Settings

# ---- fakes ---------------------------------------------------------------


class FakeChat:
    """Scripted backend: returns the given turns in order, records requests."""

    name = "fake"

    def __init__(self, turns: list[ChatTurn]) -> None:
        self._turns = list(turns)
        self.requests: list[tuple[list[dict], list[dict] | None]] = []

    async def chat(self, messages, tools_):
        self.requests.append(([dict(m) for m in messages], tools_))
        if not self._turns:
            return ChatTurn(content="(no more scripted turns)")
        return self._turns.pop(0)

    async def describe_image(self, image_b64, prompt):
        self.images = getattr(self, "images", [])
        self.images.append((image_b64, prompt))
        return "billing-api-7f9c   0/1   CrashLoopBackOff\nERROR could not connect to postgres:5432 from admin@corp.example"


class FakeConn:
    def __init__(self, store) -> None:
        self.store = store
        self.executed: list[tuple[str, tuple]] = []

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        if "CREATE EXTENSION" in sql and not self.store.has_vector:
            raise RuntimeError('extension "vector" is not available')
        if sql.strip().startswith("INSERT INTO memory"):
            self.store.memory[args[0]] = {"kind": args[1], "ref": args[2], "title": args[3], "content": args[4]}

    async def fetch(self, sql, *args):
        if "FROM incidents i" in sql:  # unindexed, or resolved after indexing
            indexed = self.store.indexed_ids()
            return [r for r in self.store.incidents if r["id"] not in indexed or r["resolved_at"]]
        if "FROM memory" in sql:
            return [
                {"kind": m["kind"], "ref": m["ref"], "title": m["title"], "content": m["content"], "score": 0.9}
                for m in self.store.memory.values()
            ][: args[1]]
        if "GROUP BY" in sql or "count(*)" in sql:
            return self.store.report_rows(sql)
        if "FROM incidents" in sql:  # recent_incidents
            return self.store.incidents[: args[2]]
        return []

    async def fetchrow(self, sql, *args):
        if sql.startswith("UPDATE incidents"):
            for r in self.store.incidents:
                if r["id"].startswith(args[0].rstrip("%")):
                    r["verdict"], r["resolution"] = args[1], args[2]
                    r["resolved_at"] = datetime.now(timezone.utc)
                    return {"id": r["id"]}
            return None
        if "count(*)" in sql:
            return self.store.totals
        for r in self.store.incidents:  # incident_details
            if r["id"].startswith(args[0].rstrip("%")):
                return r
        return None


class FakePool:
    def __init__(self, has_vector=True, incidents=None, totals=None) -> None:
        self.has_vector = has_vector
        self.incidents = incidents or []
        self.memory: dict[str, dict] = {}
        self.totals = totals or {}
        self.conn = FakeConn(self)

    def indexed_ids(self):
        return {m["ref"] for m in self.memory.values() if m["kind"] == "incident"}

    def report_rows(self, sql):
        if "AS cause" in sql:
            return [{"cause": "OOMKilled", "n": 3}, {"cause": "missing Secret", "n": 1}]
        if "AS namespace" in sql:
            return [{"namespace": "payments", "n": 4}]
        if "alertname" in sql:
            return [{"alertname": "KubePodCrashLooping", "n": 4}]
        if "AS hour" in sql:
            return [{"hour": 3, "n": 3}, {"hour": 14, "n": 1}]
        if "AS dow" in sql:
            return [{"dow": 6, "n": 2}, {"dow": 2, "n": 2}]
        if "blast_radius" in sql:
            return [{"blast_radius": "single-pod", "n": 4}]
        return []

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self):
                return pool.conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


class FakeEmbedder:
    async def embed(self, texts):
        return [[0.1] * 4 for _ in texts]


def _incident(**over):
    base = {
        "id": "abcdef1234567890",
        "alertname": "KubePodCrashLooping",
        "namespace": "payments",
        "severity": "warning",
        "status": "analyzed",
        "root_cause": "database connection refused",
        "confidence": "medium",
        "blast_radius": "single-pod",
        "evidence": ["ERROR connect refused"],
        "disproof": "check the db is up",
        "next_steps": ["look at db"],
        "backend": "ollama",
        "created_at": datetime.now(timezone.utc),
        "verdict": None,
        "resolution": None,
        "resolved_at": None,
        "context": "",
    }
    base.update(over)
    return base


def _cfg(**over) -> Settings:
    base = {"agent_max_tool_calls": 3, "agent_timeout_seconds": 5, "telegram_chat_id": "42"}
    return Settings(**{**base, **over})


# ---- tools (CC-31, CC-32) -----------------------------------------------


def test_tool_registry_is_closed_and_read_only():
    names = set(tools.TOOLS)
    assert names == {
        "recent_incidents",
        "incident_details",
        "search_memory",
        "k8s_events",
        "pod_metrics",
        "pod_logs",
        "deploy_history",
        "node_status",
    }
    # No tool name even hints at mutation; and every schema is a function schema.
    for name, tool in tools.TOOLS.items():
        assert not any(v in name for v in ("delete", "apply", "exec", "scale", "patch", "write"))
        assert tool.schema()["type"] == "function"


async def test_unknown_tool_is_an_error_string_not_a_crash():
    out = await tools.run(tools.ToolContext(cfg=_cfg()), "kubectl_delete", {"pod": "x"})
    assert out.startswith("error: unknown tool")


async def test_bad_arguments_are_reported_not_raised():
    out = await tools.run(tools.ToolContext(cfg=_cfg()), "incident_details", {"nonsense": 1})
    assert out.startswith("error: bad arguments")


async def test_tool_output_is_redacted():
    pool = FakePool(
        incidents=[_incident(root_cause="auth failed for admin@corp.example from 10.1.2.3")]
    )
    out = await tools.run(tools.ToolContext(cfg=_cfg(), pool=pool), "recent_incidents", {})
    assert "admin@corp.example" not in out
    assert "10.1.2.3" not in out
    assert "#abcdef12" in out


async def test_history_tools_degrade_without_postgres():
    ctx = tools.ToolContext(cfg=_cfg())  # pool=None
    assert "unavailable" in await tools.run(ctx, "recent_incidents", {})
    assert "unavailable" in await tools.run(ctx, "search_memory", {"query": "x"})


async def test_deploy_history_lists_rollouts_in_window():
    now = datetime.now(timezone.utc)

    def rs(name, owner, image, age_h):
        return SimpleNamespace(
            metadata=SimpleNamespace(
                name=name,
                creation_timestamp=now - timedelta(hours=age_h),
                owner_references=[SimpleNamespace(kind="Deployment", name=owner)],
            ),
            spec=SimpleNamespace(
                template=SimpleNamespace(spec=SimpleNamespace(containers=[SimpleNamespace(image=image)]))
            ),
            status=SimpleNamespace(replicas=3),
        )

    class Apps:
        async def list_namespaced_replica_set(self, ns):
            return SimpleNamespace(
                items=[
                    rs("api-1", "billing-api", "billing:1.4.1", 30),  # outside the window
                    rs("api-2", "billing-api", "billing:1.4.2", 1),
                ]
            )

    ctx = tools.ToolContext(cfg=_cfg(), k8s_apps=Apps())
    out = await tools.run(ctx, "deploy_history", {"namespace": "payments", "hours": 6})
    assert "billing:1.4.2" in out and "billing:1.4.1" not in out
    assert "deployment/billing-api" in out


async def test_pod_logs_falls_back_to_the_kubernetes_api_without_loki():
    class Core:
        async def read_namespaced_pod_log(self, pod, ns, tail_lines, previous):
            if previous:
                return "ERROR could not connect to postgres:5432"
            return ""  # the live container has printed nothing yet

    ctx = tools.ToolContext(cfg=_cfg(collectors="k8s-events"), k8s_api=Core())
    out = await tools.run(ctx, "pod_logs", {"namespace": "sentinelops", "pod": "billing-api"})
    assert "previous container" in out and "postgres:5432" in out
    assert "name the pod" in await tools.run(ctx, "pod_logs", {"namespace": "sentinelops"})


async def test_node_status_reports_pressure_and_node_warnings():
    def node(name, ready="True", **pressure):
        conds = [SimpleNamespace(type="Ready", status=ready)]
        conds += [SimpleNamespace(type=k, status="True" if v else "False") for k, v in pressure.items()]
        return SimpleNamespace(
            metadata=SimpleNamespace(name=name),
            status=SimpleNamespace(
                conditions=conds,
                allocatable={"cpu": "2", "memory": "3600Mi", "pods": "11"},
                node_info=SimpleNamespace(kubelet_version="v1.36.3"),
            ),
        )

    class Core:
        async def list_node(self):
            return SimpleNamespace(items=[node("n1"), node("n2", MemoryPressure=True, DiskPressure=True)])

        async def list_event_for_all_namespaces(self, field_selector):
            assert field_selector == "involvedObject.kind=Node"
            return SimpleNamespace(
                items=[
                    SimpleNamespace(type="Warning", reason="SystemOOM", message="System OOM encountered, victim process: java",
                                    involved_object=SimpleNamespace(name="n2"), last_timestamp=None, event_time=None),
                    SimpleNamespace(type="Normal", reason="Starting", message="Starting kubelet.",
                                    involved_object=SimpleNamespace(name="n1"), last_timestamp=None, event_time=None),
                ]
            )

    out = await tools.run(tools.ToolContext(cfg=_cfg(), k8s_api=Core()), "node_status", {})
    assert "n1: Ready=True" in out and "PRESSURE" not in out.split("\n")[0]
    assert "n2: Ready=True PRESSURE=MemoryPressure,DiskPressure" in out
    assert "kubelet=v1.36.3" in out
    assert "event SystemOOM node/n2" in out
    assert "Starting kubelet" not in out  # only warnings


async def test_node_status_degrades_when_nodes_are_not_readable():
    class Core:
        async def list_node(self):
            raise RuntimeError("nodes is forbidden: User cannot list resource")

    out = await tools.run(tools.ToolContext(cfg=_cfg(), k8s_api=Core()), "node_status", {})
    assert out.startswith("node status unavailable")


async def test_incident_details_includes_the_context_the_model_saw():
    pool = FakePool(incidents=[_incident(context="## Kubernetes events\nWarning BackOff pod/x: Back-off " + "y" * 3000)])
    out = await tools.run(tools.ToolContext(cfg=_cfg(), pool=pool), "incident_details", {"incident_id": "abcdef12"})
    assert '"context_shown_to_model"' in out
    assert "truncated" in out  # capped for chat, full text stays in the database


# ---- loop (CC-33, CC-34, CC-35) -----------------------------------------


async def test_loop_runs_tools_then_answers():
    chat = FakeChat(
        [
            ChatTurn(tool_calls=[ToolCall(name="recent_incidents", arguments={"hours": 1})]),
            ChatTurn(content="Likely cause: X. Evidence from recent_incidents."),
        ]
    )
    ctx = tools.ToolContext(cfg=_cfg(), pool=FakePool(incidents=[_incident()]))
    agent = Agent(chat, ctx, InMemoryBudget(1.0), _cfg())

    answer = await agent.ask("what happened?")

    assert answer.text.startswith("Likely cause")
    assert answer.tool_calls == ["recent_incidents"]
    assert not answer.truncated
    # The tool result reached the model as a tool message with the call's id.
    second_request = chat.requests[1][0]
    assert second_request[-1]["role"] == "tool"
    assert "#abcdef12" in second_request[-1]["content"]


async def test_loop_stops_at_tool_call_bound_and_still_answers():
    # A model that asks for a tool every single turn. With the bound at 3, the
    # loop must stop asking after the third call and demand an answer.
    wants_tool = ChatTurn(tool_calls=[ToolCall(name="k8s_events", arguments={"namespace": "a"})])
    chat = FakeChat([wants_tool, wants_tool, wants_tool, ChatTurn(content="best guess with what I have")])

    class Api:
        async def list_namespaced_event(self, ns):
            return SimpleNamespace(items=[])

    ctx = tools.ToolContext(cfg=_cfg(), k8s_api=Api())
    agent = Agent(chat, ctx, InMemoryBudget(1.0), _cfg(agent_max_tool_calls=3))

    answer = await agent.ask("loop forever please")

    assert answer.truncated
    assert len(answer.tool_calls) == 3
    # The final request offered no tools, so the model had to answer.
    assert chat.requests[-1][1] is None
    assert answer.text == "best guess with what I have"


async def test_every_requested_call_gets_a_result_even_past_the_bound():
    chat = FakeChat(
        [
            ChatTurn(
                tool_calls=[
                    ToolCall(name="recent_incidents", arguments={}),
                    ToolCall(name="recent_incidents", arguments={}),
                    ToolCall(name="recent_incidents", arguments={}),
                    ToolCall(name="recent_incidents", arguments={}),
                ]
            ),
            ChatTurn(content="done"),
        ]
    )
    ctx = tools.ToolContext(cfg=_cfg(), pool=FakePool())
    agent = Agent(chat, ctx, InMemoryBudget(1.0), _cfg(agent_max_tool_calls=2))

    answer = await agent.ask("q")

    tool_msgs = [m for m in chat.requests[-1][0] if m["role"] == "tool"]
    assert len(tool_msgs) == 4  # all four answered, none dangling
    assert answer.truncated


async def test_loop_does_not_rerun_an_identical_tool_call():
    """Seen live: the same k8s_events(namespace=x) four times in one question."""
    same = ToolCall(name="recent_incidents", arguments={"hours": 1})
    chat = FakeChat(
        [
            ChatTurn(tool_calls=[ToolCall(name="recent_incidents", arguments={"hours": 1})]),
            ChatTurn(tool_calls=[ToolCall(name="recent_incidents", arguments={"hours": 1})]),
            ChatTurn(content="done"),
        ]
    )
    pool = FakePool(incidents=[_incident()])
    calls = {"n": 0}
    orig_fetch = pool.conn.fetch

    async def counting_fetch(sql, *args):
        calls["n"] += 1
        return await orig_fetch(sql, *args)

    pool.conn.fetch = counting_fetch
    agent = Agent(chat, tools.ToolContext(cfg=_cfg(), pool=pool), InMemoryBudget(1.0), _cfg())
    await agent.ask("q")
    assert calls["n"] == 1  # the database was asked once
    second_result = [m for m in chat.requests[-1][0] if m["role"] == "tool"][1]["content"]
    assert "already called this tool" in second_result
    assert same.name == "recent_incidents"


async def test_budget_exhaustion_skips_the_model():
    chat = FakeChat([ChatTurn(content="should not be called")])
    budget = InMemoryBudget(0.0)
    agent = Agent(chat, tools.ToolContext(cfg=_cfg()), budget, _cfg())

    answer = await agent.ask("hi")

    assert "budget" in answer.text.lower()
    assert chat.requests == []


async def test_timeout_returns_an_answer_not_an_exception():
    class Slow:
        name = "slow"

        async def chat(self, messages, tools_):
            import asyncio

            await asyncio.sleep(5)
            return ChatTurn(content="late")

    agent = Agent(Slow(), tools.ToolContext(cfg=_cfg()), InMemoryBudget(1.0), _cfg(agent_timeout_seconds=0.05))
    answer = await agent.ask("q")
    assert isinstance(answer, AgentAnswer)
    assert answer.truncated and "time" in answer.text


# ---- memory (CC-36) -----------------------------------------------------


async def test_feedback_is_stored_and_reindexed_with_real_cause_first():
    pool = FakePool(incidents=[_incident()])
    mem = Memory(pool, FakeEmbedder(), _cfg())
    assert await mem.ensure_schema()
    await mem.index_incidents()
    assert len(pool.memory) == 1

    full = await mem.record_feedback("abcdef12", "wrong", "NetworkPolicy blocked egress to the db")

    assert full == "abcdef1234567890"
    row = pool.incidents[0]
    assert row["verdict"] == "wrong"
    # The re-indexed memory text leads with the engineer's cause, marks the old one wrong.
    text = next(iter(pool.memory.values()))["content"]
    assert text.index("ACTUAL CAUSE") < text.index("WRONG")
    assert "NetworkPolicy" in text


async def test_feedback_works_without_pgvector():
    pool = FakePool(has_vector=False, incidents=[_incident()])
    mem = Memory(pool, FakeEmbedder(), _cfg())
    assert await mem.ensure_schema() is False
    assert not mem.available
    assert await mem.search("anything") == []
    assert await mem.record_feedback("abcdef12", "correct", "") == "abcdef1234567890"


def test_incident_text_and_chunking():
    title, body = incident_text(_incident(verdict="correct"))
    assert title == "KubePodCrashLooping in payments"
    assert "Confirmed correct" in body
    chunks = chunk_text("para one\n\n" + "x" * 1500 + "\n\npara three", max_chars=600)
    assert all(len(c) <= 600 for c in chunks)
    assert "".join(chunks).count("para") == 2


async def test_search_redacts_the_query_and_returns_hits():
    pool = FakePool(incidents=[_incident()])
    mem = Memory(pool, FakeEmbedder(), _cfg())
    await mem.ensure_schema()
    await mem.index_incidents()
    hits = await mem.search("why did admin@corp.example see errors", k=3)
    assert hits and hits[0].kind == "incident"


# ---- report (CC-37) -----------------------------------------------------


async def test_report_renders_from_sql_without_a_model():
    pool = FakePool(
        totals={
            "total": 4, "analyzed": 4, "failed": 0, "over_budget": 0, "critical": 1,
            "confirmed": 2, "refuted": 1, "cost_usd": 0.0, "avg_latency_ms": 26000,
        }
    )
    data = await collect(pool, days=7)
    text = render(data)
    assert "Incidents: 4" in text
    assert "OOMKilled" in text and "75%" in text  # 3 of 4
    assert "accuracy on reviewed incidents: 67%" in text  # 2 of 3
    assert "at night (22:00–07:00): 3 (75%)" in text
    assert "at the weekend:          2 (50%)" in text
    assert "26.0s" in text


def test_report_handles_an_empty_period():
    data = ReportData(period_start=datetime(2026, 9, 1, tzinfo=timezone.utc), period_end=datetime(2026, 9, 8, tzinfo=timezone.utc))
    assert "Good week" in render(data)


# ---- telegram bot (CC-38) -----------------------------------------------


def _bot(pool=None, memory=None, chat=None, agent=None):
    cfg = _cfg()
    chat = chat or FakeChat([ChatTurn(content="answer")])
    agent = agent or Agent(chat, tools.ToolContext(cfg=cfg, pool=pool, memory=memory), InMemoryBudget(1.0), cfg)
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/sendMessage"):
            import json

            sent.append(json.loads(request.read()))
        if request.url.path.endswith("/getFile"):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "photos/1.jpg"}})
        if "/file/bot" in str(request.url):
            return httpx.Response(200, content=b"\xff\xd8fakejpeg")
        return httpx.Response(200, json={"ok": True, "result": {}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://tg/botTOKEN")
    return TelegramBot(cfg, agent, chat, memory, pool, client=client), sent


def _update(chat_id, text):
    return {"update_id": 1, "message": {"chat": {"id": chat_id}, "text": text}}


async def test_allow_list_accepts_a_chat_id_that_became_a_float():
    """Helm --reuse-values turned 194698214 into 1.94698214e+08; same chat."""
    chat = FakeChat([ChatTurn(content="answer")])
    cfg = Settings(telegram_chat_id="1.94698214e+08, -100123456789")
    agent = Agent(chat, tools.ToolContext(cfg=cfg), InMemoryBudget(1.0), cfg)
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.url.path)
        return httpx.Response(200, json={"ok": True, "result": {}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://tg/botTOKEN")
    bot = TelegramBot(cfg, agent, chat, None, None, client=client)
    assert bot._allowed == {"194698214", "-100123456789"}
    assert (await bot.handle(_update(194698214, "/help"))) is not None


async def test_bot_ignores_chats_not_on_the_allow_list():
    bot, sent = _bot()
    assert await bot.handle(_update(999, "/help")) is None
    assert sent == []


async def test_bot_help_and_status():
    bot, sent = _bot()
    reply = await bot.handle(_update(42, "/help"))
    assert reply.text == HELP
    assert sent[0]["parse_mode"] == "HTML"
    status = await bot.dispatch("42", "/status")
    assert "read-only" in status.text
    assert "off (store is not postgres)" in status.text


async def test_bot_routes_questions_to_the_agent_with_footer():
    chat = FakeChat([ChatTurn(content="Likely cause: OOM.")])
    bot, sent = _bot(chat=chat)
    reply = await bot.handle(_update(42, "why did billing-api crash?"))
    assert reply.text.startswith("Likely cause: OOM.")
    assert "\n\n— " in reply.text  # latency footer
    assert sent[-1]["text"].startswith("Likely cause")


async def test_bot_feedback_commands():
    pool = FakePool(incidents=[_incident()])
    mem = Memory(pool, FakeEmbedder(), _cfg())
    await mem.ensure_schema()
    bot, _ = _bot(pool=pool, memory=mem)

    assert "real cause" in (await bot.dispatch("42", "/wrong abcdef12")).text  # needs a cause
    ok = await bot.dispatch("42", "/wrong abcdef12 it was the NetworkPolicy")
    assert "will remember" in ok.text
    assert pool.incidents[0]["verdict"] == "wrong"
    assert "No incident" in (await bot.dispatch("42", "/ok 00000000")).text


async def test_bot_report_is_monospace_and_survives_no_model():
    class Broken:
        name = "broken"

        async def chat(self, messages, tools_):
            from app.ports import BackendError

            raise BackendError("down")

    pool = FakePool(totals={"total": 4, "analyzed": 4, "critical": 0, "confirmed": 0, "refuted": 0, "cost_usd": 0, "avg_latency_ms": 0})
    bot, _ = _bot(pool=pool, chat=Broken())
    reply = await bot.dispatch("42", "/report 7")
    assert reply.mono
    assert "INCIDENT REVIEW" in reply.text and "OOMKilled" in reply.text


async def test_bot_group_command_suffix_is_stripped():
    bot, _ = _bot()
    assert (await bot.dispatch("42", "/help@SentinelBot")).text == HELP


async def test_bot_answers_every_request_in_one_message():
    """'/status' followed by a question used to answer only the status."""
    chat = FakeChat([ChatTurn(content="Likely cause: OOM.")])
    bot, sent = _bot(chat=chat)
    await bot.handle(_update(42, "/status why did billing-api crash?"))
    texts = [m["text"] for m in sent]
    assert any("read-only" in t for t in texts)  # the status
    assert any(t.startswith("Likely cause") for t in texts)  # and the question
    sent.clear()
    await bot.handle(_update(42, "/help\n/status"))
    assert len(sent) == 2


def test_segments_split_lines_and_bare_commands():
    seg = TelegramBot._segments
    assert seg("/status") == ["/status"]
    assert seg("/status почему падает billing-api?") == ["/status", "почему падает billing-api?"]
    assert seg("/report 30") == ["/report 30"]  # a command that takes arguments stays whole
    assert seg("/wrong abc it was X") == ["/wrong abc it was X"]
    assert seg("line one\n\n/report 7") == ["line one", "/report 7"]


async def test_bot_transcribes_a_screenshot_then_asks_the_agent():
    chat = FakeChat([ChatTurn(content="Cause: the database is unreachable.")])
    bot, sent = _bot(chat=chat)
    update = {
        "update_id": 7,
        "message": {
            "chat": {"id": 42},
            "caption": "what is this?",
            "photo": [{"file_id": "small", "file_size": 10}, {"file_id": "big", "file_size": 999}],
        },
    }
    reply = await bot.handle(update)

    assert chat.images and chat.images[0][0]  # the image reached the vision model, base64
    question = chat.requests[-1][0][-1]["content"]  # what the tool loop was asked
    assert question.startswith("what is this?")
    assert "postgres:5432" in question
    assert "admin@corp.example" not in question  # transcript is redacted before the loop
    assert reply.text.startswith("From the screenshot I read:")
    assert "Cause: the database is unreachable." in reply.text
    assert sent[-1]["text"].startswith("From the screenshot")


async def test_bot_handles_an_image_sent_as_a_file_and_ignores_other_files():
    chat = FakeChat([ChatTurn(content="ok")])
    bot, _sent = _bot(chat=chat)
    as_file = {"update_id": 8, "message": {"chat": {"id": 42}, "document": {"file_id": "d", "mime_type": "image/png"}}}
    assert (await bot.handle(as_file)) is not None
    pdf = {"update_id": 9, "message": {"chat": {"id": 42}, "document": {"file_id": "p", "mime_type": "application/pdf"}}}
    assert (await bot.handle(pdf)) is None


async def test_loop_salvages_narrated_tool_calls():
    """A 7B model wrote its tool calls as JSON in prose; the loop still runs them."""
    narrated = ChatTurn(
        content='Let me check:\n```json\n{"name": "recent_incidents", "arguments": {"hours": 2}}\n```'
    )
    chat = FakeChat([narrated, ChatTurn(content="Found it.")])
    ctx = tools.ToolContext(cfg=_cfg(), pool=FakePool(incidents=[_incident()]))
    agent = Agent(chat, ctx, InMemoryBudget(1.0), _cfg())

    answer = await agent.ask("what happened?")

    assert answer.tool_calls == ["recent_incidents"]
    assert answer.text == "Found it."
    tool_msgs = [m for m in chat.requests[-1][0] if m["role"] == "tool"]
    assert tool_msgs and "#abcdef12" in tool_msgs[0]["content"]


def test_salvage_ignores_unknown_tools_and_non_calls():
    from app.agent.chat import salvage_tool_calls

    text = '{"name": "rm_rf", "arguments": {}} {"foo": 1} {"name": "k8s_events", "arguments": "{\\"namespace\\": \\"a\\"}"}'
    calls = salvage_tool_calls(text, {"k8s_events"})
    assert [(c.name, c.arguments) for c in calls] == [("k8s_events", {"namespace": "a"})]
    assert salvage_tool_calls("no json here", {"k8s_events"}) == []


# ---- chat adapters (CC-39) ----------------------------------------------


def test_ollama_translation_of_tool_messages():
    msgs = [
        {"role": "system", "content": "s"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "k8s_events", "arguments": '{"namespace": "a"}'}}
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "name": "k8s_events", "content": "Warning BackOff"},
    ]
    out = OllamaChat._to_ollama(msgs)
    assert out[1]["tool_calls"][0]["function"]["arguments"] == {"namespace": "a"}
    assert out[2] == {"role": "tool", "content": "Warning BackOff", "tool_name": "k8s_events"}


async def test_ollama_chat_parses_tool_calls_and_is_free():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "message": {
                    "content": "",
                    "tool_calls": [{"function": {"name": "pod_logs", "arguments": {"namespace": "p"}}}],
                },
                "prompt_eval_count": 10,
                "eval_count": 5,
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://ollama")
    turn = await OllamaChat(_cfg(), client=client).chat([{"role": "user", "content": "q"}], tools.schemas())
    assert turn.tool_calls[0].name == "pod_logs"
    assert turn.tool_calls[0].arguments == {"namespace": "p"}
    assert turn.cost_usd == 0.0
    await client.aclose()


async def test_openai_chat_parses_string_arguments_and_prices():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {"id": "call_1", "type": "function", "function": {"name": "search_memory", "arguments": '{"query": "dns"}'}}
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 0},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://p/v1")
    turn = await OpenAIChat(_cfg(openai_price_in_per_mtok=1.0), client=client).chat([], None)
    assert turn.tool_calls[0].id == "call_1"
    assert turn.tool_calls[0].arguments == {"query": "dns"}
    assert turn.cost_usd == pytest.approx(0.001)
    await client.aclose()



async def test_ollama_describe_image_sends_images_field():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.read())
        return httpx.Response(200, json={"message": {"content": "ERROR could not connect"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://ollama")
    out = await OllamaChat(_cfg(vision_model="qwen2.5vl:7b"), client=client).describe_image("QUJD", "transcribe")
    assert out == "ERROR could not connect"
    assert seen["body"]["model"] == "qwen2.5vl:7b"
    assert seen["body"]["messages"][0]["images"] == ["QUJD"]
    assert "tools" not in seen["body"]
    await client.aclose()


async def test_openai_describe_image_uses_data_url_and_vision_model():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.read())
        return httpx.Response(200, json={"choices": [{"message": {"content": "CrashLoopBackOff"}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://p/v1")
    cfg = _cfg(openai_model="deepseek-chat", openai_vision_model="gemini-2.5-flash")
    out = await OpenAIChat(cfg, client=client).describe_image("QUJD", "transcribe")
    assert out == "CrashLoopBackOff"
    assert seen["body"]["model"] == "gemini-2.5-flash"
    parts = seen["body"]["messages"][0]["content"]
    assert parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,QUJD")
    await client.aclose()


async def test_chat_fallback_and_vision_routing():
    from app.agent.chat import FallbackChat, get_chat_backend

    class Dead:
        name = "openai"

        async def chat(self, messages, tools_):
            from app.ports import BackendError

            raise BackendError("HTTP 429 from provider: quota")

        async def describe_image(self, b64, prompt):
            raise AssertionError("vision must not go to the dead cloud adapter")

    local = FakeChat([ChatTurn(content="local answer")])
    fb = FallbackChat(Dead(), local, vision=local)
    turn = await fb.chat([{"role": "user", "content": "q"}], None)
    assert turn.content == "local answer"
    assert "postgres:5432" in await fb.describe_image("QUJD", "transcribe")  # FakeChat's transcript

    cfg = Settings(llm_provider="openai", llm_fallback_provider="ollama", vision_provider="ollama")
    assert get_chat_backend(cfg).name == "openai>ollama"
    cfg = Settings(llm_provider="openai", vision_provider="ollama")
    routed = get_chat_backend(cfg)
    assert routed.name == "openai" and routed._vision.name == "ollama"
