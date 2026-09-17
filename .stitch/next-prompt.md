---
page: index
---
The single landing page for Minus20, an AI-assisted incident-triage layer for
Kubernetes. Audience: platform engineers, SRE leads and CTOs who run Kubernetes
with an on-call rotation. They scroll fast, look for the architecture, and bounce
on marketing language.

**DESIGN SYSTEM (REQUIRED):**
Dark technical aesthetic for a developer infrastructure tool.
Background #0A0C10, surfaces #12151C, hairline borders #232833.
Text #E6E9EF primary, #9AA4B2 secondary. Single accent #4C8DFF used sparingly.
Status colors: #3FBF7F ok, #F0A93B warning, #E5534B danger.
Typography: Inter for UI, JetBrains Mono for code and numbers.
Generous whitespace, max width 1150px, 8px spacing grid.
Restrained motion only: subtle fade-and-rise on scroll, no parallax, no 3D.
Feels like Linear, Vercel or Grafana docs — precise, fast, understated.
No stock photography, no gradients-as-decoration, no marketing superlatives.

**Page Structure:**
1. Hero. Headline: "An alert tells you what broke. Not why." Subline: Minus20
   collects the context an engineer would gather by hand and returns a ranked
   root-cause hypothesis in seconds. Primary CTA "View on GitHub", secondary
   "How it works".
2. The 3 AM problem. Short section on the ~20 minutes of manual context gathering
   that starts every incident: logs, metrics, recent deploys, correlated by hand.
3. How it works. A labelled architecture diagram of the real pipeline:
   Alertmanager → ingest-api → Redis Streams → analyzer-worker, with three
   collectors (Loki logs, Prometheus metrics, Kubernetes events), then mandatory
   PII redaction, then a single structured LLM call, then delivery and storage.
4. What the engineer receives. A realistic hypothesis card in monospace showing:
   root cause, confidence, blast radius, evidence bullets, "cheapest way to
   disprove", numbered next steps, and a footer line with backend, latency and
   cost.
5. Three decisions worth defending, as three cards: deterministic pipeline over an
   agent loop (predictable ~$0.006 per alert); graceful degradation (the raw alert
   is always delivered, even when the model is down); privacy (non-bypassable PII
   redaction, optional fully local model with zero egress).
6. Security posture. Read-only credentials on every integration, the model holds
   no write-capable tool, its output is never executed, so prompt injection through
   logs is contained to a wrong suggestion rather than an action in the cluster.
7. Status table. Two honest columns, shipped versus roadmap.
8. Footer. GitHub, LinkedIn, contact.

**Constraints:**
- Do not invent customers, logos, testimonials or metrics.
- Only these numbers may appear: ~$0.006 per alert, ~20 minutes of manual triage,
  60+ tests, CI green.
- No pricing or monetization copy anywhere on the page.
