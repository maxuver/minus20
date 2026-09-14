"""Turn a provider error into a sentence safe to store, log and deliver.

httpx's own message is "Client error '404 Not Found' for url '<full url>'
For more information check: https://developer.mozilla.org/...". The URL can
carry a credential (the Telegram one does), and the MDN link is noise, while
the one useful thing, the provider's response body, is not included at all.
The Gemini 404 that reached an engineer's phone said nothing; its body said
"use models/gemini-3.6-flash". This keeps the body and drops the rest.
"""

from __future__ import annotations

import re

from .redaction import redact

_URL_CLAUSE = re.compile(r"\s*for url '[^']*'", re.IGNORECASE)
_MDN_CLAUSE = re.compile(r"\s*For more information check: \S+", re.IGNORECASE)
MAX_LEN = 240


def describe(exc: BaseException) -> str:
    """One redacted line: HTTP status plus the provider's body, or the exception type."""
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status is not None:
        body = ""
        try:
            body = (response.text or "").strip()
        except Exception:  # noqa: BLE001 - a body we cannot read is not worth failing over
            body = ""
        body = re.sub(r"\s+", " ", body)
        text = f"HTTP {status} from provider" + (f": {body}" if body else "")
    else:
        text = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        text = _MDN_CLAUSE.sub("", _URL_CLAUSE.sub("", text))
    text = redact(text)
    return text if len(text) <= MAX_LEN else text[: MAX_LEN - 1] + "…"


RETRY_STATUSES = frozenset({429, 503})
RETRY_DELAYS = (2.0, 5.0)  # two retries, seven seconds at most, inside the call timeout


async def post_with_retry(client, path: str, payload: dict):
    """POST, retrying only on the two statuses that mean 'not now': 429 and 503.

    Gemini's free tier answered "This model is currently experiencing high
    demand" (503) on and off for an hour on 2026-09-14; a one-shot call turned
    every such blip into a failed analysis. Anything else raises at once.
    """
    import asyncio

    for delay in (*RETRY_DELAYS, None):
        resp = await client.post(path, json=payload)
        if resp.status_code in RETRY_STATUSES and delay is not None:
            await asyncio.sleep(delay)
            continue
        resp.raise_for_status()
        return resp
    return resp  # pragma: no cover - loop always returns or raises
