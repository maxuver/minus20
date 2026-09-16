"""Correctness checks for the replay / benchmark harness (VISION §6).

CC-26 Every shipped scenario file is well-formed and loads into an alert+context.
CC-27 Replaying a scenario runs the full pipeline and records a timing + cost.
CC-28 The benchmark runs all scenarios and stays within the budget accounting.
CC-52 An incident with an engineer's verdict replays and is graded against
      that verdict; exported, it is a scenario file the file runner loads
      identically (the eval set grows from the store, not from fixtures).
"""

from app.config import Settings
from app.models import IncidentStatus
from app.replay import SCENARIOS_DIR, load_scenario, run_all


def test_scenarios_exist_and_load():  # CC-26
    files = sorted(SCENARIOS_DIR.glob("*.json"))
    assert len(files) >= 5
    for path in files:
        name, alert, context = load_scenario(path)
        assert name
        assert alert.alertname
        # each scenario carries at least some context to reason over
        assert context.k8s_events or context.metrics or context.log_lines


async def test_replay_runs_full_pipeline_with_stub():  # CC-27
    incidents = await run_all(SCENARIOS_DIR, Settings(llm_provider="stub"))
    assert len(incidents) >= 5
    for inc in incidents:
        assert inc.status is IncidentStatus.ANALYZED
        assert inc.hypothesis is not None
        assert inc.latency_ms >= 0  # time-to-first-hypothesis recorded
        assert inc.cost_usd == 0.0  # stub backend is free


async def test_replay_respects_budget():  # CC-28
    # With zero budget, every scenario degrades instead of calling the model.
    incidents = await run_all(SCENARIOS_DIR, Settings(llm_provider="stub", daily_budget_usd=0.0))
    assert all(inc.status is IncidentStatus.BUDGET_EXCEEDED for inc in incidents)


async def test_verdict_backed_incident_replays_and_exports(tmp_path):  # CC-52
    from datetime import datetime, timezone

    from app.agent.scenario import incident_to_scenario
    from app.replay import export_cases, file_cases, run_cases, scenario_case

    row = {
        "id": "abcdef1234567890", "fingerprint": "fp-1", "alertname": "KubePodCrashLooping",
        "namespace": "payments", "severity": "critical", "alert_summary": "billing-api crash loops",
        "root_cause": "Image pull issue",
        "verdict": "wrong", "resolution": "NetworkPolicy blocked egress to postgres",
        "context": "## Kubernetes events\nWarning BackOff pod/billing-api-1 restarting\n## Logs\nFATAL: could not connect to postgres:5432\n",
        "created_at": datetime(2026, 9, 15, 3, 0, tzinfo=timezone.utc),
    }
    data = incident_to_scenario(row)
    case = scenario_case(data)
    name, alert, context, keywords = case
    assert name == "kubepodcrashlooping-abcdef12"
    assert alert.labels["pod"] == "billing-api-1" and alert.namespace == "payments"
    assert "networkpolicy" in keywords and "postgres:5432" in context.log_lines[0]

    graded = await run_cases([case], Settings(llm_provider="stub"))
    (incident, ok), = graded
    assert incident.status is IncidentStatus.ANALYZED
    assert ok is False  # the stub does not name the engineer's cause: a graded FAIL, not "ungraded"

    written = export_cases([(case, data)], tmp_path / "from-verdicts")
    assert [p.name for p in written] == ["kubepodcrashlooping-abcdef12.json"]
    (again,) = file_cases(tmp_path / "from-verdicts")
    assert again[0] == name and again[3] == keywords and again[2].log_lines == context.log_lines
