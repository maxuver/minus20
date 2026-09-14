"""Incident review report, generated from the incident history (ADR-0005).

Modelled on the review document an engineering manager actually reads: which
causes dominate, when incidents happen (night, weekend), where they happen,
how often the assistant was right, and what to do about it. Assembling that
by hand for one production API took days; every input to it is already in
the `incidents` table.

The numbers are computed in SQL and rendered by code, so they are always
right. The model contributes only the closing section (observations and
P0/P1/P2 recommendations) and is handed the aggregates as data with an
instruction to use no other figures. If the model is unavailable, the report
ships without that section (ADR-0003).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from ..ports import BackendError
from .chat import ChatBackend

logger = logging.getLogger("sentinelops.agent.report")

NIGHT_HOURS = set(range(7)) | {22, 23}  # 22:00–06:59 UTC

_TOTALS = """
SELECT count(*)                                              AS total,
       count(*) FILTER (WHERE status = 'analyzed')           AS analyzed,
       count(*) FILTER (WHERE status = 'analysis_failed')    AS failed,
       count(*) FILTER (WHERE status = 'budget_exceeded')    AS over_budget,
       count(*) FILTER (WHERE severity = 'critical')         AS critical,
       count(*) FILTER (WHERE verdict = 'correct')           AS confirmed,
       count(*) FILTER (WHERE verdict = 'wrong')             AS refuted,
       coalesce(sum(cost_usd), 0)                            AS cost_usd,
       coalesce(avg(latency_ms) FILTER (WHERE status = 'analyzed'), 0) AS avg_latency_ms,
       coalesce(avg(time_to_hypothesis_ms) FILTER (WHERE status = 'analyzed'), 0) AS avg_ttfh_ms,
       coalesce(percentile_cont(0.95) WITHIN GROUP (ORDER BY time_to_hypothesis_ms)
                FILTER (WHERE status = 'analyzed' AND time_to_hypothesis_ms IS NOT NULL), 0) AS p95_ttfh_ms
FROM incidents WHERE created_at >= $1 AND created_at < $2
"""

_BY_CAUSE = """
SELECT coalesce(resolution, root_cause) AS cause, count(*) AS n
FROM incidents
WHERE created_at >= $1 AND created_at < $2 AND coalesce(resolution, root_cause) IS NOT NULL
  AND backend <> 'stub'  -- placeholders are not causes
GROUP BY 1 ORDER BY n DESC LIMIT 5
"""

_BY_NAMESPACE = """
SELECT coalesce(nullif(namespace, ''), '(none)') AS namespace, count(*) AS n
FROM incidents WHERE created_at >= $1 AND created_at < $2
GROUP BY 1 ORDER BY n DESC LIMIT 5
"""

_BY_ALERT = """
SELECT alertname, count(*) AS n
FROM incidents WHERE created_at >= $1 AND created_at < $2
GROUP BY 1 ORDER BY n DESC LIMIT 5
"""

_BY_HOUR = """
SELECT extract(hour FROM created_at AT TIME ZONE 'UTC')::int AS hour, count(*) AS n
FROM incidents WHERE created_at >= $1 AND created_at < $2
GROUP BY 1
"""

_BY_DOW = """
SELECT extract(isodow FROM created_at AT TIME ZONE 'UTC')::int AS dow, count(*) AS n
FROM incidents WHERE created_at >= $1 AND created_at < $2
GROUP BY 1
"""

_BLAST = """
SELECT coalesce(blast_radius, 'unknown') AS blast_radius, count(*) AS n
FROM incidents WHERE created_at >= $1 AND created_at < $2
GROUP BY 1 ORDER BY n DESC
"""


@dataclass
class ReportData:
    period_start: datetime
    period_end: datetime
    totals: dict[str, Any] = field(default_factory=dict)
    by_cause: list[tuple[str, int]] = field(default_factory=list)
    by_namespace: list[tuple[str, int]] = field(default_factory=list)
    by_alert: list[tuple[str, int]] = field(default_factory=list)
    by_hour: dict[int, int] = field(default_factory=dict)
    by_dow: dict[int, int] = field(default_factory=dict)
    blast: list[tuple[str, int]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return int(self.totals.get("total") or 0)

    @property
    def night_count(self) -> int:
        return sum(n for h, n in self.by_hour.items() if h in NIGHT_HOURS)

    @property
    def weekend_count(self) -> int:
        return sum(n for d, n in self.by_dow.items() if d >= 6)

    def as_json(self) -> str:
        return json.dumps(
            {
                "period": [self.period_start.date().isoformat(), self.period_end.date().isoformat()],
                "totals": {k: (float(v) if isinstance(v, float) else v) for k, v in self.totals.items()},
                # Spelled out so the model cannot read "0 refuted" as "all correct".
                "engineer_reviewed": int(self.totals.get("confirmed") or 0)
                + int(self.totals.get("refuted") or 0),
                "top_causes": self.by_cause,
                "top_namespaces": self.by_namespace,
                "top_alerts": self.by_alert,
                "night_share": _pct(self.night_count, self.total),
                "weekend_share": _pct(self.weekend_count, self.total),
                "blast_radius": self.blast,
            },
            ensure_ascii=False,
            default=str,
        )


def _pct(part: int, whole: int) -> str:
    return f"{100 * part / whole:.0f}%" if whole else "0%"


async def collect(pool, days: int = 7, now: datetime | None = None) -> ReportData:
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(days=max(1, min(int(days), 90)))
    data = ReportData(period_start=start, period_end=now)
    async with pool.acquire() as conn:
        totals = await conn.fetchrow(_TOTALS, start, now)
        data.totals = dict(totals) if totals else {}
        data.by_cause = [(r["cause"], int(r["n"])) for r in await conn.fetch(_BY_CAUSE, start, now)]
        data.by_namespace = [
            (r["namespace"], int(r["n"])) for r in await conn.fetch(_BY_NAMESPACE, start, now)
        ]
        data.by_alert = [(r["alertname"], int(r["n"])) for r in await conn.fetch(_BY_ALERT, start, now)]
        data.by_hour = {int(r["hour"]): int(r["n"]) for r in await conn.fetch(_BY_HOUR, start, now)}
        data.by_dow = {int(r["dow"]): int(r["n"]) for r in await conn.fetch(_BY_DOW, start, now)}
        data.blast = [(r["blast_radius"], int(r["n"])) for r in await conn.fetch(_BLAST, start, now)]
    return data


def render(data: ReportData) -> str:
    """Deterministic part of the report, plain text (the bot escapes it)."""
    t = data.totals
    total = data.total
    lines = [
        f"INCIDENT REVIEW  {data.period_start:%d.%m}–{data.period_end:%d.%m.%Y}",
        "",
        (
            f"Incidents: {total}   critical: {int(t.get('critical') or 0)}   "
            f"analysed: {int(t.get('analyzed') or 0)}   "
            f"analysis failed: {int(t.get('failed') or 0)}   "
            f"over budget: {int(t.get('over_budget') or 0)}"
        ),
    ]
    if total == 0:
        lines.append("")
        lines.append("Nothing happened in this period. Good week.")
        return "\n".join(lines)

    confirmed, refuted = int(t.get("confirmed") or 0), int(t.get("refuted") or 0)
    if confirmed + refuted:
        lines.append(
            f"Engineer verdicts: {confirmed} confirmed, {refuted} refuted "
            f"(hypothesis accuracy on reviewed incidents: {_pct(confirmed, confirmed + refuted)})"
        )
    else:
        lines.append("Engineer verdicts: none yet (reply /ok <id> or /wrong <id> <cause> to teach it)")
    lines.append(
        f"AI cost: ${float(t.get('cost_usd') or 0):.4f}   "
        f"model call avg: {int(t.get('avg_latency_ms') or 0) / 1000:.1f}s"
    )
    ttfh_avg, ttfh_p95 = int(t.get("avg_ttfh_ms") or 0), int(t.get("p95_ttfh_ms") or 0)
    if ttfh_avg:
        lines.append(
            f"Alert fired → hypothesis in chat: avg {ttfh_avg / 1000:.0f}s, p95 {ttfh_p95 / 1000:.0f}s "
            "(manual triage: typically 10–30 min)"
        )

    lines += ["", "TOP CAUSES"]
    lines += [f"  {n:>3}  {_pct(n, total):>4}  {cause}" for cause, n in data.by_cause]
    lines += ["", "WHERE"]
    lines += [f"  {n:>3}  {_pct(n, total):>4}  {ns}" for ns, n in data.by_namespace]
    lines += ["", "WHICH ALERTS"]
    lines += [f"  {n:>3}  {_pct(n, total):>4}  {a}" for a, n in data.by_alert]
    lines += [
        "",
        "WHEN (UTC)",
        f"  at night (22:00–07:00): {data.night_count} ({_pct(data.night_count, total)})",
        f"  at the weekend:          {data.weekend_count} ({_pct(data.weekend_count, total)})",
        "  by hour: " + _sparkline(data.by_hour, 24),
    ]
    lines += ["", "BLAST RADIUS"]
    lines += [f"  {n:>3}  {_pct(n, total):>4}  {b}" for b, n in data.blast]
    return "\n".join(lines)


def _sparkline(by_hour: dict[int, int], width: int) -> str:
    bars = " ▁▂▃▄▅▆▇█"
    peak = max(by_hour.values(), default=0)
    if peak == 0:
        return "-" * width
    return "".join(bars[round(8 * by_hour.get(h, 0) / peak)] for h in range(width))


NARRATIVE_PROMPT = (
    "You are writing the closing section of a weekly incident review for an "
    "engineering manager. You are given the aggregated figures as JSON. Use ONLY "
    "those figures; do not invent numbers, causes or systems that are not in the "
    "data. Write two short parts in plain text, no markdown:\n"
    "OBSERVATIONS: 2-4 sentences on what the pattern says (concentration of causes, "
    "timing, blast radius, how often the assistant was right).\n"
    "RECOMMENDATIONS: up to 3 items labelled P0 (this week), P1 (this month), P2 "
    "(when convenient), each one line, each tied to a figure above.\n"
    "A hypothesis was produced for every analysed incident; what is unknown is "
    "whether they were right. Accuracy is known only from engineer_reviewed "
    "incidents; if that is 0, say that no incident has been reviewed yet. Never "
    "say the assistant made no predictions, and never call it accurate otherwise.\n"
    "If the data is too thin to say anything, say so in one sentence."
)


async def narrative(chat: ChatBackend, data: ReportData) -> str:
    """The model's part. Empty string on failure; the report ships regardless."""
    if data.total == 0:
        return ""
    messages = [
        {"role": "system", "content": NARRATIVE_PROMPT},
        {"role": "user", "content": data.as_json()},
    ]
    try:
        turn = await chat.chat(messages, None)
    except BackendError as exc:
        logger.warning("report narrative skipped: %s", exc)
        return ""
    return turn.content.strip()
