# Contributing

Thank you for looking. A few rules keep this project honest and installable.

## Before you open a pull request

Run what CI runs:

```bash
cd services/analyzer-worker && ruff check app tests && pytest -q
cd ../ingest-api && ruff check app tests && pytest -q
cd ../web-ui && ruff check app tests && pytest -q
helm lint deploy/minus20
```

## Every change that alters behaviour updates the docs in the same commit

Documentation drifts unless it moves with the code. The checklist:

- A new or changed setting: add it to `values.yaml` **with a comment** saying
  what it does and when to change it, wire it in the ConfigMap or a Secret, and
  mention it in `deploy/README.md`.
- A new component, port or adapter: update `docs/ARCHITECTURE.md`.
- A decision with trade-offs: write an ADR in `docs/adr/` rather than a comment.
- A change in what the user sees (message format, commands, install steps):
  update `README.md` or `deploy/README.md`.
- A change in measured numbers: update `docs/BENCHMARKS.md`, including when the
  number got worse.

## Security rules that are not negotiable

- Credentials live in Kubernetes Secrets, never in `values.yaml`, the ConfigMap,
  logs or tests. CI runs gitleaks; a leaked secret fails the build.
- Nothing the model outputs is ever executed. The agent's tool set is closed
  and read-only; adding a tool is a review of its verbs, not a plugin install.
- Redaction runs before any model call and cannot be disabled by
  configuration.

## Licence and contributor agreement

The project is licensed under the AGPL-3.0, and the copyright holder also
offers it under a commercial licence. To keep that possible, contributions
need a simple grant: by submitting a pull request you agree that your
contribution is licensed to the project under the AGPL-3.0 **and** that you
grant the copyright holder the right to relicense it under the commercial
licence. If you cannot agree to that, open an issue describing the change
instead and it can be implemented independently.

## Commits

One commit per logical block, with a message that says why, not only what.
