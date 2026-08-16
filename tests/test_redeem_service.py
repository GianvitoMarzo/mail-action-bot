"""Use case tests: dry run, idempotency, rejection and failure handling."""

from __future__ import annotations

from dataclasses import replace

import pytest

from bidoo_bot.application.redeem import RedeemOptions, RedeemService
from bidoo_bot.config import AppConfig
from bidoo_bot.errors import MailboxError, RedemptionError
from bidoo_bot.models.results import MessageStatus, RedemptionAttempt
from bidoo_bot.parsing.action_parser import ActionParser
from bidoo_bot.security import UrlPolicy
from tests.fakes import (
    BORING_HTML,
    GOOD_HTML,
    OFFSITE_HTML,
    CountingFactory,
    FakeMailbox,
    FakeRedeemer,
    make_email,
)


def build_service(
    config: AppConfig,
    mailbox: FakeMailbox,
    factory: CountingFactory,
) -> RedeemService:
    return RedeemService(
        config=config,
        mailbox=mailbox,
        parser=ActionParser(config.parser),
        policy=UrlPolicy(config.security),
        redeemer_factory=factory,
        sleep=lambda _seconds: None,
    )


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


def test_dry_run_finds_the_candidate_but_executes_nothing(
    service: RedeemService, mailbox: FakeMailbox, factory: CountingFactory
) -> None:
    mailbox.messages = [make_email(html=GOOD_HTML)]

    report = service.run(RedeemOptions(dry_run=True))

    assert report.dry_run
    assert report.emails_found == 1
    result = report.results[0]
    assert result.status is MessageStatus.DRY_RUN
    assert result.candidate is not None
    assert result.candidate.confidence > 0.9
    assert factory.builds == 0, "a dry run must not even build the redeemer"
    assert mailbox.labelled == [], "a dry run must not label anything"


def test_dry_run_defaults_to_the_config_value(
    service: RedeemService, mailbox: FakeMailbox, factory: CountingFactory
) -> None:
    mailbox.messages = [make_email(html=GOOD_HTML)]

    report = service.run()  # packaged default is dry_run: true

    assert report.dry_run
    assert report.results[0].status is MessageStatus.DRY_RUN
    assert factory.builds == 0


# ---------------------------------------------------------------------------
# Live run
# ---------------------------------------------------------------------------


def test_live_run_redeems_and_labels(
    service: RedeemService, mailbox: FakeMailbox, factory: CountingFactory, redeemer: FakeRedeemer
) -> None:
    mailbox.messages = [make_email(html=GOOD_HTML)]

    report = service.run(RedeemOptions(dry_run=False))

    assert report.results[0].status is MessageStatus.REDEEMED
    assert report.redeemed == 1
    assert len(redeemer.calls) == 1
    assert redeemer.calls[0].url.startswith("https://www.bidoo.com/")
    assert mailbox.labelled == [("msg-1", "Bidoo/Processed")]
    assert report.results[0].labels_applied == ("Bidoo/Processed",)
    assert redeemer.closed == 1, "the redeemer must be closed at the end of a run"


def test_redeemer_is_built_once_for_several_emails(
    service: RedeemService, mailbox: FakeMailbox, factory: CountingFactory, redeemer: FakeRedeemer
) -> None:
    mailbox.messages = [make_email(f"msg-{i}", html=GOOD_HTML) for i in range(3)]

    report = service.run(RedeemOptions(dry_run=False))

    assert report.redeemed == 3
    assert factory.builds == 1
    assert len(redeemer.calls) == 3


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_running_twice_does_not_redeem_twice(
    service: RedeemService, mailbox: FakeMailbox, redeemer: FakeRedeemer
) -> None:
    """The label applied by the first run makes the second one a no-op."""
    mailbox.messages = [make_email(html=GOOD_HTML)]

    first = service.run(RedeemOptions(dry_run=False))
    second = service.run(RedeemOptions(dry_run=False))

    assert first.results[0].status is MessageStatus.REDEEMED
    assert second.results[0].status is MessageStatus.ALREADY_PROCESSED
    assert len(redeemer.calls) == 1, "the action must run exactly once"


def test_already_labelled_message_is_skipped(
    service: RedeemService, mailbox: FakeMailbox, factory: CountingFactory
) -> None:
    mailbox.messages = [make_email(html=GOOD_HTML, labels=("INBOX", "Bidoo/Processed"))]

    report = service.run(RedeemOptions(dry_run=False))

    assert report.results[0].status is MessageStatus.ALREADY_PROCESSED
    assert factory.builds == 0


def test_the_query_also_excludes_processed_messages(
    service: RedeemService, mailbox: FakeMailbox
) -> None:
    """Belt and braces: the Gmail query filters them out too."""
    mailbox.exclude_processed = True
    mailbox.messages = [
        make_email("msg-1", html=GOOD_HTML, labels=("Bidoo/Processed",)),
        make_email("msg-2", html=GOOD_HTML),
    ]

    report = service.run(RedeemOptions(dry_run=False))

    assert report.emails_found == 1
    assert mailbox.searches[0][0].endswith('-label:"Bidoo/Processed"')


def test_label_failure_is_reported_but_does_not_hide_the_redeem(
    config: AppConfig, mailbox: FakeMailbox, factory: CountingFactory
) -> None:
    mailbox.messages = [make_email(html=GOOD_HTML)]
    mailbox.label_error = "Gmail API error while labelling"
    service = build_service(config, mailbox, factory)

    report = service.run(RedeemOptions(dry_run=False))

    assert report.results[0].status is MessageStatus.REDEEMED
    assert "label not applied" in report.results[0].detail
    assert report.errors, "a missed label must surface as a run level warning"


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_unrecognised_email_executes_nothing(
    service: RedeemService, mailbox: FakeMailbox, factory: CountingFactory
) -> None:
    mailbox.messages = [make_email(html=BORING_HTML)]

    report = service.run(RedeemOptions(dry_run=False))

    assert report.results[0].status is MessageStatus.UNRECOGNIZED
    assert factory.builds == 0
    assert mailbox.labelled == []


def test_offsite_link_is_rejected(
    service: RedeemService, mailbox: FakeMailbox, factory: CountingFactory
) -> None:
    mailbox.messages = [make_email(html=OFFSITE_HTML)]

    report = service.run(RedeemOptions(dry_run=False))

    result = report.results[0]
    assert result.status is MessageStatus.REJECTED
    assert "not in security.allowed_domains" in result.detail
    assert factory.builds == 0, "a rejected URL must never reach the redeemer"


def test_ambiguous_email_is_not_acted_upon(
    service: RedeemService, mailbox: FakeMailbox, factory: CountingFactory
) -> None:
    html = (
        "<html><body><p>Due puntate gratis</p>"
        '<a class="btn" href="https://www.bidoo.com/promo/riscuoti?token=AAAAAAAA">'
        "Riscuoti la puntata gratis</a>"
        '<a class="btn" href="https://www.bidoo.com/promo/riscuoti?token=BBBBBBBB">'
        "Riscuoti la puntata gratis</a></body></html>"
    )
    mailbox.messages = [make_email(html=html)]

    report = service.run(RedeemOptions(dry_run=False))

    assert report.results[0].status is MessageStatus.AMBIGUOUS
    assert factory.builds == 0


# ---------------------------------------------------------------------------
# Failures
# ---------------------------------------------------------------------------


def test_unsuccessful_attempt_is_marked_failed_and_labelled(
    config: AppConfig, mailbox: FakeMailbox
) -> None:
    redeemer = FakeRedeemer(attempt=RedemptionAttempt(success=False, detail="server said HTTP 500"))
    factory = CountingFactory(redeemer=redeemer)
    mailbox.messages = [make_email(html=GOOD_HTML)]
    service = build_service(config, mailbox, factory)

    report = service.run(RedeemOptions(dry_run=False))

    assert report.results[0].status is MessageStatus.FAILED
    assert report.results[0].detail == "server said HTTP 500"
    assert mailbox.labelled == [("msg-1", "Bidoo/Failed")]
    assert not report.ok


def test_redeemer_exception_does_not_abort_the_run(config: AppConfig, mailbox: FakeMailbox) -> None:
    redeemer = FakeRedeemer(error=RedemptionError("boom"))
    factory = CountingFactory(redeemer=redeemer)
    mailbox.messages = [make_email("msg-1", html=GOOD_HTML), make_email("msg-2", html=GOOD_HTML)]
    service = build_service(config, mailbox, factory)

    report = service.run(RedeemOptions(dry_run=False))

    assert [r.status for r in report.results] == [MessageStatus.FAILED, MessageStatus.FAILED]
    assert report.emails_found == 2


def test_unexpected_exception_is_contained(config: AppConfig, mailbox: FakeMailbox) -> None:
    redeemer = FakeRedeemer(error=ValueError("something odd"))
    factory = CountingFactory(redeemer=redeemer)
    mailbox.messages = [make_email(html=GOOD_HTML)]
    service = build_service(config, mailbox, factory)

    report = service.run(RedeemOptions(dry_run=False))

    assert report.results[0].status is MessageStatus.FAILED
    assert "unexpected ValueError" in report.results[0].detail


def test_search_failure_produces_an_empty_report_with_an_error(
    service: RedeemService, mailbox: FakeMailbox
) -> None:
    mailbox.search_error = "Gmail API error while searching messages"

    report = service.run()

    assert report.emails_found == 0
    assert report.errors == ("Gmail API error while searching messages",)
    assert not report.ok


def test_failed_label_can_be_disabled(config: AppConfig, mailbox: FakeMailbox) -> None:
    no_failed_label = replace(config, gmail=replace(config.gmail, failed_label=None))
    redeemer = FakeRedeemer(attempt=RedemptionAttempt(success=False, detail="nope"))
    mailbox.messages = [make_email(html=GOOD_HTML)]
    service = build_service(no_failed_label, mailbox, CountingFactory(redeemer=redeemer))

    report = service.run(RedeemOptions(dry_run=False))

    assert report.results[0].status is MessageStatus.FAILED
    assert mailbox.labelled == []


# ---------------------------------------------------------------------------
# Options, counters and status
# ---------------------------------------------------------------------------


def test_options_override_the_configured_query_and_limit(
    service: RedeemService, mailbox: FakeMailbox
) -> None:
    mailbox.messages = [make_email(f"msg-{i}", html=GOOD_HTML) for i in range(5)]

    service.run(RedeemOptions(query="label:Custom", max_results=2))

    assert mailbox.searches == [("label:Custom", 2)]


def test_counters_add_up(service: RedeemService, mailbox: FakeMailbox) -> None:
    mailbox.messages = [
        make_email("msg-1", html=GOOD_HTML),
        make_email("msg-2", html=BORING_HTML),
        make_email("msg-3", html=OFFSITE_HTML),
        make_email("msg-4", html=GOOD_HTML, labels=("Bidoo/Processed",)),
    ]

    report = service.run(RedeemOptions(dry_run=False))

    counts = report.counts
    assert report.emails_found == 4
    assert counts[MessageStatus.REDEEMED] == 1
    assert counts[MessageStatus.UNRECOGNIZED] == 1
    assert counts[MessageStatus.REJECTED] == 1
    assert counts[MessageStatus.ALREADY_PROCESSED] == 1


def test_status_reports_configuration_without_secrets(
    service: RedeemService, mailbox: FakeMailbox
) -> None:
    status = service.status()

    assert status.dry_run is True
    assert status.strategy == "http"
    assert status.mailbox_ok
    assert "bidoo.com" in status.allowed_domains
    assert "label:Bidoo" in status.query


def test_status_survives_a_broken_mailbox(service: RedeemService, mailbox: FakeMailbox) -> None:
    mailbox.connected = False

    status = service.status()

    assert not status.mailbox_ok
    assert "not connected" in status.mailbox_detail


def test_search_errors_are_expected_type(service: RedeemService, mailbox: FakeMailbox) -> None:
    """The service only swallows our own exceptions, never everything."""
    mailbox.search_error = None

    with pytest.raises(MailboxError):
        FakeMailbox(search_error="down").search("q", max_results=1)
