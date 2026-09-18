# Minus20

**Minus twenty minutes of not knowing.**

An alert tells you *what* broke. It never tells you *why*. The twenty minutes
that follow, at 03:00, are spent assembling context by hand and guessing.
Minus20 takes those minutes off every Kubernetes incident: it reacts to the
Alertmanager webhook, collects what an engineer would gather (Kubernetes
events, pod logs, Prometheus, Loki, CloudWatch), and puts a falsifiable
root-cause hypothesis with its evidence in your chat, in under a minute on a
cloud model and about two on a local one. Inside your cluster; the logs never
leave.

It never acts on your cluster. It recommends; the engineer decides.

**Try it without installing:** paste the output of `kubectl describe pod`
or `kubectl logs` into the bot and get the same message the installed
product sends after an alert. Owners of any Minus20 bot have this today; a
public trial bot follows once the demo cluster has a permanent home.

*Until 2026-09-17 this project was called SentinelOps; the old repository URL,
chart and image names redirect or remain, and `SENTINELOPS_*` environment
variables became `MINUS20_*`.*

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
weekly incident review with `/report`. Every verdict is also a test: the
benchmark replays your own incidents against the current model and grades
it by what you said really happened.

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
helm upgrade --install m20 oci://ghcr.io/maxuver/charts/minus20 -n minus20 --create-namespace
kubectl -n minus20 rollout status deploy/m20-analyzer-worker
```

From a checkout, `deploy/minus20` works in place of the OCI reference.

No cluster handy? `kind create cluster --config kind/cluster.yaml` gives you one
in a minute.

Break something on purpose and watch it work:

```bash
kubectl -n minus20 run billing-api --image=busybox --command -- \
  sh -c "echo 'ERROR could not connect to postgres:5432'; sleep 2; exit 1"

kubectl -n minus20 logs -f deploy/m20-analyzer-worker
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
| CloudWatch log group + cluster name | logs and metrics on EKS without Loki/Prometheus | optional |
| An LLM backend | the hypothesis | see below |

**The LLM is your choice, and one option costs nothing.** Run a local model
through Ollama and no data leaves your network — no API key, no per-alert cost.
Any OpenAI-compatible provider (DeepSeek, Groq, Together, OpenRouter, vLLM) or
Anthropic works through the same interface. Selecting a backend is one value,
never a code change.

```bash
helm upgrade --install m20 deploy/minus20 -n minus20 \
  --set config.llmProvider=ollama \
  --set config.ollamaUrl=http://host.docker.internal:11434 \
  --set config.collectors='k8s-events\,prometheus\,loki' \
  --set config.notifier=slack
```

---

## Design principles

1. **The AI is an overlay, never a dependency.** Ingestion is decoupled behind a
   length-capped Redis Stream, analysis has a hard timeout and a daily budget cap,
   and the raw alert is delivered even when the model is down. Switch Minus20
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
| `analyzer-worker` — collectors, redaction, budget, dedup, alert-storm grouping, cross-alert correlation (three alerts at 03:00, one revised hypothesis), graceful degradation | ✅ ([ADR-0006](docs/adr/0006-cross-alert-correlation.md)) |
| Collectors — Kubernetes events and pod logs, Prometheus, Loki, CloudWatch (Container Insights) | ✅ |
| LLM backends — local Ollama, any OpenAI-compatible API (DeepSeek, Mistral, Gemini, Groq, vLLM…), Anthropic, offline stub; local-first with a cloud fallback tier | ✅ |
| Delivery — Slack, Telegram | ✅ |
| Incident history — Postgres, with the redacted context each hypothesis was based on (audit trail) | ✅ |
| Read-only web UI for the incident history | ✅ |
| Agent in Telegram — read-only tools, memory of past incidents (pgvector), `/report` (on demand and on a weekly schedule), screenshots via a local vision model | ✅ ([ADR-0005](docs/adr/0005-reflex-and-deliberate-agent.md)) |
| Unattended demo — a CronJob that breaks a pod on a schedule and pages the reflex, under its own write-capable ServiceAccount; the product stays read-only | ✅ `demo.chaos.enabled` |
| MCP server — the same read-only tools for Gemini CLI, Claude Code, Cursor | ✅ |
| Helm chart with least-privilege RBAC, validated end-to-end on kind | ✅ |
| Fault-injection scenarios + replay benchmark; the eval set grows from real incidents with an engineer's verdict (`replay --from-store`) | ✅ [results](docs/BENCHMARKS.md) |
| CI — lint, tests, container build + CVE scan, helm lint, SAST, dependency scan, secret scan of full history, signed provenance + SBOM per image | ✅ |
| Terraform for AWS EKS | ✅ two real runs (2026-09-13/14): chart from GHCR, Postgres on EBS, MCP, real incidents, destroyed the same session, ~$0.13 each at list price — [proof](docs/EKS-RUN.md) |

### Known limitations

Stated plainly, because you will find them anyway:

- **The web UI has no authentication.** It is read-only and its Service is
  ClusterIP on purpose; reach it with `kubectl port-forward`, do not expose it.
- **No multi-tenancy.** Single team, single cluster.
- **Accuracy depends on the model, and it is measured.** With the local 7B
  model: 6/6 on plainly-stated scenarios, 5/7 on scenarios built to mislead.
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
  minus20/       Helm chart (services, RBAC, Postgres)
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
docker build services/analyzer-worker -t minus20/analyzer-worker:dev
kind load docker-image minus20/analyzer-worker:dev --name minus20
helm upgrade --install m20 deploy/minus20 -n minus20   --set images.analyzer=minus20/analyzer-worker:dev
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
