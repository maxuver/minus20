# Minus20 — Design System

Audience: platform engineers, SRE leads, CTOs. They distrust marketing polish and
trust evidence. The page must feel like a well-built tool, not a campaign.

## 1. Principles

1. **Evidence over adjectives.** Real numbers, a real architecture diagram, a real
   hypothesis output. No "revolutionary", no stock imagery.
2. **Fast and quiet.** Restrained motion. No heavy 3D or parallax — for this
   audience it reads as marketing covering for a thin product.
3. **Dark-first.** This is a 3 AM on-call tool. Dark is the native mode.
4. **Honest status.** Ship-state and roadmap are visibly separated.

## 2. Color

| Token | Value | Use |
|---|---|---|
| `--bg` | `#0A0C10` | page background |
| `--surface` | `#12151C` | cards, panels |
| `--border` | `#232833` | hairlines, card edges |
| `--text` | `#E6E9EF` | primary text |
| `--text-dim` | `#9AA4B2` | secondary text |
| `--accent` | `#4C8DFF` | links, primary CTA, active states |
| `--warn` | `#F0A93B` | alert/warning states in demos |
| `--ok` | `#3FBF7F` | healthy states, checkmarks |
| `--danger` | `#E5534B` | failure states in demos |

Accent is used sparingly: one primary CTA per viewport, plus data highlights.

## 3. Typography

- **UI/body:** Inter, fallback `system-ui, -apple-system, sans-serif`
- **Code/data:** JetBrains Mono, fallback `ui-monospace, monospace`
- Scale: hero `clamp(2.5rem, 6vw, 4.5rem)` / h2 `2rem` / h3 `1.25rem` /
  body `1rem` / caption `0.875rem`
- Line height: 1.15 headings, 1.65 body. Max text width `68ch`.

## 4. Layout & motion

- Max content width `1150px`, section padding `clamp(4rem, 10vh, 7rem)`.
- 8px spacing grid.
- Motion: fade + 12px rise on scroll-in, 250ms, `ease-out`, staggered 60ms.
  Respect `prefers-reduced-motion` — disable all transforms.
- No scroll-jacking. No 3D parallax. Hover states are 120ms.

## 5. Components

- **Hero** — one sentence on the problem, one on the mechanism, primary CTA
  (GitHub), secondary (see how it works).
- **Architecture diagram** — inline SVG, the real pipeline:
  Alertmanager → ingest-api → Redis Streams → analyzer-worker → (Loki, Prometheus,
  K8s events) → redaction → LLM → incident. Must render in light and dark.
- **Hypothesis card** — a real output: root cause, evidence, "cheapest way to
  disprove", blast radius, next steps. This is the money component.
- **Metrics row** — ~$0.006/alert · seconds vs ~20 min manual · 60+ tests · CI green.
- **Design-decision cards** — three ADR summaries: deterministic over agent,
  graceful degradation, redaction/local model.
- **Status table** — shipped vs roadmap, honestly split.

## 6. DESIGN SYSTEM BLOCK (copy into every Stitch prompt)

```
Dark technical aesthetic for a developer infrastructure tool.
Background #0A0C10, surfaces #12151C, hairline borders #232833.
Text #E6E9EF primary, #9AA4B2 secondary. Single accent #4C8DFF used sparingly.
Status colors: #3FBF7F ok, #F0A93B warning, #E5534B danger.
Typography: Inter for UI, JetBrains Mono for code and numbers.
Generous whitespace, max width 1150px, 8px spacing grid.
Restrained motion only: subtle fade-and-rise on scroll, no parallax, no 3D.
Feels like Linear, Vercel or Grafana docs — precise, fast, understated.
No stock photography, no gradients-as-decoration, no marketing superlatives.
```
