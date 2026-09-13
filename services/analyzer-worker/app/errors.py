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
