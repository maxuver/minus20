# Architecture

One page. The decisions behind it live in the ADRs linked at the end; this is
the map.

## Two processes, one boundary

```
                      ┌────────────────── always on ──────────────────┐
Alertmanager ──► ingest-api ──► Redis Stream ──► analyzer-worker        REFLEX
  webhook         (FastAPI)     (consumer group)  collect → redact →    one LLM call per alert
                                                  budget → 1 call →     hard timeout, daily cap
                                                  persist → notify      dedup, dead-letter
                                                       │
                                                       ├─► Postgres (redacted incidents) ◄─────┐
                                                       ├─► Slack / Telegram (hypothesis)       │
                                                       └─► web-ui (read-only history)          │
                                                                                               │
Engineer in Telegram ──► sentinel-agent                                          DELIBERATE    │
   "why did billing-api crash?"     bounded tool loop, on demand ──────────────────────────────┘
   "what changed in the last hour?"    tools: closed, read-only (events, logs, metrics,
   "/report 7"                                rollouts, incident history, memory)
   "/wrong <id> it was X"              memory: pgvector, local embeddings
                                       feedback written back, becomes memory
```

**Reflex** reacts to every alert with exactly one bounded model call. It never
depends on the agent. If the model, the budget or the agent is gone, the raw
alert is still delivered (ADR-0001, ADR-0003).

**Deliberate** runs only when a human asks. It may call tools in a loop, but
every tool observes and none can change anything; the loop is capped by tool
calls, wall clock and a daily budget of its own (ADR-0005).

Both run from the same container image with different entrypoints
(`python -m app.worker`, `python -m app.agent`) and share one read-only
ServiceAccount. A third entrypoint, `python -m app.mcp_server`, serves the
agent's tool registry over the Model Context Protocol so an external agent
(Gemini CLI, Claude Code) can use SentinelOps as its memory of the cluster;
it adds no tool and no permission.

## Ports and adapters

The worker depends on interfaces (`services/analyzer-worker/app/ports.py`),
never on vendors. Choosing an adapter is one environment variable; swapping a
vendor is never a code change (ADR-0002).

| Port | Adapters | Selected by |
|---|---|---|
| `Collector` | `K8sEventsCollector`, `PrometheusCollector`, `LokiCollector`, `StubCollector`, fanned out by `AggregateCollector` | `SENTINELOPS_COLLECTORS` |
| `LLMBackend` | `OllamaBackend` (local, $0), `OpenAICompatibleBackend` (DeepSeek, Groq, Gemini, vLLM…), `AnthropicBackend`, `StubBackend`; `FallbackBackend` tiers any two | `SENTINELOPS_LLM_PROVIDER`, `_LLM_FALLBACK_PROVIDER` |
| `Notifier` | `SlackNotifier` (Incoming Webhook), `TelegramNotifier`, `StubNotifier` | `SENTINELOPS_NOTIFIER` |
| `IncidentStore` | `PostgresStore`, `InMemoryStore` | `SENTINELOPS_STORE` |
| `Budget` | `RedisBudget` (shared across replicas), `InMemoryBudget` | Redis present or not |
| `Deduplicator` | `RedisDeduplicator` (`SET NX EX`), `InMemoryDeduplicator` | Redis present or not |
| `ChatBackend` (agent) | `OllamaChat`, `OpenAIChat`, `FallbackChat`; vision routed separately | `SENTINELOPS_LLM_PROVIDER`, `_VISION_PROVIDER` |
| `Embedder` (agent) | `OllamaEmbedder` | `SENTINELOPS_EMBED_MODEL` |

Tests inject fakes at the same ports (`httpx.MockTransport`, fake pools, fake
Kubernetes clients), which is why the whole suite runs offline in about a
second.

## What flows where, and what never leaves

1. Alertmanager POSTs the alert. `ingest-api` validates it with Pydantic and
   publishes it to a length-capped Redis Stream. Nothing else is parsed here.
2. The worker reads it through a consumer group, checks the dedup window,
   and runs the collectors: Kubernetes events (`kubectl describe`-level),
   PromQL instant queries, LogQL over a bounded window. Fixed set, small caps.
3. **Redaction** masks e-mails, IPs, tokens, cloud keys and secret-shaped
   values in everything collected. It runs before any model call and has no
   off switch (ADR-0002).
4. One structured model call produces a hypothesis with evidence, the cheapest
   disproof, blast radius and next steps (ADR-0004). The context is framed as
   untrusted data; the model holds no tools on this path.
5. The redacted incident is stored **together with the exact redacted context
   the model was shown** (the audit trail: what did it see when it said that),
   the message is delivered, the cost is charged to the daily budget. Failure
   at any step still delivers the alert.
6. A message read by a worker that dies mid-analysis is reclaimed by the next
   worker (`XAUTOCLAIM` after an idle threshold) and analysed, bypassing the
   dedup key the dead worker already set. Each pod is its own consumer, a
   worker stops reading on SIGTERM and finishes the message in hand, and dead
   consumer names are pruned at startup. Every log line passes through the
   redactor before it is written, exception text included.
7. Two clocks are recorded per incident: the model call (`latency_ms`) and
   the wall clock from the alert firing to the hypothesis
   (`time_to_hypothesis_ms`), which is what an engineering manager asks for.

With the Ollama backend and local embeddings, no byte of log, metric or
event leaves the cluster. That is the difference between "we redact" and
"there is nothing to redact from".

## Security posture in one paragraph

Model output is never executed. The worker's model has no tools. The agent's
tools are a closed registry of read-only observations (`app/agent/tools.py`);
adding one is a code review of its verbs. RBAC grants get/list on events,
pods, pods/log, nodes, replicasets and deployments, and nothing with a write verb.
Credentials live in Secrets, never in values or the ConfigMap; the Telegram
token is kept out of logs by pinning httpx to WARNING. A prompt injection in a
log line can produce a wrong sentence in chat; it cannot produce an action.

## Deployment shape

Helm chart `deploy/sentinelops` (published as OCI at
`ghcr.io/maxuver/charts/sentinelops`): ingest-api, analyzer-worker, Redis,
optional Postgres (pgvector image), optional web-ui, optional agent. Images
come from GHCR on every push to `main`, gated on tests, SAST, SCA and secret
scanning. `infra/terraform` stands up an ephemeral EKS for the same chart.

## Decisions

- [ADR-0001](adr/0001-event-driven-pipeline-over-agent-loop.md) — one bounded call per alert, not an agent loop
- [ADR-0002](adr/0002-llm-privacy-and-pluggable-backends.md) — mandatory redaction, backend is configuration
- [ADR-0003](adr/0003-graceful-degradation.md) — the AI is an overlay; the alert always arrives
- [ADR-0004](adr/0004-hypothesis-evidence-and-blast-radius.md) — a hypothesis carries evidence, its disproof and blast radius
- [ADR-0005](adr/0005-reflex-and-deliberate-agent.md) — reflex and deliberate agent as separate processes
