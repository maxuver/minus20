"""Logging that cannot leak a credential.

Two things put secrets into pod logs in this project's history: httpx logging
request URLs at INFO (the Telegram URL carries the bot token), and httpx
exception messages that quote the same URL, which then travel through
`logger.exception(...)`. Silencing httpx fixed the first; only redacting the
final formatted line fixes the second, tracebacks included. Every entrypoint
installs this formatter, so it holds for code nobody has written yet.
"""

from __future__ import annotations

import logging
import sys

from .redaction import redact


class RedactingFormatter(logging.Formatter):
    """Redacts the message and the exception text; leaves asctime alone."""

    def format(self, record: logging.LogRecord) -> str:
        record.msg = redact(record.getMessage())
        record.args = None
        return super().format(record)

    def formatException(self, ei) -> str:
        return redact(super().formatException(ei))


def configure_logging(level: str = "INFO", stream=None) -> None:
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # Belt and braces: never log request URLs at INFO either.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
