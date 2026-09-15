# SentinelOps — Vision

> Written 2026-07-28, before a line of code. Kept as the statement of intent.
> What exists today, and how it is built, is in [ARCHITECTURE.md](ARCHITECTURE.md);
> the decisions since are in [adr/](adr/). Where this document and those disagree,
> the code and the ADRs are right. Section 8 lists what changed.

## 1. The pain

The most expensive part of an incident is not the fix — it is the first 20–30 minutes of
not knowing what is going on. An alert fires; the notification contains a bare
Alertmanager template. The on-call engineer manually assembles context from metrics,
pod/node status, events and recent releases. While that search is running, nobody can
tell the business what happened. Understanding arrives in 10–20 minutes; ten minutes
later the incident is often already fixed — but for the business it looked like 30 lost
minutes. This window is what MTTA (mean time to acknowledge/assess) measures, and it is
the window SentinelOps attacks.

The dashboards are usually excellent. Grafana, runbooks, on-call rotation — all
exemplary. But dashboards help *after* the incident, not *during* it: monitoring knows
**what** is burning; **what it means** is still figured out by a human, by hand, every
single time. That work is repetitive, pattern-shaped, and largely automatable.

There is a second pain: the asymmetry of knowledge. Seniors know how to investigate.
A junior on call either drowns or escalates everything, waking up expensive people.
Bus factor, burnout, churn.

## 2. The solution

SentinelOps automates the first minutes of every incident with an event-driven pipeline:

1. **Alertmanager** fires a webhook on every alert.
2. **ingest-api** validates it and publishes it to a Redis Stream. The raw alert is
   forwarded to Telegram *immediately* — analysis must never delay delivery.
3. **analyzer-worker** consumes the stream and gathers the context an engineer would
   gather by hand: relevant Loki logs, Prometheus metrics around the alert timestamp,
   Kubernetes events and object status.
4. The context bundle is **redacted and minimized**, then sent to an LLM with a
   structured prompt. The output: probable root cause, severity assessment,
   recommended next steps.
5. The hypothesis lands in **Postgres** (incident history — a growing dataset) and in
   **Telegram** as a follow-up to the raw alert.

The engineer receives a pre-investigated incident. What used to take 30 minutes by hand
takes the pipeline seconds.

**Human-in-the-loop by design.** The agent prepares context and a hypothesis; the
decision is made by the engineer — who is also the one held accountable. The system
recommends, never remediates.

## 3. Why now, and industry validation

- LLMs in the Haiku class became cheap enough that analysing *every* alert costs cents.
  A year earlier the economics did not work.
- The pattern is validated by the ecosystem: **k8sgpt** and **HolmesGPT** are CNCF
  Sandbox projects in exactly this niche; cloud providers are embedding alert-triage
  copilots into their own consoles.
- The same architecture is being built in production teams: a July 2026 community talk
  ("teaching AI to dig through metrics while the on-call finishes their tea")
  demonstrated a production deployment of this exact shape — enricher + AI worker on
  top of Alertmanager, Telegram delivery, human decision-making — and its author's
  conclusion was that what took him six months to build is now a matter of days with
  current tooling. The value is in the approach: hand the *confirmation routine* to an
  agent, keep the *decision* with the engineer.

We are not guessing at a trend; we are entering a confirmed one.

## 4. Positioning against prior art

|                        | k8sgpt                  | HolmesGPT                     | AWS DevOps Agent (2026)            | SentinelOps                             |
|------------------------|-------------------------|-------------------------------|------------------------------------|-----------------------------------------|
| Model of operation     | on-demand cluster scan  | autonomous agent loop         | managed autonomous agent           | event-driven reflex + bounded on-demand agent |
| Context                | K8s resources           | many toolsets                 | account topology, telemetry, code  | logs + metrics + events around the alert; memory of past incidents |
| History                | no                      | partially (SaaS)              | in AWS                             | Postgres in your cluster — incident dataset with engineer verdicts |
| Cost per alert         | —                       | unpredictable (agent iterates) | $0.0083 per agent-second          | deterministic pipeline → known in advance; $0 with a local model |
| Data leaves the cluster | no                     | to the model vendor           | to CloudWatch/S3/Bedrock           | never, with the local backend |
| Scope                  | CLI tool                | agent                         | AWS + integrations, 6 regions      | any Kubernetes, one `helm install` |

The choice of a deterministic pipeline over an agent loop is a deliberate trade-off:
predictable cost, bounded latency, auditable behaviour
([ADR-0001](adr/0001-event-driven-pipeline-over-agent-loop.md)).

## 5. Data privacy (a first-class requirement)

Alert context contains **other people's data**: logs carry emails, IPs, tokens, request
bodies. Shipping that to a third-party cloud API is a data-governance decision, not a
technical detail — under GDPR it can make the LLM vendor a data processor.
SentinelOps treats this as a design constraint, not an afterthought:

- **Redaction before any LLM call** — emails, IP addresses, bearer/JWT tokens, cloud
  credentials and secret-shaped strings are masked in the collector output.
- **Data minimization** — hard caps on log lines and context size; only the window
  around the alert timestamp is collected.
- **Pluggable LLM backend** — `ollama` (fully local), any OpenAI-compatible API
  (DeepSeek, Groq, Gemini, vLLM), or `anthropic`. A data-sovereign deployment
  sends nothing outside the cluster and costs $0 per alert.
- **Redacted-only persistence** with a configurable retention TTL.

Details: [ADR-0002](adr/0002-llm-privacy-and-pluggable-backends.md).

## 6. What we measure (success criteria)

A library of fault-injection scenarios (CrashLoopBackOff, OOMKill, DNS failure,
ImagePullBackOff, full PVC, dead dependency) is part of the project. Against it we
measure, reproducibly:

- **Time-to-first-hypothesis** — target < 60 s, vs ~10–30 min of manual triage.
- **Hypothesis quality** per scenario (correct / partially correct / wrong).
- **Cost per analysed alert** in cents, per backend.
- **Degradation behaviour** — raw alert delivery latency with the LLM backend down.

These numbers are the project's résumé: each one can be demonstrated live.

## 7. Non-goals

- **No auto-remediation.** Recommendations only. (The talk cited above put it best:
  the agent drafts, the engineer decides, the engineer gets the reprimand.)
- **No shell for the model.** The agent's tools are a closed, read-only set; a general
  agent harness with shell access is not something to run inside someone's cluster.
- **No multi-cluster federation** in scope for v1.
- **No pre-deploy / release-readiness review.** Post-deploy operations only.

## 8. What changed since this was written (2026-09-13)

- The reflex pipeline shipped as described. An **on-demand agent** was added as a
  second, separate process (ADR-0005): the agent-loop trade-off in §4 still holds
  for the alert path, and the agent is bounded (tool calls, time, budget) and
  holds no tool that can change anything.
- **Memory** of past incidents with the engineer's verdict (pgvector, local
  embeddings) and a weekly **incident review** (`/report`) were added; the
  review format came from a real incident report for a production API.
- The same tools are served over **MCP** to external agent CLIs.
- **AWS DevOps Agent** (GA April 2026) confirmed the category with the same
  pitch ("always-on autonomous on-call engineer"). SentinelOps is positioned
  for where it cannot be used: data that must stay in the cluster, clusters not
  on AWS, teams that need the price known in advance, open code.
- Terraform for EKS was **applied for real** once and destroyed ([EKS-RUN.md](EKS-RUN.md)).
- Measured accuracy is published, including where it fails ([BENCHMARKS.md](BENCHMARKS.md)).

## 9. Where this goes next (2026-09-15)

The final shape is unchanged: two always-on processes inside the customer's
cluster (the reflex on the alert path, the deliberate agent on demand), a
memory of incidents with the engineer's verdicts, a weekly review for the
people who ask "how many, and why", and the same tools served over MCP. It
is a Helm release, not a script on someone's laptop, and it must keep working
when nobody is watching. In that order, the next steps:

1. **Verdicts become the eval set.** `/wrong <id> <cause>` already turns an
   incident into a replay scenario (`/scenario`). The next step is grading the
   current model against every verdict in the store, so the benchmark grows
   from real incidents instead of hand-written fixtures.
2. **Correlation, not just storms.** Same alert on many pods is already one
   incident and one model call. Different alerts on one namespace inside one
   window (a crash loop, a replicas mismatch and an error-rate alert at 03:00)
   should also be one incident with one hypothesis. That is the alert-fatigue
   pain in one sentence.
3. **A cloud tier with a known price and a known jurisdiction.** Gemini's free
   tier proved too throttled for anything sustained. The OpenAI-compatible
   adapter makes the provider a config value; the candidates are compared in
   [BENCHMARKS.md](BENCHMARKS.md) as they are measured.
4. **AWS-shaped scenarios** (spot interruption storms, Pod Identity denied,
   EBS volume stuck on a replaced node) for the benchmark, because that is
   where the comparison with AWS DevOps Agent will be made.
5. **Unattended operation:** a demo cluster that runs for weeks without a
   laptop, breaks itself on a schedule, and sends its own weekly review.

Still non-goals: auto-remediation, a shell for the model, and anything that
"repels" attacks. Traffic anomalies at night are an explanation problem for
this project (attack, deploy or bug, with evidence); mitigation belongs to the
edge (WAF, Shield, rate limits), not to a model inside the cluster.
