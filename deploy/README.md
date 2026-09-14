# Deploying SentinelOps to Kubernetes

Helm chart: [`sentinelops/`](sentinelops). The defaults run **fully offline** on a
local kind cluster: the stub LLM backend (no API key), the Kubernetes-events
collector, and a read-only RBAC ServiceAccount.

## Local cluster (kind)

```bash
# 1. build the service images
docker build services/ingest-api      -t sentinelops/ingest-api:dev
docker build services/analyzer-worker -t sentinelops/analyzer-worker:dev

# 2. side-load them into the kind nodes
kind load docker-image sentinelops/ingest-api:dev      --name sentinelops
kind load docker-image sentinelops/analyzer-worker:dev --name sentinelops

# 3. install
helm upgrade --install so deploy/sentinelops -n sentinelops --create-namespace
kubectl -n sentinelops rollout status deploy/so-analyzer-worker
```

## Smoke test (end to end)

```bash
# a failing pod produces real Warning events for the collector to read
kubectl -n sentinelops run billing-api --image=nginx:tag-does-not-exist

# fire an Alertmanager webhook at ingest-api and watch the worker
kubectl -n sentinelops run alert-sender --image=curlimages/curl --restart=Never --rm -i --command -- \
  curl -s -X POST http://so-ingest-api:8080/webhook/alertmanager -H 'content-type: application/json' \
  -d '{"version":"4","status":"firing","alerts":[{"status":"firing","labels":{"alertname":"KubePodCrashLooping","namespace":"sentinelops","pod":"billing-api","severity":"warning"},"annotations":{"description":"crash looping"},"fingerprint":"deadbeef01"}]}'

kubectl -n sentinelops logs deploy/so-analyzer-worker | tail
# -> incident alert=KubePodCrashLooping status=analyzed backend=stub ...
```

## Verify what you are about to run

Every image on GHCR carries a signed SLSA build provenance attestation: proof
that this exact digest was built by this repository's public workflow from a
specific commit, not on someone's laptop. Verify before the first install:

```bash
gh attestation verify oci://ghcr.io/maxuver/sentinelops/analyzer-worker:latest --owner maxuver
gh attestation verify oci://ghcr.io/maxuver/sentinelops/ingest-api:latest --owner maxuver
gh attestation verify oci://ghcr.io/maxuver/sentinelops/web-ui:latest --owner maxuver
```

Each CI run also publishes an SBOM (SPDX) per image as a workflow artifact,
and the build fails on any CRITICAL or HIGH vulnerability with a fix
available (Trivy). The source is public, the Dockerfiles are five lines, and
the RBAC below is the whole set of permissions the software holds.

## RBAC (least privilege)

The ServiceAccount shared by the worker and the agent can only read: events,
pods and their logs, ReplicaSets and Deployments. No write verb anywhere, no
exec, no secrets:

```bash
sa=system:serviceaccount:sentinelops:so-analyzer
kubectl auth can-i list events    --as=$sa -A   # yes
kubectl auth can-i get pods/log   --as=$sa -A   # yes (the agent reads crash output)
kubectl auth can-i list secrets   --as=$sa -A   # no
kubectl auth can-i delete pods    --as=$sa -A   # no
kubectl auth can-i create pods/exec --as=$sa -A # no
```

Set `rbac.clusterWide=false` to restrict reads to the release namespace instead
of cluster-wide.

## Full observability stack (real Prometheus + Loki)

The k8s-events collector needs only the cluster API. To enrich with real metrics
and logs, install a monitoring stack and point the analyzer at it:

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo add grafana https://grafana.github.io/helm-charts
helm repo update

helm upgrade --install kps prometheus-community/kube-prometheus-stack \
  -n monitoring --create-namespace -f kind/values-monitoring.yaml
helm upgrade --install loki grafana/loki-stack -n monitoring \
  --set promtail.enabled=true

# switch the analyzer to all three collectors, wired to the in-cluster services
helm upgrade --install so deploy/sentinelops -n sentinelops \
  --set config.collectors='k8s-events\,prometheus\,loki' \
  --set config.prometheusUrl=http://kps-kube-prometheus-stack-prometheus.monitoring:9090 \
  --set config.lokiUrl=http://loki.monitoring:3100
kubectl -n sentinelops rollout restart deploy/so-analyzer-worker
```

Validated on kind: a crash-looping pod produced real BackOff events, real
`kube_pod_container_status_restarts_total` / `container_memory_working_set_bytes`
metrics, and real log lines shipped by Promtail, all collected by the three
collectors and fed to the analyzer.

## Autonomous loop (Alertmanager fires the pipeline)

With the monitoring stack installed, SentinelOps runs with no manual step. A
Prometheus rule fires on a crash-looping pod, Alertmanager routes it to the
ingest-api webhook (routing is in `kind/values-monitoring.yaml`), and the
analyzer produces an incident.

```bash
kubectl apply -f kind/sentinelops-demo-rule.yaml       # fast crash-loop alert
kubectl -n sentinelops run billing-api --image=busybox --command -- \
  sh -c "echo boom; sleep 2; exit 1"

# ~90s later, with no manual curl:
kubectl -n sentinelops logs deploy/so-ingest-api      | grep queued
kubectl -n sentinelops logs deploy/so-analyzer-worker | grep 'incident alert'
```

Validated on kind: pod restarts -> KubePodCrashLoopingFast fires -> Alertmanager
webhook -> `{"queued":1}` -> analyzer incident, end to end.

## Delivery: Slack or Telegram

Both channels render the same content: cause, evidence, the cheapest way to
disprove it, blast radius and next steps.

**Slack** uses an Incoming Webhook, so the only thing to set up is one URL
(api.slack.com → your app → Incoming Webhooks → Add New Webhook to Workspace).
No OAuth app to install, no scopes for a security team to review.

```bash
kubectl -n sentinelops create secret generic so-slack \
  --from-literal=webhook-url='https://hooks.slack.com/services/T.../B.../...'

helm upgrade --install so deploy/sentinelops -n sentinelops \
  --set config.notifier=slack
```

**Telegram** needs a bot token from @BotFather and the target chat id:

```bash
kubectl -n sentinelops create secret generic so-telegram \
  --from-literal=bot-token='123456:ABC...'

helm upgrade --install so deploy/sentinelops -n sentinelops \
  --set config.notifier=telegram --set config.telegramChatId=123456789
```

Neither credential ever goes into `values.yaml` or the ConfigMap. A Slack
webhook URL *is* the credential — anyone holding it can post to the channel.

## Real LLM backend

Selecting a backend is one value; the code never changes (ADR-0002).

**Local model, zero egress, $0 per alert** (Ollama). The URL must be reachable
from inside the cluster. On kind or Docker Desktop, the host's Ollama is
`host.docker.internal`; in a real cluster, run Ollama as a Service and point
at it.

```bash
helm upgrade --install so deploy/sentinelops -n sentinelops \
  --set config.llmProvider=ollama \
  --set config.ollamaUrl=http://host.docker.internal:11434 \
  --set config.ollamaModel=qwen2.5:7b \
  --set config.collectors=k8s-events\,prometheus\,loki
```

**Any OpenAI-compatible provider** (DeepSeek by default; Groq, Together,
OpenRouter, vLLM or LM Studio by changing `openaiBaseUrl`):

```bash
kubectl -n sentinelops create secret generic so-llm \
  --from-literal=openai-api-key=sk-...
helm upgrade --install so deploy/sentinelops -n sentinelops \
  --set config.llmProvider=openai \
  --set config.collectors=k8s-events\,prometheus\,loki
```

**Gemini** is the same adapter with Google's OpenAI-compatible endpoint. Create
a key at aistudio.google.com/apikey (keys created in 2026 do not necessarily
start with `AIza`), store it under the same `openai-api-key` name (the name
means "the OpenAI *dialect*", not the company), and name a current model:

```bash
kubectl -n sentinelops create secret generic so-llm --from-literal=openai-api-key='...'
helm upgrade --install so deploy/sentinelops -n sentinelops   --set config.llmProvider=openai   --set config.openaiBaseUrl=https://generativelanguage.googleapis.com/v1beta/openai/   --set config.openaiModel=gemini-3.6-flash   --set config.openaiPriceInPerMtok=0 --set config.openaiPriceOutPerMtok=0
```

Measured 2026-09-13: hard benchmark 5/5 in 7.6 s average (`docs/BENCHMARKS.md`).
Google's free tier may use your data to improve its products; use a paid key
or a local model for real logs.

**Tiered: cloud when it answers, local when it does not.** `llmFallbackProvider`
names a second backend used only when the first raises (quota, outage,
timeout). The incident records which one answered. Screenshots are read by
`agent.visionProvider` (local by default) regardless of the chat provider.

```bash
helm upgrade --install so deploy/sentinelops -n sentinelops   --set config.llmProvider=openai --set config.llmFallbackProvider=ollama   --set config.ollamaUrl=http://host.docker.internal:11434
```

Measured 2026-09-14, Gemini's free tier out of quota: primary failed with
HTTP 429, Ollama answered, `backend=ollama`, 131 s from the alert firing to
the hypothesis.

**Anthropic:**

```bash
kubectl -n sentinelops create secret generic so-llm \
  --from-literal=anthropic-api-key=sk-ant-...
helm upgrade --install so deploy/sentinelops -n sentinelops \
  --set config.llmProvider=anthropic \
  --set config.collectors=k8s-events\,prometheus\,loki
```

## The agent: ask it questions in Telegram (ADR-0005)

The worker is the reflex: one bounded call per alert. The agent is the
deliberate half: an engineer asks a question in Telegram and it investigates
with read-only tools, remembers this cluster's past incidents, and writes the
weekly incident review.

It needs the Telegram bot Secret (see Delivery above) and, for history and
memory, `config.store=postgres`. Memory uses pgvector (the chart's default
Postgres image) and a local embedding model:

```bash
ollama pull nomic-embed-text

helm upgrade --install so deploy/sentinelops -n sentinelops \
  --set config.store=postgres \
  --set config.llmProvider=ollama --set config.ollamaUrl=http://host.docker.internal:11434 \
  --set agent.enabled=true --set agent.telegramChatId=123456789
```

Then, in the chat:

```
why is billing-api in namespace payments crashing?
what changed in payments in the last 6 hours?
/report 7                      incident review: top causes, night/weekend share, verdicts
/wrong 3f9a1c2e NetworkPolicy blocked egress to the db
/ok 3f9a1c2e
```

**Screenshots work too.** Send a photo of the error (a terminal, a Grafana
panel, a log viewer) with a question as its caption. A local vision model
(`agent.visionModel`, default `qwen2.5vl:7b`, `ollama pull` it first)
transcribes the text, the transcript is redacted like any other input, and
the same tool loop answers. Measured on CPU: about a minute for transcription
plus the answer.

`/wrong` and `/ok` quote the `#id` from an alert message. The verdict is stored
on the incident and indexed, so the next similar incident is answered with
"the last time this happened the real cause was...". That is the part that
improves with use.

**What it can and cannot do.** Its tools are `recent_incidents`,
`incident_details`, `search_memory`, `k8s_events`, `pod_logs`, `pod_metrics`,
`deploy_history` and `node_status`. Every one observes. There is no shell, no `kubectl`, no
tool that changes anything, and the set is closed: adding one is a code
review, not a plugin. Every question is bounded by `agent.maxToolCalls`,
`agent.timeoutSeconds` and `agent.dailyBudgetUsd`.

**Measured, CPU-only, qwen2.5:7b.** One tool call and an answer: about 40 s.
A four-step investigation (events, logs, events again twice) that correctly
found "cannot connect to postgres:5432" in the previous container's output:
8 minutes. A cloud model does the same in about 20 s; that is what the
`openai` provider is for. Runbooks to index go in `agent.runbooks` as
filename → markdown.

**Indexing runbooks into agent memory.** Operational runbooks (markdown or text)
can be passed to the Helm release so the agent indexes them into pgvector memory.
Use Helm's `--set-file` to mount a runbook from your repository:

```bash
helm upgrade --install so deploy/sentinelops -n sentinelops \
  --set-file agent.runbooks."networkpolicy-dns-failure\.md"=docs/runbooks/networkpolicy-dns-failure.md
```

Or declare them directly in a custom `values.yaml`:

```yaml
agent:
  runbooks:
    networkpolicy-dns-failure.md: |
      # Runbook: DNS Failures After NetworkPolicy Changes
      ...
```

Runbooks are mounted to `/runbooks`, chunked, embedded via the local embedding
model, and searched alongside incident history when answering questions. You can
also send `/index` in Telegram to trigger a re-index.

## Use it from your own agent (MCP)

The seven read-only tools are also served over the Model Context Protocol, so
an agent you already use can ask SentinelOps what happened in this cluster
before. Nothing new is exposed and nothing can be changed: it is the agent's
closed registry, annotated read-only, behind the same redaction.

**Locally, over stdio** (uses your kubeconfig; port-forward Postgres first if
you want history and memory):

```bash
kubectl -n sentinelops port-forward svc/so-postgres 5432:5432 &
cd services/analyzer-worker && pip install -r requirements.txt
SENTINELOPS_STORE=postgres SENTINELOPS_POSTGRES_DSN=postgresql://sentinel:sentinel@localhost:5432/sentinelops \
  python -m app.mcp_server
```

Gemini CLI, `~/.gemini/settings.json`:

```json
{
  "mcpServers": {
    "sentinelops": {
      "command": "python",
      "args": ["-m", "app.mcp_server"],
      "cwd": "/path/to/sentinelops/services/analyzer-worker",
      "env": {
        "SENTINELOPS_STORE": "postgres",
        "SENTINELOPS_POSTGRES_DSN": "postgresql://sentinel:sentinel@localhost:5432/sentinelops"
      }
    }
  }
}
```

Claude Code:

```bash
claude mcp add sentinelops -e SENTINELOPS_STORE=postgres \
  -e SENTINELOPS_POSTGRES_DSN=postgresql://sentinel:sentinel@localhost:5432/sentinelops \
  -- python -m app.mcp_server
```

Then ask your agent: *"has billing-api crashed before, and what was the real
cause?"* and it will call `search_memory`.

**In the cluster, over HTTP**: `--set mcp.enabled=true` adds a ClusterIP
Service `so-mcp` on port 8765 (`kubectl port-forward svc/so-mcp 8765`), and
clients that speak streamable HTTP connect to `http://localhost:8765/mcp`.
