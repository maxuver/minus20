# ADR-0001: Event-driven pipeline over an autonomous agent loop

- Status: accepted
- Date: 2026-07-28

## Context

Two dominant patterns exist for AI-assisted Kubernetes diagnostics:

1. **On-demand scan** (k8sgpt): a CLI walks cluster resources through analyzers and
   asks an LLM to explain findings. No reaction to live alerts.
2. **Autonomous agent loop** (HolmesGPT): the LLM decides which tools to call,
   iterates, refines. Powerful, but the number of LLM round-trips — and therefore
   cost and latency — is unbounded and varies per incident.

Minus20 reacts to production alerts on a budget measured in cents, and its output
must be explainable to an on-call engineer who has to trust it at 3 AM.

## Decision

A deterministic event-driven pipeline: webhook → queue → context collectors →
**one structured LLM call** → delivery. The set of collectors (Loki logs, Prometheus
metrics, K8s events) is fixed in code per alert type, not chosen by the model at
runtime.

## Consequences

- Cost and latency per alert are known in advance and easy to cap.
- The pipeline is auditable: for any incident, the exact context sent to the model is
  recorded; behaviour is reproducible in tests and benchmarks.
- Trade-off: less adaptive investigation depth than an agent loop. A fixed context
  bundle covers the standard triage ritual, which is the 80% case this project targets.
- An optional tool-use step can be added later behind a feature flag without changing
  the core contract.
