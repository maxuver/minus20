"""Mandatory PII/secret redaction (ADR-0002).

`redact()` is a pure function with no I/O, no config and no bypass path. Every
context bundle passes through `redact_bundle()` before a prompt is built, so the
LLM backend never sees raw secrets. This module is security-critical and is
tested and benchmarked on its own.

Masks are chosen so that re-running redaction is a no-op (idempotent): a mask
never contains a character that any pattern below would match again.
"""

from __future__ import annotations

import re

from .models import ContextBundle

# Order matters. High-structure secrets (JWTs, bearer tokens, cloud keys) are
# masked before the generic key=value rule so the generic rule can't shred them
# into a half-masked mess. Emails before IPs; IPs before the generic rule.
_RULES: list[tuple[re.Pattern[str], str]] = [
    # JSON Web Tokens: three base64url segments separated by dots.
    (
        re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b"),
        "[REDACTED_JWT]",
    ),
    # "Authorization: Bearer <token>" / "Bearer <token>".
    (
        re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
        "Bearer [REDACTED_TOKEN]",
    ),
    # Telegram bot tokens (<bot id>:<35 chars>). httpx puts the full request URL
    # into every exception message, and the URL carries the token. No leading
    # word boundary: in that URL the digits are glued to "bot".
    (re.compile(r"\d{8,12}:[A-Za-z0-9_-]{30,}\b"), "[REDACTED_BOT_TOKEN]"),
    # AWS access key id and secret access key.
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "[REDACTED_AWS_KEY]"),
    (
        re.compile(r"(?i)\baws_secret_access_key\b\s*[=:]\s*[A-Za-z0-9/+]{40}"),
        "aws_secret_access_key=[REDACTED_SECRET]",
    ),
    # Email addresses.
    (
        re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
        "[REDACTED_EMAIL]",
    ),
    # IPv6 then IPv4. IPv6 first so it isn't partially eaten by IPv4. Either
    # the full eight groups, or a compressed form containing "::". The earlier
    # "any 3-8 colon-separated hex groups" rule also matched clock times like
    # 20:59:12 once the log formatter started redacting whole lines.
    (
        re.compile(
            r"\b(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}\b"
            r"|(?<![:\w])(?:[0-9A-Fa-f]{1,4}:){1,6}:(?:[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4}){0,5})?(?![:\w])"
            r"|(?<![:\w])::(?:[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4}){0,6})(?![:\w])"
        ),
        "[REDACTED_IPV6]",
    ),
    (
        re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b"),
        "[REDACTED_IP]",
    ),
    # Generic secret-shaped key=value / key: value pairs. The value is a run of
    # non-space, non-quote characters; a value that is already a mask ("[...]")
    # is matched but replaced with the identical text, preserving idempotency.
    (
        re.compile(
            r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|"
            r"client[_-]?secret|authorization)\b(\s*[=:]\s*)([^\s\"']+)"
        ),
        r"\1\2[REDACTED_SECRET]",
    ),
]


def redact(text: str) -> str:
    """Mask emails, IPs, tokens, cloud keys and secret-shaped values in `text`."""
    for pattern, replacement in _RULES:
        text = pattern.sub(replacement, text)
    return text


def redact_lines(lines: list[str]) -> list[str]:
    return [redact(line) for line in lines]


def redact_bundle(bundle: ContextBundle) -> ContextBundle:
    """Return a copy of `bundle` with every collected line redacted.

    Source-health metadata (sources_ok/sources_failed) is passed through
    unchanged — it names collectors, never payload data.
    """
    return ContextBundle(
        log_lines=redact_lines(bundle.log_lines),
        metrics=redact_lines(bundle.metrics),
        k8s_events=redact_lines(bundle.k8s_events),
        sources_ok=list(bundle.sources_ok),
        sources_failed=list(bundle.sources_failed),
    )
