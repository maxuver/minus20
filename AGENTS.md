# AGENTS.md

Instructions for any AI coding agent (Claude Code, Codex, Gemini CLI, Cursor)
and any human contributor working in this repository. Read this before
changing code. The reasons behind each rule are in the ADRs it cites.

## What this is

Minus20: AI incident triage for Kubernetes. An Alertmanager alert goes in;
the context an engineer would gather by hand is collected inside the
cluster, redacted, and one bounded model call returns a falsifiable
hypothesis (cause, evidence, cheapest disproof, blast radius) to Slack or
Telegram. A separate agent answers follow-up questions with read-only tools
and remembers the engineer's verdicts. The same tools are served over MCP.
Map of the code and the data flow: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Non-negotiable invariants

Breaking one of these is an architecture change: it needs a new ADR and the
owner's explicit approval, not a refactor.

1. **The raw alert is delivered when the AI fails.** Model down, over
   budget, timed out or wrong: the engineer still gets the alert
   (ADR-0003). The AI layer is never a dependency of alert delivery.
2. **One bounded model call per alert on the alert path.** Hard timeout,
   daily budget. The only exception is the correlation revision, itself
   bounded to one per settle period per namespace (ADR-0001, ADR-0006).
   No agent loop on the alert path.
3. **Redaction runs before every model call and has no off switch**
   (ADR-0002). New collectors, new prompts, paste mode, revisions: all
   pass through the same redaction.
4. **Read-only.** The product's ServiceAccount has no write verb, no exec,
   no secrets. Model output is text and is never executed. Next steps that
   would change the cluster are marked `REQUIRES APPROVAL:` by code, not
   only by the prompt (`app/models.py`). The demo chaos job is the single
   write-capable component, with its own ServiceAccount, off by default.
5. **Every hypothesis carries its evidence and its cheapest disproof**
   (ADR-0004). A cause without evidence is not a valid output.
6. **The audit trail is exact.** The redacted context the model saw is
   stored with the incident; a revision stores the revision prompt.
7. **No credentials in code, values, ConfigMaps, logs or chat.** Secrets
   come from Kubernetes Secrets created by the operator. Logs pass through
   `app/logsafe.py`.
8. **Claims are measured.** Do not claim a change improves accuracy,
   latency, cost or reliability without a number. A prompt change reruns
   `python -m app.replay scenarios/hard` and reports misses, not only the
   total ([docs/BENCHMARKS.md](docs/BENCHMARKS.md)).

## How to change things

- **Read first.** Find the existing pattern (ports in `app/ports.py`,
  adapters selected by configuration) and follow it. A new provider,
  collector or notifier is an adapter plus a config value, not a branch in
  the pipeline.
- **Plan before architecture.** For anything touching the alert path,
  storage schema, RBAC, the chart's security model or the prompt: write the
  plan (and an ADR if an invariant moves) before the code.
- **Tests are the correctness claims.** Each `CC-nn` in a test docstring is
  a promise; add one for every new behaviour. `ruff check app tests` and
  `pytest` must pass in each service you touch.
- **Verify live, not only in tests.** A feature that talks to Kubernetes,
  Telegram or a model provider is run once on a real cluster before it is
  called done. Confirm the running image contains your change
  (`docker image inspect -f '{{.Created}}'`, or grep inside the pod).
- **Never test a chat bot with multi-line input against a real owner's
  chat on an unverified build.**
- **Docs move with the code.** New behaviour: README (status table or
  quickstart), `deploy/README.md` (how to turn it on), `values.yaml`
  (commented), ADR if a decision was made. One logical block per commit.
- **Never weaken a security or reliability guarantee to make something
  easier.** If a requirement seems to need it, stop and ask.

## Commands

```bash
cd services/analyzer-worker && ruff check app tests && pytest -q
helm lint deploy/minus20
python -m app.replay scenarios/hard          # benchmark (needs a model)
python -m app.replay --from-store            # grade against engineer verdicts
```

## Out of scope

Auto-remediation in the open version; a shell or write tools for any model;
pre-deploy review; anything that "fights" attacks (explaining an anomaly is
in scope, mitigating it is not).
