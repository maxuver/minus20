"""Delivery of a processed incident.

Every incident produces a message, including when analysis failed or the budget
was exhausted — the engineer must always receive the alert essentials, with the
hypothesis as an overlay when present (ADR-0003). Raw-alert-first-on-ingest is a
separate ingest-api responsibility; this notifier guarantees the worker never
swallows an alert even when the model is unavailable.

Two channels, same content. Telegram is rendered as HTML rather than MarkdownV2
(MarkdownV2 needs a dozen characters escaped and log lines are full of them),
Slack as Block Kit. Both escape &, < and >, so a log line or model output can
neither break the rendering nor inject markup.
"""

from __future__ import annotations

from html import escape

from .config import Settings, settings
from .models import Incident, IncidentStatus
from .redaction import redact

_SEVERITY_ICON = {"critical": "🔴", "warning": "🟡", "info": "🔵"}


def _icon(severity: str) -> str:
    return _SEVERITY_ICON.get(severity.lower(), "⚪")


def _ttfh(incident: Incident) -> str:
    """' · 41 s from alert' when the alert carried a firing time."""
    if not incident.time_to_hypothesis_ms:
        return ""
    return f" · {incident.time_to_hypothesis_ms / 1000:.0f} s from alert"


def _where(incident: Incident) -> str:
    return " · ".join(
        p for p in (incident.namespace, incident.severity) if p and p != "unknown"
    )


# --- Telegram ---------------------------------------------------------------


def format_message(incident: Incident) -> str:
    """Render the incident as Telegram-flavoured HTML."""
    name = escape(incident.alertname or "Alert")
    lines = [f"{_icon(incident.severity)} <b>{name}</b>"]
    where = _where(incident)
    if where:
        lines.append(f"<i>{escape(where)}</i>")

    if incident.status is IncidentStatus.ANALYZED and incident.hypothesis:
        h = incident.hypothesis
        lines.append("")
        lines.append(f"🤖 <b>Likely cause</b> <i>({escape(h.confidence)} confidence)</i>")
        lines.append(escape(h.root_cause))

        if h.blast_radius and h.blast_radius != "unknown":
            lines.append("")
            lines.append(f"💥 <b>Blast radius:</b> <code>{escape(h.blast_radius)}</code>")

        if h.evidence:
            lines.append("")
            lines.append("📋 <b>Evidence</b>")
            lines.extend(f"• <code>{escape(e)}</code>" for e in h.evidence)

        if h.disproof:
            lines.append("")
            lines.append("🎯 <b>Cheapest way to disprove</b>")
            lines.append(f"<i>{escape(h.disproof)}</i>")

        if h.next_steps:
            lines.append("")
            lines.append("✅ <b>Next steps</b>")
            lines.extend(f"{i}. {escape(s)}" for i, s in enumerate(h.next_steps, 1))

        lines.append("")
        lines.append(
            f"<i>⏱ {escape(incident.backend)} · {incident.latency_ms} ms"
            f"{_ttfh(incident)} · ${incident.cost_usd:.4f} · #{incident.id[:8]}</i>"
        )

    elif incident.status is IncidentStatus.GROUPED:
        lines.append("")
        lines.append(f"⚡ <b>Alert storm:</b> {incident.storm_size} pods so far with this alert")
        if incident.storm_pods:
            shown = ", ".join(escape(p) for p in incident.storm_pods[:8])
            more = "" if len(incident.storm_pods) <= 8 else f" +{len(incident.storm_pods) - 8} more"
            lines.append(f"<code>{shown}</code>{escape(more)}")
        lines.append(f"<i>Analysed once as #{incident.grouped_into[:8]}; one shared cause is likely "
                     "(node, rollout, dependency). Members are recorded, not re-analysed.</i>")

    elif incident.status is IncidentStatus.BUDGET_EXCEEDED:
        lines.append("")
        lines.append("⚠️ <b>AI analysis skipped</b> — daily budget reached.")
        lines.append("<i>Raw alert delivered as usual.</i>")

    else:
        lines.append("")
        lines.append("⚠️ <b>AI analysis unavailable</b>")
        if incident.failure_reason:
            lines.append(f"<code>{escape(redact(incident.failure_reason)[:240])}</code>")
        lines.append("<i>Raw alert delivered as usual.</i>")

    return "\n".join(lines)


# --- Slack ------------------------------------------------------------------
# Slack mrkdwn reserves &, < and > for entities and links, so the same three
# characters need escaping as in HTML, but inside Slack's own block structure.


def _slack_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def format_slack_blocks(incident: Incident) -> list[dict]:
    """Render the incident as Slack Block Kit."""
    esc = _slack_escape
    title = f"{_icon(incident.severity)} {incident.alertname or 'Alert'}"
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": title[:150]}}
    ]

    where = _where(incident)
    if where:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": esc(where)}]})

    if incident.status is IncidentStatus.ANALYZED and incident.hypothesis:
        h = incident.hypothesis
        blocks.append(
            _section(f"*Likely cause* _({esc(h.confidence)} confidence)_\n{esc(h.root_cause)}")
        )
        if h.blast_radius and h.blast_radius != "unknown":
            blocks.append(_section(f"*Blast radius*  `{esc(h.blast_radius)}`"))
        if h.evidence:
            bullets = "\n".join(f"• `{esc(e)}`" for e in h.evidence)
            blocks.append(_section(f"*Evidence*\n{bullets}"))
        if h.disproof:
            blocks.append(_section(f"*Cheapest way to disprove*\n_{esc(h.disproof)}_"))
        if h.next_steps:
            steps = "\n".join(f"{i}. {esc(s)}" for i, s in enumerate(h.next_steps, 1))
            blocks.append(_section(f"*Next steps*\n{steps}"))
        blocks.append({"type": "divider"})
        # The short id is what the engineer quotes back to the agent
        # (/ok <id>, /wrong <id> <cause>) to record the real outcome (ADR-0005).
        footer = (
            f"{esc(incident.backend)} · {incident.latency_ms} ms{_ttfh(incident)} · "
            f"${incident.cost_usd:.4f} · #{incident.id[:8]}"
        )
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": footer}]})

    elif incident.status is IncidentStatus.GROUPED:
        pods = ", ".join(esc(p) for p in incident.storm_pods[:8])
        more = "" if len(incident.storm_pods) <= 8 else f" +{len(incident.storm_pods) - 8} more"
        blocks.append(
            _section(
                f"*Alert storm:* {incident.storm_size} pods so far with this alert"
                + (f"\n`{pods}`{more}" if pods else "")
                + f"\n_Analysed once as #{incident.grouped_into[:8]}; one shared cause is likely "
                "(node, rollout, dependency). Members are recorded, not re-analysed._"
            )
        )

    elif incident.status is IncidentStatus.BUDGET_EXCEEDED:
        blocks.append(
            _section(
                "*AI analysis skipped* — daily budget reached."
                "\n_Raw alert delivered as usual._"
            )
        )

    else:
        reason = esc(redact(incident.failure_reason or ""))[:240]
        tail = f"\n`{reason}`" if reason else ""
        blocks.append(
            _section(f"*AI analysis unavailable*{tail}\n_Raw alert delivered as usual._")
        )

    return blocks


# --- adapters ---------------------------------------------------------------


class StubNotifier:
    """Records deliveries in memory for tests and offline runs."""

    name = "stub"

    def __init__(self) -> None:
        self.sent: list[Incident] = []

    async def notify(self, incident: Incident) -> None:
        self.sent.append(incident)


class TelegramNotifier:
    """Sends the incident message to a Telegram chat."""

    name = "telegram"

    def __init__(self, cfg: Settings = settings, client=None) -> None:
        self._cfg = cfg
        self._client = client  # inject an httpx.AsyncClient in tests

    async def notify(self, incident: Incident) -> None:
        import httpx

        url = f"https://api.telegram.org/bot{self._cfg.telegram_bot_token}/sendMessage"
        client = self._client or httpx.AsyncClient(timeout=10.0)
        try:
            resp = await client.post(
                url,
                json={
                    "chat_id": self._cfg.telegram_chat_id,
                    "text": format_message(incident),
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
            resp.raise_for_status()
        finally:
            if self._client is None:
                await client.aclose()


class SlackNotifier:
    """Posts the incident to a Slack Incoming Webhook.

    A webhook rather than the Bot API on purpose: the customer pastes one URL,
    with no OAuth app to create and no scopes for their security team to review.
    """

    name = "slack"

    def __init__(self, cfg: Settings = settings, client=None) -> None:
        self._cfg = cfg
        self._client = client  # inject an httpx.AsyncClient in tests

    async def notify(self, incident: Incident) -> None:
        import httpx

        client = self._client or httpx.AsyncClient(timeout=10.0)
        try:
            resp = await client.post(
                self._cfg.slack_webhook_url,
                json={
                    # `text` is the fallback shown in the sidebar and in mobile
                    # push notifications; `blocks` is the rich body.
                    "text": f"{incident.alertname}: {incident.status.value}",
                    "blocks": format_slack_blocks(incident),
                },
            )
            resp.raise_for_status()
        finally:
            if self._client is None:
                await client.aclose()


def get_notifier(cfg: Settings = settings):
    channel = cfg.notifier.lower()
    if channel == "telegram":
        return TelegramNotifier(cfg)
    if channel == "slack":
        return SlackNotifier(cfg)
    return StubNotifier()
