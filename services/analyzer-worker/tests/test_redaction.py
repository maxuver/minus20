"""Correctness checks for the mandatory redactor (ADR-0002).

CC-6  Emails, IPv4, IPv6, bearer tokens, JWTs, AWS keys and secret-shaped
      key=value pairs are all masked.
CC-8  Redaction is idempotent and preserves surrounding structure.
"""

import pytest

from app.models import ContextBundle
from app.redaction import redact, redact_bundle

SECRETS = [
    ("user alice@example.com failed login", "alice@example.com", "[REDACTED_EMAIL]"),
    ("client ip 192.168.10.34 timed out", "192.168.10.34", "[REDACTED_IP]"),
    ("peer 2001:0db8:85a3:0000:0000:8a2e:0370:7334 reset", "2001:0db8", "[REDACTED_IPV6]"),
    ("Authorization: Bearer abcDEF123456ghिJKL", "abcDEF123456", "[REDACTED_TOKEN]"),
    (
        "jwt=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJ",
        "SflKxwRJ",
        "[REDACTED_JWT]",
    ),
    ("key AKIAIOSFODNN7EXAMPLE leaked", "AKIAIOSFODNN7EXAMPLE", "[REDACTED_AWS_KEY]"),
    ("password=hunter2 in config", "hunter2", "[REDACTED_SECRET]"),
    ("api_key: sk-abc123XYZ mounted", "sk-abc123XYZ", "[REDACTED_SECRET]"),
    # A Telegram bot token as httpx prints it inside an exception's URL.
    (
        "Client error '400 Bad Request' for url 'https://api.telegram.org/bot1890788154:AAHVJEmQlOanKaS9SWyRphHOCwWMp5YsETc/sendMessage'",
        "AAHVJEmQlOanKaS9SWyRphHOCwWMp5YsETc",
        "[REDACTED_BOT_TOKEN]",
    ),
]


@pytest.mark.parametrize("text,leaked,mask", SECRETS)
def test_secret_is_masked(text, leaked, mask):
    out = redact(text)
    assert leaked not in out, f"raw secret survived redaction: {out!r}"
    assert mask in out


def test_redaction_preserves_surrounding_text():
    out = redact("pod billing-api restarted; contact alice@example.com now")
    assert out.startswith("pod billing-api restarted; contact ")
    assert out.endswith(" now")


def test_redaction_is_idempotent():
    text = "login alice@example.com from 10.0.0.5 token=deadbeefcafe"
    once = redact(text)
    twice = redact(once)
    assert once == twice


def test_redact_bundle_masks_every_section_and_keeps_source_health():
    bundle = ContextBundle(
        log_lines=["error for bob@corp.io"],
        metrics=["scrape from 10.1.2.3"],
        k8s_events=["pulled by admin@corp.io"],
        sources_ok=["loki"],
        sources_failed=["prometheus"],
    )
    out = redact_bundle(bundle)
    assert "bob@corp.io" not in out.log_lines[0]
    assert "10.1.2.3" not in out.metrics[0]
    assert "admin@corp.io" not in out.k8s_events[0]
    # Source-health metadata names collectors, not payload — passed through as-is.
    assert out.sources_ok == ["loki"]
    assert out.sources_failed == ["prometheus"]


def test_redacting_formatter_scrubs_exception_messages_and_tracebacks():
    """The token must not survive `logger.exception(...)`, traceback included."""
    import io
    import logging

    from app.logsafe import RedactingFormatter

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(RedactingFormatter("%(levelname)s %(message)s"))
    log = logging.getLogger("test.logsafe")
    log.handlers[:] = [handler]
    log.propagate = False
    log.setLevel(logging.DEBUG)
    try:
        raise RuntimeError("for url 'https://api.telegram.org/bot1890788154:AAHVJEmQlOanKaS9SWyRphHOCwWMp5YsETc/getUpdates'")
    except RuntimeError:
        log.exception("polling error for admin@corp.example")
    out = stream.getvalue()
    assert "AAHVJEmQlOanKaS9SWyRphHOCwWMp5YsETc" not in out
    assert "[REDACTED_BOT_TOKEN]" in out
    assert "admin@corp.example" not in out
    assert "Traceback" in out  # the traceback itself is kept


@pytest.mark.parametrize(
    "text",
    [
        "2026-09-13 20:59:12,781 INFO worker: up",  # clock time is not an address
        "restart at 03:12:45 and again at 23:00:00",
        "sha256:dc2d74b28e4cf8984fa52af1f39bc7c3d9c73760b41a74d629f5d11b1ab28616",
    ],
)
def test_times_and_digests_are_not_ipv6(text):
    assert redact(text) == text


@pytest.mark.parametrize("addr", ["2001:db8::1", "::1", "fe80::a1b2:c3d4", "2001:0db8:85a3:0000:0000:8a2e:0370:7334"])
def test_real_ipv6_forms_are_masked(addr):
    assert addr not in redact(f"peer {addr} reset")


def test_redacting_formatter_keeps_the_timestamp():
    import io
    import logging

    from app.logsafe import RedactingFormatter

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(RedactingFormatter("%(asctime)s %(message)s"))
    log = logging.getLogger("test.logsafe.time")
    log.handlers[:] = [handler]
    log.propagate = False
    log.info("peer %s said hi", "10.1.2.3")
    out = stream.getvalue()
    assert "[REDACTED_IP]" in out and "10.1.2.3" not in out
    assert "REDACTED_IPV6" not in out  # the asctime survived intact
