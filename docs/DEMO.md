# Demo: from an empty cluster to a hypothesis in your chat

A step-by-step walkthrough that doubles as the script for a screen recording.
Every step is a command you can run; timings are what they take on a laptop
with Docker Desktop. Total: about 25 minutes the first time, 8 minutes once
the images are cached.

Choose your model before you start:

| | Local (Ollama) | Cloud (Gemini, DeepSeek, …) |
|---|---|---|
| Data leaves the cluster | never | yes, redacted |
| Cost per alert | $0 | cents, or a free tier |
| Time to hypothesis | 30–60 s on CPU | ~7 s |
| Hard-case accuracy (docs/BENCHMARKS.md) | 2/5 | 5/5 |

For a recording, use the cloud model: the waits are short. Show the local
one for the privacy point.

## Scene 1 — the cluster (2 min)

```bash
kind create cluster --name minus20
kubectl get nodes
```

What to say: one node, nothing installed, no monitoring stack needed for
this demo. Minus20 reads Kubernetes events directly.

## Scene 2 — install with one command (3 min)

Telegram: create a bot with @BotFather, put its token in a Secret, and find
your chat id (message the bot, then open
`https://api.telegram.org/bot<token>/getUpdates` in a browser; `chat.id`).

```bash
kubectl create namespace minus20
kubectl -n minus20 create secret generic m20-telegram --from-literal=bot-token='<token>'
```

Local model:

```bash
ollama pull qwen2.5:7b && ollama pull nomic-embed-text && ollama pull qwen2.5vl:7b
helm upgrade --install m20 oci://ghcr.io/maxuver/charts/minus20 -n minus20 \
  --set config.store=postgres \
  --set config.notifier=telegram --set config.telegramChatId=<chat id> \
  --set config.llmProvider=ollama --set config.ollamaUrl=http://host.docker.internal:11434 \
  --set agent.enabled=true --set mcp.enabled=true
```

Cloud model (Gemini through the OpenAI-compatible endpoint; the same lines
work for DeepSeek or Groq with another URL):

```bash
kubectl -n minus20 create secret generic m20-llm --from-literal=openai-api-key='<key>'
helm upgrade --install m20 oci://ghcr.io/maxuver/charts/minus20 -n minus20 \
  --set config.store=postgres \
  --set config.notifier=telegram --set config.telegramChatId=<chat id> \
  --set config.llmProvider=openai \
  --set config.openaiBaseUrl=https://generativelanguage.googleapis.com/v1beta/openai/ \
  --set config.openaiModel=gemini-3.6-flash \
  --set config.ollamaUrl=http://host.docker.internal:11434 \
  --set agent.enabled=true --set mcp.enabled=true
kubectl -n minus20 get pods -w
```

What to say: the chart and images come from GitHub Container Registry, so
there is nothing to clone or build. Six pods: ingest, worker, Redis,
Postgres, the agent, the MCP server. Point at the ServiceAccount:

```bash
sa=system:serviceaccount:minus20:m20-analyzer
kubectl auth can-i list events --as=$sa -A     # yes
kubectl auth can-i delete pods --as=$sa -A     # no
kubectl auth can-i create pods/exec --as=$sa -A   # no
```

It can look. It cannot touch.

## Scene 3 — break something (1 min)

```bash
kubectl -n minus20 run billing-api --image=busybox --restart=Always --command -- \
  sh -c "echo 'ERROR could not connect to postgres:5432'; sleep 2; exit 1"
kubectl -n minus20 get pod billing-api -w
```

CrashLoopBackOff within a minute. In a real cluster Alertmanager fires
`KubePodCrashLooping` and posts a webhook; here we post the same webhook by
hand so the demo needs no Prometheus:

```bash
kubectl -n minus20 port-forward svc/m20-ingest-api 8080:8080 &
curl -s -X POST localhost:8080/webhook/alertmanager -H 'content-type: application/json' \
  -d @services/ingest-api/tests/fixtures/crashloop.json
```

(Edit the fixture's `namespace` to `minus20` and `pod` to `billing-api`
first, or use a copy. The full autonomous loop with a real PrometheusRule and
Alertmanager is in `docs/LOCAL-SETUP.md`.)

## Scene 4 — the hypothesis arrives (1 min, or 10 s on a cloud model)

Show Telegram. The message carries: likely cause, confidence, evidence
lines, the cheapest way to disprove it, blast radius, next steps, and the
footer: backend, latency, cost, and `#id`.

What to say: this is the reflex path. One model call, hard timeout, daily
budget, PII redacted before the call. If the model is down, the raw alert
still arrives (send another one with the model unreachable to prove it).

## Scene 5 — ask the agent (2–3 min)

In the same chat:

```
/status
why is billing-api crashing?
what changed in minus20 in the last hour?
```

Then send a screenshot of any terminal error with a caption. Then:

```
/wrong <id from the footer> the database was fine, the Secret with credentials was missing
why is billing-api crashing?
```

The second answer quotes the verdict: "the last time this happened the real
cause was…". That memory lives in the customer's Postgres, not with a vendor.

```
/report 30
```

What to say: top causes, night and weekend share, blast radius, how often
the assistant was right according to the engineers. The review a manager
asks for on Monday, generated in seconds.

## Scene 6 — the audit trail (1 min)

```bash
kubectl -n minus20 port-forward svc/m20-web-ui 8090:8080 &
```

Open http://localhost:8090, expand an incident, open "What the model was
shown (redacted)". What did it see when it said that. Regulated teams ask
for exactly this.

## Scene 7 — the same tools from your own agent (optional, 1 min)

```bash
kubectl -n minus20 port-forward svc/m20-mcp 8765:8765 &
```

Add `http://localhost:8765/mcp` to Claude Code or Gemini CLI as an MCP
server and ask "has billing-api crashed before, and what was the real
cause?" The agent calls `search_memory`. Read-only, annotated as such.

## Scene 8 — clean up (30 s)

```bash
kind delete cluster --name minus20
```

## If something does not work

- No message in Telegram: `kubectl -n minus20 logs deploy/m20-analyzer-worker`.
  A `status=analysis_failed` line shows the provider's error, without URLs.
- The agent is silent: `kubectl -n minus20 logs deploy/m20-agent`; the
  chat id must be in the allow-list, and a photo needs the vision model pulled.
- Pods Pending on a cloud cluster: the Postgres PVC needs a volume driver
  (on EKS, the EBS CSI add-on; see `infra/terraform/addons.tf`) **and** a
  StorageClass: EKS's `gp2` is not the default, so `--set postgres.storageClass=gp2`.
