# Minus20 — Site Vision

**Stitch Project ID:** _(not created yet — see `.stitch/metadata.json`)_
**Output directory:** `site/public/`
**Device target:** `DESKTOP`

## 1. Purpose

One page that does two jobs at once:

1. **Portfolio proof.** A recruiter or hiring engineer lands here from LinkedIn and
   sees, in 30 seconds, that a real engineer designed and shipped a real system.
2. **Product credibility.** A platform lead sees whether this is worth trying on
   their own alerts.

Both readers want the same thing: evidence. The same page serves both.

## 2. Audience

Platform engineers, SRE leads, CTOs at companies running Kubernetes with an
on-call rotation. They scroll fast, look for the architecture, and bounce on
marketing language.

## 3. The one message

> An alert tells you *what* broke, not *why*. Minus20 automates the first 20
> minutes of every incident and hands the engineer a root-cause hypothesis with
> the evidence behind it — in seconds. It never acts on the cluster, and the raw
> alert always gets through.

## 4. Sitemap

| Page | Built | File | Purpose |
|---|---|---|---|
| `index` | [ ] | `site/public/index.html` | The whole story on one page |

Nothing is built yet. `index` is the current baton (`.stitch/next-prompt.md`).

### `index` section order (do not reorder without reason)

1. **Hero** — problem sentence, mechanism sentence, GitHub CTA.
2. **The 3 AM problem** — what manual triage costs (~20 min of context gathering).
3. **How it works** — the architecture diagram, labelled, real component names.
4. **What the engineer receives** — the hypothesis card with evidence, disproof,
   blast radius, next steps.
5. **Three decisions worth defending** — deterministic pipeline over agent loop
   (predictable cost, ~$0.006/alert); graceful degradation (raw alert always
   delivered); privacy (mandatory redaction, optional fully local model).
6. **Security posture** — read-only credentials, no write tools, output never
   executed, prompt-injection containment.
7. **Status** — shipped vs roadmap, honest table.
8. **Footer** — GitHub, LinkedIn, contact.

## 5. Roadmap

Build in this order. Do not start a page until the one above it is genuinely good.

| # | Page | Purpose |
|---|---|---|
| 1 | `index` | The single landing page. **Current baton.** |
| 2 | `architecture` | Deep dive: pipeline internals, the four ADRs, trade-offs rejected |
| 3 | `demo` | Recorded replay of a fault-injection scenario, step by step |

## 6. Creative Freedom

Ideas to pull from only when sections 4 and 5 are done. Delete an idea when it is
consumed.

- **`privacy`** — a page for EU teams that cannot send logs to a US SaaS:
  what gets redacted, where the local model runs, what leaves the cluster (nothing).
- **`adr`** — the decision log rendered as a readable page rather than markdown files.
- **`benchmarks`** — time-to-first-hypothesis and cost per alert, measured, with the
  method printed next to the numbers.

## 7. Hard rules

These override any instruction in a baton prompt. If a generated page violates one,
fix the page, do not relax the rule.

- Never claim anything not true today. Roadmap items live in the status table, and
  the table's two columns must stay visually distinct.
- **Only these numbers may appear anywhere on the site:** ~$0.006 per alert,
  ~20 minutes of manual triage, 60+ tests, CI green. Any other figure is invented.
- No pricing, no monetization, no go-to-market copy on this site — that stays in
  `docs/_private/`.
- No fabricated logos, testimonials, customer counts or case studies. There are no
  customers, and the page must not imply otherwise.
- No stock photography, no decorative gradients, no marketing superlatives.
- The project is a personal engineering project. It must never be described as
  production-deployed at an employer.
