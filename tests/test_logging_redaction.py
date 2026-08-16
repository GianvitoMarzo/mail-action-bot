"""Nothing secret may reach a log line."""

from __future__ import annotations

import io
import logging

import pytest

from bidoo_bot.logging_config import (
    REDACTED,
    RedactingFilter,
    configure_logging,
    redact,
    redact_email,
    redact_url,
    register_secret,
    short_id,
)

TELEGRAM_TOKEN = "7891234567:AAH9xKq2LmNoPqRsTuVwXyZ0123456789abc"


@pytest.mark.parametrize(
    "secret",
    [
        TELEGRAM_TOKEN,
        "ya29.a0AfB_byC3xample-access-token-value",
        "1//0gExampleRefreshTokenValue",
        "Authorization: Bearer abcdef0123456789",
        "Cookie: session=deadbeefcafebabe",
        'refresh_token": "1-2-3-4-5-6-7-8-9"',
        "client_secret=GOCSPX-abcdefghijklmno",
        "password: hunter2000",
        "utente@example.invalid",
    ],
)
def test_secret_shapes_are_scrubbed(secret: str) -> None:
    cleaned = redact(f"something happened: {secret}")

    assert REDACTED in cleaned
    for fragment in (
        "AAH9xKq2",
        "a0AfB_byC3",
        "abcdef0123456789",
        "deadbeefcafebabe",
        "GOCSPX-abcdefghijklmno",
        "hunter2000",
        "utente@example.invalid",
    ):
        if fragment in secret:
            assert fragment not in cleaned


def test_registered_values_are_scrubbed_verbatim() -> None:
    register_secret("s3cr3t-value-registered-at-runtime")

    assert "s3cr3t" not in redact("token is s3cr3t-value-registered-at-runtime")


def test_ordinary_text_is_left_alone() -> None:
    message = "Found 5 matching emails, 3 redeemed in 2.10s"

    assert redact(message) == message


def test_redact_email_keeps_only_a_hint() -> None:
    assert redact_email("mario.rossi@example.com") == "m***i@example.com"
    assert redact_email("ab@example.com") == "**@example.com"
    assert redact_email("not-an-address") == REDACTED


def test_an_already_masked_address_survives_the_filter() -> None:
    """redact_email() output must not be mangled a second time."""
    masked = redact_email("mario.rossi@example.com")

    assert redact(f"connected as {masked}") == f"connected as {masked}"


def test_redact_url_drops_the_query_but_keeps_the_target() -> None:
    redacted = redact_url("https://www.bidoo.com/promo/riscuoti?token=SUPERSECRET&u=1")

    assert "SUPERSECRET" not in redacted
    assert redacted.startswith("https://www.bidoo.com/promo/riscuoti")
    assert REDACTED in redacted


def test_redact_url_handles_junk() -> None:
    assert redact_url("not a url") == REDACTED
    assert redact_url("") == REDACTED


def test_short_id_is_stable_and_not_reversible() -> None:
    handle = short_id("18f2a9c3b4d5e6f7")

    assert handle == short_id("18f2a9c3b4d5e6f7")
    assert len(handle) == 8
    assert "18f2a9c3b4d5e6f7" not in handle


# ---------------------------------------------------------------------------
# The filter, in a real handler
# ---------------------------------------------------------------------------


def test_the_filter_rewrites_records(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("bidoo_bot.test.filter")
    logger.addFilter(RedactingFilter())

    with caplog.at_level(logging.INFO):
        logger.info("using token %s", TELEGRAM_TOKEN)

    assert "AAH9xKq2" not in caplog.text
    assert REDACTED in caplog.text


def test_configure_logging_installs_the_filter() -> None:
    stream = io.StringIO()
    configure_logging(level="INFO", redact_records=True, secrets=(TELEGRAM_TOKEN,), stream=stream)

    logging.getLogger("bidoo_bot.test.configured").info("token=%s", TELEGRAM_TOKEN)

    output = stream.getvalue()
    assert "AAH9xKq2" not in output
    assert REDACTED in output
    logging.getLogger().handlers.clear()


def test_json_format_is_parseable() -> None:
    import json

    stream = io.StringIO()
    configure_logging(level="INFO", fmt="json", stream=stream)

    logging.getLogger("bidoo_bot.test.json").info("Found %d emails", 5)

    payload = json.loads(stream.getvalue().strip())
    assert payload["message"] == "Found 5 emails"
    assert payload["level"] == "INFO"
    logging.getLogger().handlers.clear()


def test_a_real_run_never_logs_the_url_query(
    caplog: pytest.LogCaptureFixture, service, mailbox
) -> None:
    """End-to-end check on the service's own logging."""
    from tests.fakes import make_email

    mailbox.messages = [
        make_email(
            html='<a class="btn" href="https://www.bidoo.com/promo/riscuoti?token=TOPSECRET99">'
            "Riscuoti la puntata gratis</a>"
        )
    ]

    with caplog.at_level(logging.DEBUG, logger="bidoo_bot"):
        service.run()

    assert "TOPSECRET99" not in caplog.text
    assert "msg-1" not in caplog.text, "the raw Gmail id should not be logged either"
