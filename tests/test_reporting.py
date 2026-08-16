"""Summary rendering, as seen in Telegram and in the terminal."""

from __future__ import annotations

from datetime import UTC, datetime

from bidoo_bot.models.candidate import ActionCandidate
from bidoo_bot.models.results import MessageResult, MessageStatus, RedeemReport, StatusReport
from bidoo_bot.reporting import format_report, format_status, report_to_dict

CANDIDATE = ActionCandidate(
    text="Riscuoti la tua puntata gratis",
    url="https://www.bidoo.com/promo/riscuoti?token=ABCDEFGH",
    confidence=0.92,
    reason="matched redeem-verb-it(+0.70)",
    signals=("redeem-verb-it",),
)


def result(status: MessageStatus, **kwargs) -> MessageResult:
    return MessageResult(message_id=kwargs.pop("message_id", "m1"), status=status, **kwargs)


def report(*results: MessageResult, dry_run: bool = False, **kwargs) -> RedeemReport:
    return RedeemReport(
        results=results,
        dry_run=dry_run,
        started_at=datetime(2026, 3, 1, 12, 0, tzinfo=UTC),
        duration_seconds=1.5,
        **kwargs,
    )


def test_summary_counts_every_outcome() -> None:
    text = format_report(
        report(
            result(MessageStatus.REDEEMED, detail="free bid redeemed"),
            result(MessageStatus.REDEEMED, message_id="m2"),
            result(MessageStatus.REDEEMED, message_id="m3"),
            result(MessageStatus.ALREADY_PROCESSED, message_id="m4"),
            result(
                MessageStatus.UNRECOGNIZED, message_id="m5", detail="best candidate scored 0.10"
            ),
        )
    )

    assert "🎁 Bidoo check completed" in text
    assert "📧 Emails found: 5" in text
    assert "✅ Redeemed: 3" in text
    assert "⏭️ Already processed: 1" in text
    assert "⚠️ Unrecognized: 1" in text
    assert "Details:" in text


def test_statuses_with_no_occurrences_are_not_listed() -> None:
    text = format_report(report(result(MessageStatus.REDEEMED)))

    assert "Failed" not in text
    assert "Rejected" not in text


def test_dry_run_is_announced_and_shows_the_candidate() -> None:
    text = format_report(
        report(result(MessageStatus.DRY_RUN, candidate=CANDIDATE), dry_run=True),
    )

    assert "DRY RUN" in text
    assert 'text: "Riscuoti la tua puntata gratis"' in text
    assert "url: https://www.bidoo.com/promo/riscuoti?token=ABCDEFGH" in text
    assert "confidence: 0.92" in text


def test_urls_can_be_hidden_in_dry_run() -> None:
    text = format_report(
        report(result(MessageStatus.DRY_RUN, candidate=CANDIDATE), dry_run=True),
        show_urls=False,
    )

    assert "confidence: 0.92" in text
    assert "token=ABCDEFGH" not in text


def test_an_empty_run_says_so() -> None:
    text = format_report(report())

    assert "Emails found: 0" in text
    assert "Nothing to do" in text


def test_run_level_errors_are_shown() -> None:
    text = format_report(report(errors=("Gmail API error while searching messages",)))

    assert "Warnings:" in text
    assert "Gmail API error" in text


def test_details_are_capped() -> None:
    results = tuple(result(MessageStatus.REDEEMED, message_id=f"m{i}") for i in range(30))

    text = format_report(report(*results), max_detail_lines=5)

    assert "… and 25 more" in text


def test_long_subjects_are_truncated() -> None:
    text = format_report(report(result(MessageStatus.REDEEMED, subject="x" * 300)))

    assert "x" * 300 not in text
    assert "…" in text


def test_failure_details_are_visible() -> None:
    text = format_report(
        report(result(MessageStatus.REJECTED, detail="host 'evil.invalid' is not in the allowlist"))
    )

    assert "⛔ Rejected" in text
    assert "evil.invalid" in text


def test_status_rendering_has_no_secrets() -> None:
    text = format_status(
        StatusReport(
            dry_run=True,
            query='label:Bidoo -label:"Bidoo/Processed"',
            strategy="http",
            processed_label="Bidoo/Processed",
            allowed_domains=("bidoo.com",),
            mailbox_ok=True,
            mailbox_detail="connected as m***o@example.invalid",
            config_path="/home/user/config.yaml",
            version="0.1.0",
        )
    )

    assert "dry-run" in text
    assert "bidoo.com" in text
    assert "token" not in text.lower()


def test_json_view_uses_a_hashed_message_handle() -> None:
    payload = report_to_dict(
        report(result(MessageStatus.REDEEMED, message_id="18f2a9c3b4d5", candidate=CANDIDATE))
    )

    assert payload["emails_found"] == 1
    assert payload["counts"]["REDEEMED"] == 1
    assert payload["results"][0]["message"] != "18f2a9c3b4d5"
    assert payload["results"][0]["candidate"]["confidence"] == 0.92
