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
- The one exception to "one call per alert" is the correlation revision
  (ADR-0006): several alerts of one namespace, attached to one leader, get
  ONE extra call over all their contexts after a settle period, replacing
  the leader's hypothesis. Still bounded: at most one revision per settle
  period per namespace.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from .correlation import alert_line
from .models import Incident, IncidentStatus, StreamAlert
from .ports import (
    Budget,
    Collector,
    CorrelationTracker,
    Deduplicator,
    IncidentStore,
    LLMBackend,
    Notifier,
    StormTracker,
)
from .prompt import build_correlated_prompt, build_prompt
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
        correlation_tracker: CorrelationTracker | None = None,
        correlation_settle_seconds: float = 20.0,
        correlation_window_seconds: int = 180,
    ) -> None:
        self._collector = collector
        self._backend = backend
        self._notifier = notifier
        self._store = store
        self._budget = budget
        self._timeout = llm_timeout_seconds
        self._dedup = deduplicator
        self._storm = storm_tracker
        self._corr = correlation_tracker
        self._settle = correlation_settle_seconds
        self._corr_window = correlation_window_seconds
        self._revisions: set[asyncio.Task] = set()

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

        # A different alert in a namespace that already has a fresh incident is
        # attached to it instead of getting its own hypothesis; one revision
        # over all attached alerts follows after the settle period (ADR-0006).
        # Registered after collection so the member's context is on record.
        if self._corr is not None:
            state = await self._corr.track(alert, incident.id, incident.context)
            if not state.leader:
                incident.status = IncidentStatus.CORRELATED
                incident.grouped_into = state.leader_id
                incident.correlated_alerts = list(state.members)
                await self._store.save(incident)
                await self._notifier.notify(incident)
                self._schedule_revision(alert)
                logger.info(
                    "correlated alert=%s ns=%s leader=%s members=%d",
                    alert.alertname, alert.namespace, state.leader_id[:8], len(state.members),
                )
                return incident

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

    # --- correlation revision (ADR-0006) ------------------------------------

    def _schedule_revision(self, alert: StreamAlert) -> None:
        task = asyncio.create_task(self._revise_later(alert))
        self._revisions.add(task)
        task.add_done_callback(self._revisions.discard)

    async def drain(self) -> None:
        """Let pending revisions finish (called on graceful stop and by tests)."""
        while self._revisions:
            await asyncio.gather(*list(self._revisions), return_exceptions=True)

    async def _revise_later(self, alert: StreamAlert) -> None:
        if self._settle > 0:
            await asyncio.sleep(self._settle)
        try:
            await self.revise(alert)
        except Exception:
            logger.exception("correlation revision failed for ns=%s", alert.namespace)

    async def revise(self, alert: StreamAlert) -> Incident | None:
        """One model call over the leader and every attached alert; the leader's
        hypothesis is replaced, the previous cause kept as revised_from."""
        if self._corr is None:
            return None
        # One revision per settle period per namespace, across replicas.
        if not await self._corr.claim_revision(alert, max(self._settle, 1.0)):
            return None
        bundle = await self._corr.bundle(alert)
        if bundle is None or not bundle.members:
            return None
        if not await self._budget.has_budget():
            logger.warning("daily budget exhausted; correlation revision skipped for ns=%s", alert.namespace)
            return None
        leader = bundle.leader_alert
        members = bundle.members[:12]
        prompt = build_correlated_prompt(leader, bundle.leader_context, members, self._corr_window)
        started = time.perf_counter()
        try:
            result = await asyncio.wait_for(self._backend.analyze(prompt), timeout=self._timeout)
        except (TimeoutError, Exception) as exc:  # noqa: BLE001 - best-effort by design
            logger.warning("correlation revision failed for ns=%s: %s", alert.namespace, exc)
            return None
        await self._budget.add(result.cost_usd)
        previous = await self._previous_cause(bundle.leader_id)
        revised = Incident(
            id=bundle.leader_id,
            fingerprint=leader.fingerprint,
            alertname=leader.alertname,
            namespace=leader.namespace,
            severity=leader.severity,
            alert_summary=leader.summary(),
            status=IncidentStatus.ANALYZED,
            hypothesis=result.hypothesis,
            backend=getattr(result, "backend", "") or self._backend.name,
            cost_usd=result.cost_usd,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            latency_ms=int((time.perf_counter() - started) * 1000),
            correlated_alerts=[alert_line(a) for a, _ in bundle.members],
            revised_from=previous,
            context=prompt,  # the audit trail: exactly what the revision was shown
        )
        await self._store.revise(revised)
        await self._notifier.notify(revised)
        logger.info(
            "revised leader=%s ns=%s alerts=%d backend=%s latency=%dms cause=%r",
            bundle.leader_id[:8], leader.namespace, len(bundle.members) + 1, revised.backend,
            revised.latency_ms, (result.hypothesis.root_cause if result.hypothesis else "")[:80],
        )
        return revised

    async def _previous_cause(self, leader_id: str) -> str:
        """The cause the leader had before the revision, when the store can tell."""
        saved = getattr(self._store, "saved", None)
        if saved is not None:
            for inc in saved:
                if inc.id == leader_id and inc.hypothesis:
                    return inc.hypothesis.root_cause
        getter = getattr(self._store, "root_cause", None)
        if getter is not None:
            return await getter(leader_id) or ""
        return ""

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
