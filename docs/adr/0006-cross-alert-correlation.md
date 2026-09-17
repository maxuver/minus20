# ADR-0006: Correlate different alerts of one namespace into one incident, with one revision

- Status: accepted
- Date: 2026-09-17

## Context

ADR-0001 fixed the shape of the alert path: one alert, one model call, one
message. Storm grouping (2026-09-15) kept that promise when the *same* alert
fires on thirty pods: one leader is analysed, members are counted. What it did
not cover is the other shape of a 03:00 page. A dependency goes away and,
within a minute, the namespace fires a crash loop, a replicas mismatch and an
error-rate alert. Three different alertnames, one fault. The pipeline wrote
three hypotheses and the engineer did the joining. A reader of the first
public post asked exactly this: "when three alerts fire at once at 03:00,
does it correlate them into a single hypothesis, or send three and make you
do the joining?"

Two constraints shape the answer. The first hypothesis must not get slower:
the alert-to-hypothesis time is the number the business asks for, and a
"wait and see what else fires" delay on every alert would pay for correlation
with the common case. And the cost model of ADR-0001 must stay bounded: no
open-ended loop that keeps re-analysing as alerts trickle in.

## Decision

The first alert of a namespace inside `correlation_window_seconds` (default
180) is the **leader** and is analysed immediately, exactly as before. A later
alert of a *different* alertname in that namespace is a **member**:

- its context is collected and redacted like any alert's, and stored with the
  member record (status `correlated`, `grouped_into` = leader id);
- it is not analysed on its own; the chat gets a one-line "attached to #id";
- after `correlation_settle_seconds` (default 20) one **revision** runs: a
  single model call over the leader's stored context plus every attached
  member's, with the instruction to name the one cause that explains all of
  them, or to say which alert it cannot explain. The leader's hypothesis is
  replaced in place; the previous cause is kept as `revised_from`; the chat
  gets one message listing all the alerts and the revised cause.

Storms take precedence: an alert with the leader's own alertname is a storm
member (storm.py) and never reaches correlation. A member of a different
alertname on many pods becomes a storm of its own whose leader is then
attached; the revision sees it once.

State lives in the tracker, Redis when replicas share the stream: the leader's
alert and context, the members' alerts and contexts, and a `SET NX EX` claim
so exactly one replica revises per settle period. The replica that receives a
member can therefore revise even if another replica analysed the leader; the
previous cause comes from the store (`root_cause(id)`), not from memory.

Bounds, in the spirit of ADR-0001 and ADR-0003: at most one revision per
settle period per namespace; a member that arrives after the revision is
attached and listed, not re-analysed; the revision respects the daily budget
and the hard timeout, and on any failure the leader keeps the hypothesis it
had and the attach notices already sent are the fallback. Pending revisions
are drained on graceful stop, inside the pod's grace period.

## Consequences

- Three alerts at 03:00 cost two model calls (leader, revision) instead of
  three, and the engineer reads one hypothesis that was asked to explain all
  of them. The first hypothesis arrives as fast as before.
- Both trackers (storm, correlation) are keyed by namespace and time, not by
  topology. Two unrelated faults in one namespace inside three minutes are
  attached to each other; the prompt tells the model to say when an alert is
  unexplained, and the previous cause is on record. Topology-aware
  correlation (owner references, service graph) is a later step, not this one.
- The audit trail holds the revision prompt as the leader's `context`: exactly
  what the model saw when it revised. `/scenario` on a revised leader exports
  that combined context.
- The incident count in `/report` keeps every alert as a row (members included,
  status `correlated`), and reports how many hypotheses were revised, so
  "how many incidents" and "how many alerts" stay distinguishable.
- One exception to "one call per alert" now exists and is named. It is the
  only write that changes a hypothesis after delivery.

## Rejected

- **Delaying every first analysis** by a settle window to batch alerts up
  front: pays for correlation with the common single-alert case; the
  alert-to-hypothesis metric would move from ~40 s to ~60 s on every incident.
- **Re-analysing on every attached alert**: unbounded model calls during a
  cascade; the settle period and the single claim bound it.
- **A separate "meta-incident" row** for the revision: doubles the rows the
  report counts and splits the verdict (`/ok`, `/wrong`) between two ids. The
  leader is revised in place and keeps its id, so the engineer's verdict lands
  on the hypothesis they actually read last.
