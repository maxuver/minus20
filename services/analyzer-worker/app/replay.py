"""Replay a library of fault-injection scenarios through the real pipeline and
report the benchmark VISION §6 asks for: time-to-first-hypothesis, cost per
alert and the produced root cause — per scenario, reproducibly.

Each scenario file carries the alert plus the exact context bundle a live
cluster would have produced, so a run is deterministic and needs no cluster,
Loki or Prometheus. Only the LLM backend is a live variable — with the stub
backend the harness is offline and free (it proves the harness); point it at a
real backend and the numbers become real.

    python -m app.replay [scenarios_dir]
    python -m app.replay --from-store [--days N] [--export DIR]

The second form is the eval set growing from real incidents: every incident
the engineer marked with /ok or /wrong carries the redacted context the model
saw and the verdict as the expected answer, so the store is replayed and
graded exactly like the fixture files, with no fixture written by hand.
`--export` writes those incidents out as scenario files, the same shape as
scenarios/hard/, so a team can commit the ones worth keeping.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from .agent.scenario import incident_to_scenario
from .analyzer import Analyzer
from .backends import get_backend
from .budget import InMemoryBudget
from .config import Settings, settings
from .models import ContextBundle, Incident, StreamAlert
from .notifiers import StubNotifier
from .stores import InMemoryStore

SCENARIOS_DIR = Path(__file__).resolve().parent.parent / "scenarios"


class ReplayCollector:
    """Serves the context recorded in a scenario file instead of a live source."""

    name = "replay"

    def __init__(self, context: ContextBundle) -> None:
        self._context = context

    async def collect(self, alert: StreamAlert) -> ContextBundle:
        return self._context


def load_scenario(path: Path) -> tuple[str, StreamAlert, ContextBundle]:
    data = json.loads(path.read_text(encoding="utf-8"))
    alert = StreamAlert(**data["alert"])
    context = ContextBundle(**data.get("context", {}))
    return data.get("name", path.stem), alert, context


def expected_keywords(path: Path) -> list[str]:
    """Terms that must appear in the root cause for the answer to count.

    Only the hard scenarios declare these. Grading by keyword rather than by an
    LLM judge keeps the score reproducible and lets a reader see exactly what was
    counted as correct.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    return [k.lower() for k in data.get("expected_keywords", [])]


def grade(incident: Incident, keywords: list[str]) -> bool | None:
    """True/False when the scenario declares an expectation, else None.

    Graded on the stated root cause only, deliberately not on the evidence list.
    An earlier version accepted a keyword appearing anywhere in the hypothesis,
    which scored a pass for an answer whose root cause was the misleading one the
    scenario was built to punish — the right term merely happened to appear in a
    cited log line. The engineer acts on the cause that is stated; if that is
    wrong they go the wrong way regardless of what the evidence contains.
    """
    if not keywords:
        return None
    if incident.hypothesis is None:
        return False
    cause = incident.hypothesis.root_cause.lower()
    return any(k in cause for k in keywords)


async def run_scenario(
    alert: StreamAlert,
    context: ContextBundle,
    backend,
    budget,
    cfg: Settings = settings,
) -> Incident:
    analyzer = Analyzer(
        collector=ReplayCollector(context),
        backend=backend,
        notifier=StubNotifier(),
        store=InMemoryStore(),
        budget=budget,
        # Must come from the passed config, not the global settings: a caller
        # benchmarking a slow local model needs its own timeout to apply.
        llm_timeout_seconds=cfg.llm_timeout_seconds,
    )
    return await analyzer.analyze(alert)


async def run_all(scenarios_dir: Path = SCENARIOS_DIR, cfg: Settings = settings) -> list[Incident]:
    return [inc for inc, _ in await run_all_graded(scenarios_dir, cfg)]


Case = tuple[str, StreamAlert, ContextBundle, list[str]]  # name, alert, context, expected keywords


def scenario_case(data: dict, fallback_name: str = "") -> Case:
    """One replayable case from scenario JSON (a file or an exported incident)."""
    alert = StreamAlert(**data["alert"])
    context = ContextBundle(**data.get("context", {}))
    keywords = [k.lower() for k in data.get("expected_keywords", [])]
    return data.get("name", fallback_name), alert, context, keywords


def file_cases(scenarios_dir: Path) -> list[Case]:
    return [
        scenario_case(json.loads(p.read_text(encoding="utf-8")), p.stem)
        for p in sorted(scenarios_dir.glob("*.json"))
    ]


VERDICT_SQL = (
    "SELECT id, fingerprint, alertname, namespace, severity, alert_summary, root_cause, "
    "verdict, resolution, context, created_at FROM incidents "
    "WHERE verdict IS NOT NULL AND context IS NOT NULL AND context <> '' "
    "AND created_at > now() - ($1::int * interval '1 day') ORDER BY created_at"
)


async def store_cases(dsn: str, days: int = 90) -> list[tuple[Case, dict]]:
    """Every incident with an engineer's verdict, as a case plus its scenario JSON.

    /ok makes the recorded hypothesis the expectation, /wrong <cause> makes the
    engineer's cause the expectation (see agent/scenario.py). Incidents without
    a verdict are not an eval set, they are just history, and are skipped.
    """
    from .stores import connect_pool

    pool = await connect_pool(dsn)
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(VERDICT_SQL, days)
    finally:
        await pool.close()
    out: list[tuple[Case, dict]] = []
    for row in rows:
        data = incident_to_scenario(dict(row))
        out.append((scenario_case(data), data))
    return out


async def run_cases(cases: list[Case], cfg: Settings = settings) -> list[tuple[Incident, bool | None]]:
    """Replay each case through the real pipeline, pairing the incident with its grade.

    The grade is None for cases that declare no expectation (the easy set,
    which exists to prove the pipeline runs, not to measure accuracy).
    """
    backend = get_backend(cfg)
    budget = InMemoryBudget(cfg.daily_budget_usd)
    results: list[tuple[Incident, bool | None]] = []
    for i, (_name, alert, context, keywords) in enumerate(cases):
        if i and cfg.replay_pause_seconds:
            # Free API tiers meter requests per minute; back-to-back scenarios
            # turned into 429s on Gemini's free tier (2026-09-14).
            await asyncio.sleep(cfg.replay_pause_seconds)
        incident = await run_scenario(alert, context, backend, budget, cfg)
        results.append((incident, grade(incident, keywords)))
    return results


async def run_all_graded(
    scenarios_dir: Path = SCENARIOS_DIR, cfg: Settings = settings
) -> list[tuple[Incident, bool | None]]:
    """Replay every scenario file in a directory (the original entry point)."""
    return await run_cases(file_cases(scenarios_dir), cfg)


def export_cases(exported: list[tuple[Case, dict]], out_dir: Path) -> list[Path]:
    """Write verdict-backed incidents as scenario files, one per incident."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for (name, _alert, _context, _kw), data in exported:
        path = out_dir / f"{name}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        written.append(path)
    return written


def _print_report(graded: list[tuple[Incident, bool | None]]) -> None:
    header = (
        f"{'scenario':<26} {'status':<16} {'ttfh(ms)':>9} {'cost($)':>10} "
        f"{'ok':>4}  root cause"
    )
    print(header)
    print("-" * len(header))
    total_ms = 0
    total_cost = 0.0
    scored = correct = 0
    for inc, ok in graded:
        total_ms += inc.latency_ms
        total_cost += inc.cost_usd
        if ok is not None:
            scored += 1
            correct += int(ok)
        mark = {True: "PASS", False: "FAIL", None: "-"}[ok]
        rc = (inc.hypothesis.root_cause if inc.hypothesis else inc.failure_reason) or ""
        print(
            f"{inc.alertname[:24]:<26} {inc.status.value:<16} {inc.latency_ms:>9} "
            f"{inc.cost_usd:>10.6f} {mark:>4}  {rc[:60]}"
        )
    n = len(graded) or 1
    print("-" * len(header))
    summary = f"{len(graded)} scenarios"
    print(
        f"{'TOTAL/AVG':<26} {summary:<16} {total_ms // n:>9} "
        f"{total_cost:>10.6f} {'':>4}  backend={settings.llm_provider}"
    )
    if scored:
        print(f"{'ACCURACY':<26} {correct}/{scored} graded scenarios correct")


async def _from_store(days: int, export: Path | None) -> None:  # pragma: no cover - needs Postgres
    exported = await store_cases(settings.postgres_dsn, days)
    if not exported:
        print(f"No incidents with a verdict in the last {days} days; reply /ok or /wrong <id> <cause> in the bot first.")
        return
    if export is not None:
        for path in export_cases(exported, export):
            print(f"exported {path}")
    print(f"{len(exported)} incidents with a verdict; replaying against backend={settings.llm_provider}")
    _print_report(await run_cases([case for case, _ in exported]))


def main() -> None:  # pragma: no cover - CLI entrypoint
    import argparse

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("scenarios_dir", nargs="?", default=None, help="directory of scenario files")
    ap.add_argument("--from-store", action="store_true", help="replay incidents with an engineer's verdict from Postgres")
    ap.add_argument("--days", type=int, default=90, help="how far back --from-store looks (default 90)")
    ap.add_argument("--export", type=Path, default=None, help="with --from-store: also write scenario files here")
    args = ap.parse_args()
    if args.from_store:
        asyncio.run(_from_store(args.days, args.export))
        return
    scenarios_dir = Path(args.scenarios_dir) if args.scenarios_dir else SCENARIOS_DIR
    _print_report(asyncio.run(run_all_graded(scenarios_dir)))


if __name__ == "__main__":  # pragma: no cover
    main()
