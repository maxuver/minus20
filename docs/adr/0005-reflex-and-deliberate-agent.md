# ADR-0005: Two always-on processes: a reflex pipeline and a deliberate agent

- Status: accepted
- Date: 2026-09-11

## Context

ADR-0001 chose a deterministic pipeline over an agent loop for the alert path,
and it was right: every alert costs one LLM call, finishes inside a hard
timeout, and is auditable. The benchmark (docs/BENCHMARKS.md) then showed the
limit of that shape. On scenarios where the obvious signal points the wrong
way, a single call reasons by elimination badly (2/5), even though the
disproving evidence was in the context. What the engineer does next in those
cases is exactly what a single call cannot: ask a follow-up question, pull one
more piece of context, compare with the last time this happened.

Two things are also missing that a fixed pipeline cannot supply on its own:

1. **Memory of this cluster.** The third time a NetworkPolicy blocks DNS, the
   right answer is "same as 12 August". No amount of prompt design gives a model
   that; only the team's own incident history does.
2. **Reporting.** An incident-review document written by hand for one
   production API (per-cause aggregation, night/weekend concentration,
   availability against an SLA, error budget, P0/P1/P2 recommendations) took
   days to assemble. Every input to it is already in the `incidents` table.

The tempting shortcut is to adopt a general-purpose agent harness (Hermes
Agent, OpenClaw) and point it at the cluster. Those are personal assistants
with shell, filesystem and browser tools. Running one inside a customer's
cluster contradicts the promise that makes this product deployable at all:
*it never acts on your cluster*. A single screenshot of an agent running
`kubectl delete` would end the conversation with every buyer.

## Decision

Two long-running processes, both shipped in the Helm chart, with a hard
boundary between them.

```
                    ┌──────────── always on ────────────┐
Alertmanager ──► ingest-api ──► Redis Stream ──► analyzer-worker      REFLEX
                                                      │               one LLM call, hard timeout,
                                                      │               daily budget, no loop
                                                      ├─► Postgres ◄──────────────┐
                                                      └─► Slack / Telegram        │
                                                                                  │
Engineer in chat ──► minus20-agent                                   DELIBERATE  │
   "why did billing-api crash?"     tool-calling loop, on demand ─────────────────┘
   "what changed in the hour before?"   tools: read-only collectors only
   "/report week"                       memory: past incidents + resolutions + runbooks
   "wrong, it was X"                    feedback is written back and becomes memory
```

**Reflex** is the existing analyzer-worker, unchanged in contract. It reacts to
every alert, bounded in time and cost, and is the only thing the alert path
depends on.

**Deliberate** is a new service, `minus20-agent`. It lives in the team's chat
and runs only when a human asks. It is a small, purpose-built loop, not a
general harness:

- **Tools are the existing collectors**, exposed to the model as functions. The
  set is closed and every tool is read-only by construction: Kubernetes
  events, PromQL, LogQL, deploy history, incident memory. There is no shell,
  no `kubectl`, no filesystem. A prompt injection in a log line can make the
  agent say something wrong; it cannot make it do anything.
- **Memory is retrieval over the team's own data** (RAG): past incidents with
  their hypothesis, evidence and, crucially, the engineer's resolution;
  runbooks and post-mortems the team chooses to index. Embeddings are computed
  locally (Ollama) so memory obeys the same zero-egress rule as analysis
  (ADR-0002). Storage is pgvector in the Postgres the chart already deploys.
- **Feedback is a product feature.** "Correct" / "wrong, it was X" from chat
  is stored on the incident and indexed. The system's accuracy on *this*
  cluster improves with use, which is the one thing a competitor cannot copy
  by reading this repository.
- **Reports are a command.** `/report week` aggregates the incident table into
  the review format an engineering manager actually reads: top causes, time
  of day and day of week, availability against the SLO, remaining error
  budget, recommendations. Generated in seconds from data already collected.
- **Every loop is bounded.** Maximum tool calls per question, wall-clock
  timeout, and a per-day budget separate from the reflex budget. A human is in
  the loop and can stop it; the reflex has no such luxury, which is why it has
  no loop.

The model behind both processes is selected by the same `LLMBackend` port. An
OpenAI-compatible adapter covers DeepSeek, Groq, Together, OpenRouter, vLLM
and LM Studio through one implementation; local Ollama stays the default.
Reasoning models are not used on CPU: measured, they time out.

## Consequences

- The alert path keeps every guarantee of ADR-0001 and ADR-0003: if the agent
  is down, slow or over budget, notifications still arrive. The agent reads the
  same Postgres and writes only human-supplied resolutions.
- The failure class the benchmark exposed (reasoning by elimination) gets the
  two things that actually fix it: a follow-up loop with a human, and memory of
  similar past incidents. Prompt work on the reflex continues, but it is no
  longer the only lever.
- The chart grows one Deployment and swaps the Postgres image for one with
  pgvector. Nothing else changes for a user who does not enable the agent.
- Trade-off accepted: a hand-built loop has fewer features than Hermes Agent
  or OpenClaw. It also has no shell, no plugin marketplace and no surprise
  capabilities, which is the point. Those harnesses remain useful as personal
  tools for the engineer, outside the cluster.
- The agent's answers are still hypotheses. Every one carries evidence and a
  disproof (ADR-0004), and the engineer decides.
