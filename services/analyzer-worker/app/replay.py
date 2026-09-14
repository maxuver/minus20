"""Replay a library of fault-injection scenarios through the real pipeline and
report the benchmark VISION §6 asks for: time-to-first-hypothesis, cost per
alert and the produced root cause — per scenario, reproducibly.

Each scenario file carries the alert plus the exact context bundle a live
cluster would have produced, so a run is deterministic and needs no cluster,
Loki or Prometheus. Only the LLM backend is a live variable — with the stub
backend the harness is offline and free (it proves the harness); point it at a
real backend and the numbers become real.

    python -m app.replay [scenarios_dir]
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

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


async def run_all_graded(
    scenarios_dir: Path = SCENARIOS_DIR, cfg: Settings = settings
) -> list[tuple[Incident, bool | None]]:
    """Replay every scenario, pairing each incident with its grade.

    The grade is None for scenarios that declare no expectation (the easy set,
    which exists to prove the pipeline runs, not to measure accuracy).
    """
    backend = get_backend(cfg)
    budget = InMemoryBudget(cfg.daily_budget_usd)
    results: list[tuple[Incident, bool | None]] = []
    for i, path in enumerate(sorted(scenarios_dir.glob("*.json"))):
        if i and cfg.replay_pause_seconds:
            # Free API tiers meter requests per minute; back-to-back scenarios
            # turned into 429s on Gemini's free tier (2026-09-14).
            await asyncio.sleep(cfg.replay_pause_seconds)
        _name, alert, context = load_scenario(path)
        incident = await run_scenario(alert, context, backend, budget, cfg)
        results.append((incident, grade(incident, expected_keywords(path))))
    return results


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


def main() -> None:  # pragma: no cover - CLI entrypoint
    scenarios_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else SCENARIOS_DIR
    _print_report(asyncio.run(run_all_graded(scenarios_dir)))


if __name__ == "__main__":  # pragma: no cover
    main()
