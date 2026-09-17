# Deploying Minus20 to Kubernetes

Helm chart: [`minus20/`](minus20). The defaults run **fully offline** on a
local kind cluster: the stub LLM backend (no API key), the Kubernetes-events
collector, and a read-only RBAC ServiceAccount.

## Local cluster (kind)

```bash
# 1. build the service images
docker build services/ingest-api      -t minus20/ingest-api:dev
docker build services/analyzer-worker -t minus20/analyzer-worker:dev

# 2. side-load them into the kind nodes
kind load docker-image minus20/ingest-api:dev      --name minus20
kind load docker-image minus20/analyzer-worker:dev --name minus20

# 3. install
helm upgrade --install m20 deploy/minus20 -n minus20 --create-namespace
kubectl -n minus20 rollout status deploy/m20-analyzer-worker
```

## Smoke test (end to end)

```bash
# a failing pod produces real Warning events for the collector to read
kubectl -n minus20 run billing-api --image=nginx:tag-does-not-exist

# fire an Alertmanager webhook at ingest-api and watch the worker
kubectl -n minus20 run alert-sender --image=curlimages/curl --restart=Never --rm -i --command -- \
  curl -s -X POST http://m20-ingest-api:8080/webhook/alertmanager -H 'content-type: application/json' \
  -d '{"version":"4","status":"firing","alerts":[{"status":"firing","labels":{"alertname":"KubePodCrashLooping","namespace":"minus20","pod":"billing-api","severity":"warning"},"annotations":{"description":"crash looping"},"fingerprint":"deadbeef01"}]}'

kubectl -n minus20 logs deploy/m20-analyzer-worker | tail
# -> incident alert=KubePodCrashLooping status=analyzed backend=stub ...
```

## Verify what you are about to run

Every image on GHCR carries a signed SLSA build provenance attestation: proof
that this exact digest was built by this repository's public workflow from a
specific commit, not on someone's laptop. Verify before the first install:

```bash
gh attestation verify oci://ghcr.io/maxuver/minus20/analyzer-worker:latest --owner maxuver
gh attestation verify oci://ghcr.io/maxuver/minus20/ingest-api:latest --owner maxuver
gh attestation verify oci://ghcr.io/maxuver/minus20/web-ui:latest --owner maxuver
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
sa=system:serviceaccount:minus20:m20-analyzer
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
helm upgrade --install m20 deploy/minus20 -n minus20 \
  --set config.collectors='k8s-events\,prometheus\,loki' \
  --set config.prometheusUrl=http://kps-kube-prometheus-stack-prometheus.monitoring:9090 \
  --set config.lokiUrl=http://loki.monitoring:3100
kubectl -n minus20 rollout restart deploy/m20-analyzer-worker
```

Validated on kind: a crash-looping pod produced real BackOff events, real
`kube_pod_container_status_restarts_total` / `container_memory_working_set_bytes`
metrics, and real log lines shipped by Promtail, all collected by the three
collectors and fed to the analyzer.

## Autonomous loop (Alertmanager fires the pipeline)

With the monitoring stack installed, Minus20 runs with no manual step. A
Prometheus rule fires on a crash-looping pod, Alertmanager routes it to the
ingest-api webhook (routing is in `kind/values-monitoring.yaml`), and the
analyzer produces an incident.

```bash
kubectl apply -f kind/minus20-demo-rule.yaml       # fast crash-loop alert
kubectl -n minus20 run billing-api --image=busybox --command -- \
  sh -c "echo boom; sleep 2; exit 1"

# ~90s later, with no manual curl:
kubectl -n minus20 logs deploy/m20-ingest-api      | grep queued
kubectl -n minus20 logs deploy/m20-analyzer-worker | grep 'incident alert'
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
kubectl -n minus20 create secret generic m20-slack \
  --from-literal=webhook-url='https://hooks.slack.com/services/T.../B.../...'

helm upgrade --install m20 deploy/minus20 -n minus20 \
  --set config.notifier=slack
```

**Telegram** needs a bot token from @BotFather and the target chat id:

```bash
kubectl -n minus20 create secret generic m20-telegram \
  --from-literal=bot-token='123456:ABC...'

helm upgrade --install m20 deploy/minus20 -n minus20 \
  --set config.notifier=telegram --set config.telegramChatId=123456789
```

Neither credential ever goes into `values.yaml` or the ConfigMap. A Slack
webhook URL *is* the credential — anyone holding it can post to the channel.

## EKS with CloudWatch instead of Loki and Prometheus

Container Insights ships pod logs to CloudWatch Logs and pod metrics to the
`ContainerInsights` namespace. Two collectors read them, read-only, with the
pod's own AWS identity (EKS Pod Identity on the chart's ServiceAccount;
`infra/terraform/minus20-identity.tf` creates the role and the
association, policy: `logs:StartQuery`, `logs:GetQueryResults`,
`cloudwatch:GetMetricData`):

```bash
helm upgrade --install m20 oci://ghcr.io/maxuver/charts/minus20 -n minus20 \
  --set config.collectors='k8s-events\,k8s-logs\,cloudwatch-logs\,cloudwatch-metrics' \
  --set config.cloudwatchLogGroup=/aws/containerinsights/<cluster>/application \
  --set config.cloudwatchClusterName=<cluster>
```

Logs Insights queries are asynchronous and metered; the collector caps each
at 20 s and 50 lines. Written against the Container Insights field names
(`kubernetes.namespace_name`, `kubernetes.pod_name`, `log`); a custom Fluent
Bit layout needs the query adjusted in `app/cloudwatch.py`. Tested against
a fake client in CI; a live run on EKS with Container Insights enabled is
still to be done.

## Alert storms

A node dies or a bad rollout lands and thirty pods fire the same alert. The
first one in a namespace is analysed as usual; the rest inside
`config.stormWindowSeconds` (default 120) are recorded as members of that
incident and never sent to the model. The chat gets one hypothesis and then
"⚡ Alert storm: 5 pods so far … analysed once as #id" at 2, 5, 10, 25, 50
and so on. Measured on kind: one webhook with five alerts, one model call,
three messages instead of five analyses.

## Correlation: three alerts at 03:00, one hypothesis

The other shape of a night page is a crash loop, a replicas mismatch and an
error-rate alert firing within a minute in the same namespace: three
symptoms, one fault. The first alert of a namespace is analysed at once (the
first hypothesis is never delayed). A later alert of a *different* name in
that namespace inside `config.correlationWindowSeconds` (default 180) is
attached to it: its context is collected, redacted and stored, the chat gets
a one-line "attached to #id", and no separate hypothesis is written. After
`config.correlationSettleSeconds` (default 20) one revision runs: a single
model call over the leader's context plus every attached alert's, asking for
the one cause that explains them all. The leader's hypothesis is replaced,
the previous cause is kept as "revised from", and the chat gets one message
listing all the alerts. At most one revision per settle period per
namespace, across replicas (Redis). Set `correlationWindowSeconds: 0` to
turn it off. Design and limits: [ADR-0006](../docs/adr/0006-cross-alert-correlation.md).

Measured on kind, 2026-09-17, `qwen2.5:7b` on CPU: `KubePodCrashLooping`,
`KubeDeploymentReplicasMismatch` and `HighErrorRate` posted 8 s apart in one
namespace. Leader analysed in 48 s ("PostgreSQL database is unreachable or
misconfigured"), two members attached with no model call, one revision 20 s
later in 55 s: "PostgreSQL database outage", confidence high, blast radius
`cluster`, evidence citing the connection error and the error-rate alert on
the other pod. Two model calls for three alerts; the previous cause kept as
`revised_from`. 130 s from the first alert to the revised hypothesis on the
local model; a cloud model does the same two calls in about 20 s.

## Unattended: the weekly review sends itself, and a demo that breaks itself

Two knobs make a cluster run for weeks with nobody at the keyboard.

**The weekly review on a schedule.** With `agent.weeklyReport.weekday: "0"`
(Monday; `hourUtc: 8`, `days: 7`) the agent sends `/report` to every allowed
chat by itself. Empty weekday (the default) turns it off.

**A demo that produces incidents.** `demo.chaos.enabled: true` installs a
CronJob (`schedule: "0 */6 * * *"`) that removes the previous chaos pod,
creates one that fails in a realistic way (`missing-secret`,
`db-unreachable`, `wrong-host`, `oom`, `bad-image`; random unless
`demo.chaos.mode` is set), waits until the cluster shows the failure and
posts the Alertmanager-shaped alert to ingest-api. From there it is the real
path: real pod, real events, real logs, a real hypothesis, real history for
`/report` and for the engineer's verdicts. This is the one component in the
chart that writes to the cluster: it runs under its own ServiceAccount with
create/delete on pods in the release namespace only, and the product's
ServiceAccount stays read-only. Never enable it on a cluster you care about.
Run one now: `kubectl -n minus20 create job chaos-now --from=cronjob/m20-chaos`.

Measured on kind, 2026-09-17: job created `chaos-wrong-host-1035`, saw the
first restart after 11 s, posted the alert; the reflex answered in 63 s.

## Real LLM backend

Selecting a backend is one value; the code never changes (ADR-0002).

**Local model, zero egress, $0 per alert** (Ollama). The URL must be reachable
from inside the cluster. On kind or Docker Desktop, the host's Ollama is
`host.docker.internal`; in a real cluster, run Ollama as a Service and point
at it.

```bash
helm upgrade --install m20 deploy/minus20 -n minus20 \
  --set config.llmProvider=ollama \
  --set config.ollamaUrl=http://host.docker.internal:11434 \
  --set config.ollamaModel=qwen2.5:7b \
  --set config.collectors=k8s-events\,prometheus\,loki
```

**Any OpenAI-compatible provider** (DeepSeek by default; Groq, Together,
OpenRouter, vLLM or LM Studio by changing `openaiBaseUrl`):

```bash
kubectl -n minus20 create secret generic m20-llm \
  --from-literal=openai-api-key=sk-...
helm upgrade --install m20 deploy/minus20 -n minus20 \
  --set config.llmProvider=openai \
  --set config.collectors=k8s-events\,prometheus\,loki
```

**Gemini** is the same adapter with Google's OpenAI-compatible endpoint. Create
a key at aistudio.google.com/apikey (keys created in 2026 do not necessarily
start with `AIza`), store it under the same `openai-api-key` name (the name
means "the OpenAI *dialect*", not the company), and name a current model:

```bash
kubectl -n minus20 create secret generic m20-llm --from-literal=openai-api-key='...'
helm upgrade --install m20 deploy/minus20 -n minus20   --set config.llmProvider=openai   --set config.openaiBaseUrl=https://generativelanguage.googleapis.com/v1beta/openai/   --set config.openaiModel=gemini-3.6-flash   --set config.openaiPriceInPerMtok=0 --set config.openaiPriceOutPerMtok=0
```

Measured 2026-09-13: hard benchmark 5/5 in 7.6 s average (`docs/BENCHMARKS.md`).
Google's free tier may use your data to improve its products, and its daily
quota ran out most days in testing; use a paid key or a local model for real
logs.

**Mistral** (EU-hosted; a free "Experiment" tier with 1 B tokens a month, 1
request per second, and the same training caveat as Gemini; paid tiers do
not train). Key from console.mistral.ai, same secret name, same adapter:

```bash
kubectl -n minus20 create secret generic m20-llm --from-literal=openai-api-key='...'
helm upgrade --install m20 deploy/minus20 -n minus20   --set config.llmProvider=openai   --set config.openaiBaseUrl=https://api.mistral.ai/v1   --set config.openaiModel=mistral-small-latest   --set config.openaiPriceInPerMtok=0 --set config.openaiPriceOutPerMtok=0
```

**DeepSeek** is the adapter's default base URL: only the key and the model
(`deepseek-flash`) are needed; about $0.001 per analysed alert at list price.

**Tiered: cloud when it answers, local when it does not.** `llmFallbackProvider`
names a second backend used only when the first raises (quota, outage,
timeout). The incident records which one answered. Screenshots are read by
`agent.visionProvider` (local by default) regardless of the chat provider.

```bash
helm upgrade --install m20 deploy/minus20 -n minus20   --set config.llmProvider=openai --set config.llmFallbackProvider=ollama   --set config.ollamaUrl=http://host.docker.internal:11434
```

Measured 2026-09-14, Gemini's free tier out of quota: primary failed with
HTTP 429, Ollama answered, `backend=ollama`, 131 s from the alert firing to
the hypothesis.

**Anthropic:**

```bash
kubectl -n minus20 create secret generic m20-llm \
  --from-literal=anthropic-api-key=sk-ant-...
helm upgrade --install m20 deploy/minus20 -n minus20 \
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

helm upgrade --install m20 deploy/minus20 -n minus20 \
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

## Use it from your own agent (MCP)

The seven read-only tools are also served over the Model Context Protocol, so
an agent you already use can ask Minus20 what happened in this cluster
before. Nothing new is exposed and nothing can be changed: it is the agent's
closed registry, annotated read-only, behind the same redaction.

**Locally, over stdio** (uses your kubeconfig; port-forward Postgres first if
you want history and memory):

```bash
kubectl -n minus20 port-forward svc/m20-postgres 5432:5432 &
cd services/analyzer-worker && pip install -r requirements.txt
MINUS20_STORE=postgres MINUS20_POSTGRES_DSN=postgresql://minus20:minus20@localhost:5432/minus20 \
  python -m app.mcp_server
```

Gemini CLI, `~/.gemini/settings.json`:

```json
{
  "mcpServers": {
    "minus20": {
      "command": "python",
      "args": ["-m", "app.mcp_server"],
      "cwd": "/path/to/minus20/services/analyzer-worker",
      "env": {
        "MINUS20_STORE": "postgres",
        "MINUS20_POSTGRES_DSN": "postgresql://minus20:minus20@localhost:5432/minus20"
      }
    }
  }
}
```

Claude Code:

```bash
claude mcp add minus20 -e MINUS20_STORE=postgres \
  -e MINUS20_POSTGRES_DSN=postgresql://minus20:minus20@localhost:5432/minus20 \
  -- python -m app.mcp_server
```

Then ask your agent: *"has billing-api crashed before, and what was the real
cause?"* and it will call `search_memory`.

**In the cluster, over HTTP**: `--set mcp.enabled=true` adds a ClusterIP
Service `m20-mcp` on port 8765 (`kubectl port-forward svc/m20-mcp 8765`), and
clients that speak streamable HTTP connect to `http://localhost:8765/mcp`.
