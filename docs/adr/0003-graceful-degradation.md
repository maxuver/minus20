# ADR-0003: Graceful degradation — AI must never block alert delivery

- Status: accepted
- Date: 2026-07-28

## Context

An incident-triage system sits on the critical path of on-call trust. If AI analysis
delayed or — worse — swallowed alerts when the LLM is down, slow or over budget, the
team would rightfully rip it out. Production experience shared by teams running this
pattern is unambiguous: the raw technical alert must reach the engineer no matter
what; AI is an overlay, not a point of failure.

## Decision

1. **Raw alert first.** The notifier forwards the raw Alertmanager payload to
   Telegram immediately on ingest, before analysis starts. The LLM hypothesis
   arrives as a follow-up message referencing the original.
2. **Analysis is best-effort.** LLM calls have a hard timeout and a per-day budget
   cap. On failure the incident record is stored without a hypothesis and marked
   `analysis_failed` — never retried into a pile-up.
3. **Queue isolation.** ingest-api only validates and enqueues (Redis Streams).
   A slow or dead worker never back-pressures webhook ingestion. Poison messages
   go to a dead-letter stream after N delivery attempts.

## Consequences

- The pipeline is strictly additive to existing alerting: switching Minus20 off
  returns the team to exactly what they had before.
- Alert delivery latency is decoupled from LLM latency and measurable separately.
- Requires two Telegram messages per incident (raw + analysis) — accepted, it also
  gives the engineer a head start on reading the raw alert.
