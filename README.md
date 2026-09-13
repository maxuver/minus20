# SentinelOps

**An alert tells you *what* broke. It never tells you *why*.**

SentinelOps automates the first twenty minutes of every Kubernetes incident. It
reacts to an Alertmanager webhook, collects the context an engineer would gather
by hand — Kubernetes events, Prometheus metrics, Loki logs — and returns a ranked
root-cause hypothesis with the evidence behind it, in seconds.

It never acts on your cluster. It recommends; the engineer decides.

---

## What you get

Every incident arrives in Slack or Telegram with:

- **The likely cause**, with a confidence level
- **The evidence** it is based on, so you can check the reasoning in seconds
- **The cheapest observation that would disprove it** — a hypothesis is cheap,
  knowing how to kill it fast is what saves the night
- **Blast radius**, tracked separately so a rare-but-catastrophic cause is never
  buried under a common one
- **Next steps**, and the backend, latency and cost of the analysis

And when that is not enough, you ask. An agent in the same chat investigates
with read-only tools, remembers what really caused the last similar incident
in *your* cluster (you tell it with `/wrong <id> <cause>`), and writes the
weekly incident review with `/report`.

Already use an agent in your terminal? The same tools and memory are an
[MCP server](deploy/README.md#use-it-from-your-own-agent-mcp): Gemini CLI or
Claude Code can ask "has this happened before?" and get the engineer's real
answer from last time.

---

## Quickstart

### Option A — any Kubernetes cluster, one command

The chart and the images are published to GitHub Container Registry on every
push to `main`, so there is nothing to clone or build. Offline defaults mean
no API key is needed either.

```bash
helm upgrade --install so oci://ghcr.io/maxuver/charts/sentinelops -n sentinelops --create-namespace
kubectl -n sentinelops rollout status deploy/so-analyzer-worker
```

From a checkout, `deploy/sentinelops` works in place of the OCI reference.

No cluster handy? `kind create cluster --config kind/cluster.yaml` gives you one
in a minute.

Break something on purpose and watch it work:

```bash
kubectl -n sentinelops run billing-api --image=busybox --command -- \
  sh -c "echo 'ERROR could not connect to postgres:5432'; sleep 2; exit 1"

kubectl -n sentinelops logs -f deploy/so-analyzer-worker
```

Full deployment guide, including the monitoring stack and the autonomous
Alertmanager loop: [`deploy/README.md`](deploy/README.md). A scripted
walkthrough for a first look or a screen recording: [`docs/DEMO.md`](docs/DEMO.md).

### Option B — docker compose, no cluster

```bash
docker compose up --build
curl -X POST http://localhost:8080/webhook/alertmanager \
  -H "Content-Type: application/json" \
  -d @services/ingest-api/tests/fixtures/crashloop.json
```

### Try it without a cluster at all

Six recorded fault-injection scenarios replay through the real pipeline and
print time-to-first-hypothesis and cost per alert:

```bash
cd services/analyzer-worker
pip install -r requirements-dev.txt
python -m app.replay
```

---

## What you need to provide

| Thing | Why | Required? |
|---|---|---|
| Kubernetes cluster with Alertmanager | the alerts to triage | yes |
| Slack Incoming Webhook **or** Telegram bot token | delivery | yes |
| Prometheus URL | metric context | optional |
| Loki URL | log context | optional |
| An LLM backend | the hypothesis | see below |

**The LLM is your choice, and one option costs nothing.** Run a local model
through Ollama and no data leaves your network — no API key, no per-alert cost.
Any OpenAI-compatible provider (DeepSeek, Groq, Together, OpenRouter, vLLM) or
Anthropic works through the same interface. Selecting a backend is one value,
never a code change.

```bash
helm upgrade --install so deploy/sentinelops -n sentinelops \
  --set config.llmProvider=ollama \
  --set config.ollamaUrl=http://host.docker.internal:11434 \
  --set config.collectors='k8s-events\,prometheus\,loki' \
  --set config.notifier=slack
```

---

## Design principles

1. **The AI is an overlay, never a dependency.** Ingestion is decoupled behind a
   length-capped Redis Stream, analysis has a hard timeout and a daily budget cap,
   and the raw alert is delivered even when the model is down. Switch SentinelOps
   off and you are back to exactly what you had before.
   ([ADR-0003](docs/adr/0003-graceful-degradation.md))
2. **Your logs never leave without permission.** Non-bypassable PII redaction
   runs before any model call, and a fully local backend means zero egress.
   ([ADR-0002](docs/adr/0002-llm-privacy-and-pluggable-backends.md))
3. **Cost is a line item, not a surprise.** A deterministic pipeline makes exactly
   one structured LLM call per alert, so the price is known before you switch it
   on. ([ADR-0001](docs/adr/0001-event-driven-pipeline-over-agent-loop.md))
4. **It cannot touch production.** The model holds no write-capable tool, every
   integration uses read-only credentials, and model output is never executed —
   so a prompt injection hidden in a log line yields a wrong suggestion in chat,
   not an action in your cluster.
5. **A hypothesis carries its own disproof.**
   ([ADR-0004](docs/adr/0004-hypothesis-evidence-and-blast-radius.md))
6. **Reflex and deliberation are separate processes.** The alert path makes one
   bounded call and never depends on the agent; the agent runs only when a
   human asks, with a closed set of read-only tools and a memory of this
   cluster's incidents. ([ADR-0005](docs/adr/0005-reflex-and-deliberate-agent.md))

---

## Status

| Area | State |
|---|---|
| `ingest-api` — Alertmanager webhook → Redis Streams | ✅ |
| `analyzer-worker` — collectors, redaction, budget, dedup, graceful degradation | ✅ |
| Collectors — Kubernetes events, Prometheus, Loki | ✅ |
| LLM backends — local Ollama, any OpenAI-compatible API (DeepSeek, Groq, vLLM…), Anthropic, offline stub | ✅ |
| Delivery — Slack, Telegram | ✅ |
| Incident history — Postgres, with the redacted context each hypothesis was based on (audit trail) | ✅ |
| Read-only web UI for the incident history | ✅ |
| Agent in Telegram — read-only tools, memory of past incidents (pgvector), `/report`, screenshots via a local vision model | ✅ ([ADR-0005](docs/adr/0005-reflex-and-deliberate-agent.md)) |
| MCP server — the same read-only tools for Gemini CLI, Claude Code, Cursor | ✅ |
| Helm chart with least-privilege RBAC, validated end-to-end on kind | ✅ |
| Fault-injection scenarios + replay benchmark | ✅ [results](docs/BENCHMARKS.md) |
| CI — lint, tests, container build, helm lint, SAST, dependency scan, secret scan of full history | ✅ |
| Terraform for AWS EKS | ✅ applied to real AWS on 2026-09-13, chart installed from GHCR, real incident analysed, destroyed 18 min later — [proof](docs/EKS-RUN.md) |

### Known limitations

Stated plainly, because you will find them anyway:

- **The web UI has no authentication.** It is read-only and its Service is
  ClusterIP on purpose; reach it with `kubectl port-forward`, do not expose it.
- **No multi-tenancy.** Single team, single cluster.
- **Accuracy depends on the model, and it is measured.** With the local 7B
  model: 6/6 on plainly-stated scenarios, 2/5 on scenarios built to mislead.
  With a current cloud model through the same pipeline: 6/6 and 5/5, in 7 s.
  Method and case-by-case results: [docs/BENCHMARKS.md](docs/BENCHMARKS.md).
  Nothing has been measured against real production incidents yet.
- **Kubernetes only.** No other alert sources yet.
- **The agent is slow on CPU.** With a local 7B model a multi-step question
  takes minutes (measured: 8 min for four tool calls); a cloud model takes
  seconds. The reflex path is unaffected.

---

## Repository layout

```
services/
  ingest-api/        FastAPI webhook receiver → Redis Streams
  analyzer-worker/   collectors, redaction, LLM backends, delivery, replay
    app/agent/       the Telegram agent: tool loop, memory, report (same image)
    scenarios/       recorded fault-injection scenarios
  web-ui/            read-only incident history (FastAPI + Jinja2)
deploy/
  sentinelops/       Helm chart (services, RBAC, Postgres)
infra/terraform/     AWS VPC + EKS (applied once for real, see docs/EKS-RUN.md)
kind/                local cluster and monitoring stack config
docs/
  ARCHITECTURE.md    the map: two processes, ports and adapters, what never leaves
  DEMO.md            25-minute walkthrough from an empty cluster to a hypothesis in chat
  VISION.md          why this project exists, in depth
  adr/               architecture decision records
```

## Development

```bash
cd services/analyzer-worker
pip install -r requirements-dev.txt
ruff check app tests && pytest
```

### Developing locally against kind

Build with a `:dev` tag, side-load it, and point the chart at it:

```bash
docker build services/analyzer-worker -t sentinelops/analyzer-worker:dev
kind load docker-image sentinelops/analyzer-worker:dev --name sentinelops
helm upgrade --install so deploy/sentinelops -n sentinelops   --set images.analyzer=sentinelops/analyzer-worker:dev
```

## License

[GNU AGPL-3.0](LICENSE) — Copyright (c) 2026 Maxim Patsyuk.

Free to use, modify and self-host, including inside a company. If you run a
modified version as a network service, the AGPL requires you to publish those
modifications.

**Commercial licence available.** Many organisations will not accept the AGPL,
and that is a reasonable position. The copyright holder can grant a separate
commercial licence on different terms — get in touch.

**What stays open.** Everything an engineer needs to run this on one cluster
is and remains AGPL: ingestion, analysis, collectors, redaction, every LLM
backend, delivery, history, the agent and its memory, the chart. Features
whose value is organisational (knowledge-base connectors, SLO and error-budget
reporting, multi-cluster, SSO) may be offered separately. Contributions are
welcome under the terms in [CONTRIBUTING.md](CONTRIBUTING.md).

Releases made before 2026-09-09 remain under the MIT licence they were published
under; this change applies from that date forward.
