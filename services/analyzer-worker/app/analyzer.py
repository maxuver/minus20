"""The analysis orchestrator: one alert in, one incident out.

The straight-line flow encodes the guarantees from the ADRs:

  collect → REDACT → budget check → ONE llm call → persist → notify

- Redaction happens before the prompt is built, on the only path to the model,
  so there is no branch that could hand raw context to the LLM (ADR-0002).
- Exactly one backend call per alert; no agent loop (ADR-0001).
- The call is wrapped in a hard timeout, and any failure — timeout, backend
  error, bad output — is caught: the incident is still recorded and delivered,
  marked, and never retried into a pile-up (ADR-0003).
- If the day's budget is spent, no call is made at all.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from .models import Incident, IncidentStatus, StreamAlert
from .ports import (
    Budget,
    Collector,
    Deduplicator,
    IncidentStore,
    LLMBackend,
    Notifier,
    StormTracker,
)
from .prompt import build_prompt
from .redaction import redact_bundle

logger = logging.getLogger("analyzer-worker.analyzer")


class Analyzer:
    def __init__(
        self,
        collector: Collector,
        backend: LLMBackend,
        notifier: Notifier,
        store: IncidentStore,
        budget: Budget,
        llm_timeout_seconds: float = 30.0,
        deduplicator: Deduplicator | None = None,
        storm_tracker: StormTracker | None = None,
    ) -> None:
        self._collector = collector
        self._backend = backend
        self._notifier = notifier
        self._store = store
        self._budget = budget
        self._timeout = llm_timeout_seconds
        self._dedup = deduplicator
        self._storm = storm_tracker

    async def analyze(self, alert: StreamAlert, *, skip_dedup: bool = False) -> Incident:
        incident = Incident(
            fingerprint=alert.fingerprint,
            alertname=alert.alertname,
            namespace=alert.namespace,
            severity=alert.severity,
            alert_summary=alert.summary(),
            backend=self._backend.name,
        )

        # Suppress a repeat of an alert already handled in this window. Checked
        # first, so a duplicate costs no collector calls and no LLM spend.
        # A message reclaimed from a consumer that died mid-analysis has already
        # been marked in the dedup window by that consumer; it must still be
        # analysed, or the alert is lost for the whole window.
        if not skip_dedup and self._dedup is not None and await self._dedup.is_duplicate(alert):
            incident.status = IncidentStatus.DUPLICATE_SUPPRESSED
            logger.info("suppressed duplicate alert=%s fp=%s", alert.alertname, alert.fingerprint)
            return incident

        # A storm member is recorded and counted, never analysed: the leader's
        # hypothesis already covers the shared cause, and thirty model calls
        # for thirty pods would say the same thing thirty times.
        if self._storm is not None:
            state = await self._storm.track(alert, incident.id)
            if not state.leader:
                incident.status = IncidentStatus.GROUPED
                incident.grouped_into = state.leader_id
                incident.storm_size = state.count
                incident.storm_pods = list(state.pods)
                await self._store.save(incident)
                if state.notify:
                    await self._notifier.notify(incident)
                logger.info(
                    "storm alert=%s ns=%s size=%d leader=%s notified=%s",
                    alert.alertname, alert.namespace, state.count, state.leader_id[:8], state.notify,
                )
                return incident

        # Collect and redact BEFORE anything leaves the process.
        raw_context = await self._collector.collect(alert)
        context = redact_bundle(raw_context)
        incident.context = context.render()  # redacted; stored with the incident

        if not await self._budget.has_budget():
            incident.status = IncidentStatus.BUDGET_EXCEEDED
            logger.warning("daily budget exhausted; skipping LLM for %s", alert.alertname)
            return await self._finish(incident)

        prompt = build_prompt(alert, context)
        started = time.perf_counter()
        try:
            result = await asyncio.wait_for(
                self._backend.analyze(prompt), timeout=self._timeout
            )
        except (TimeoutError, Exception) as exc:  # noqa: BLE001 - best-effort by design
            reason = "timeout" if isinstance(exc, TimeoutError) else str(exc)
            incident.status = IncidentStatus.ANALYSIS_FAILED
            incident.failure_reason = reason
            incident.latency_ms = int((time.perf_counter() - started) * 1000)
            logger.warning("analysis failed for %s: %s", alert.alertname, reason)
            return await self._finish(incident)

        await self._budget.add(result.cost_usd)
        incident.status = IncidentStatus.ANALYZED
        incident.hypothesis = result.hypothesis
        incident.backend = getattr(result, "backend", incident.backend) or incident.backend
        incident.cost_usd = result.cost_usd
        incident.input_tokens = result.input_tokens
        incident.output_tokens = result.output_tokens
        incident.latency_ms = int((time.perf_counter() - started) * 1000)
        if alert.startsAt is not None:
            fired = alert.startsAt if alert.startsAt.tzinfo else alert.startsAt.replace(tzinfo=timezone.utc)
            incident.time_to_hypothesis_ms = max(0, int((datetime.now(timezone.utc) - fired).total_seconds() * 1000))
        return await self._finish(incident)

    async def _finish(self, incident: Incident) -> Incident:
        """Persist and deliver. Always reached, on every status."""
        await self._store.save(incident)
        await self._notifier.notify(incident)
        cause = (
            incident.hypothesis.root_cause if incident.hypothesis else incident.failure_reason
        ) or ""
        logger.info(
            "incident alert=%s status=%s backend=%s latency=%dms cost=$%.6f cause=%r",
            incident.alertname,
            incident.status.value,
            incident.backend,
            incident.latency_ms,
            incident.cost_usd,
            cause[:80],
        )
        return incident
