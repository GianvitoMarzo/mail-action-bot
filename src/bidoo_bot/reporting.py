"""Rendering results for humans.

Shared by the CLI and the Telegram bot so both show exactly the same thing.
Nothing here reaches the network, and nothing here prints a secret: email
bodies are never included and URLs are only shown in dry-run output, where the
user explicitly asked to see what *would* be clicked.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from bidoo_bot.logging_config import short_id
from bidoo_bot.models.results import (
    ConfirmReport,
    MessageResult,
    MessageStatus,
    RedeemReport,
    StatusReport,
)

_EMOJI: dict[MessageStatus, str] = {
    MessageStatus.REDEEMED: "✅",
    MessageStatus.DRY_RUN: "🧪",
    MessageStatus.MANUAL: "🔗",
    MessageStatus.ALREADY_PROCESSED: "⏭️",
    MessageStatus.UNRECOGNIZED: "⚠️",
    MessageStatus.AMBIGUOUS: "❓",
    MessageStatus.REJECTED: "⛔",
    MessageStatus.FAILED: "❌",
}

_LABEL: dict[MessageStatus, str] = {
    MessageStatus.REDEEMED: "Redeemed",
    MessageStatus.DRY_RUN: "Dry run",
    MessageStatus.MANUAL: "To open",
    MessageStatus.ALREADY_PROCESSED: "Already processed",
    MessageStatus.UNRECOGNIZED: "Unrecognized",
    MessageStatus.AMBIGUOUS: "Ambiguous",
    MessageStatus.REJECTED: "Rejected",
    MessageStatus.FAILED: "Failed",
}

_SUMMARY_ORDER: tuple[MessageStatus, ...] = (
    MessageStatus.REDEEMED,
    MessageStatus.MANUAL,
    MessageStatus.DRY_RUN,
    MessageStatus.ALREADY_PROCESSED,
    MessageStatus.UNRECOGNIZED,
    MessageStatus.AMBIGUOUS,
    MessageStatus.REJECTED,
    MessageStatus.FAILED,
)

_MAX_SUBJECT_CHARS = 60
_MAX_DETAIL_CHARS = 120


def emoji_for(status: MessageStatus) -> str:
    return _EMOJI.get(status, "•")


def label_for(status: MessageStatus) -> str:
    return _LABEL.get(status, status.value.title())


def _truncate(value: str, limit: int) -> str:
    value = " ".join(value.split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def format_result_line(result: MessageResult, *, show_urls: bool = True) -> str:
    """One line (occasionally two) describing what happened to one email."""
    head = f"{emoji_for(result.status)} {label_for(result.status)}"
    if result.subject:
        head += f" — {_truncate(result.subject, _MAX_SUBJECT_CHARS)}"

    candidate = result.candidate
    lines = [head]
    if result.status is MessageStatus.MANUAL and candidate is not None:
        lines.append(f'   "{_truncate(candidate.text, _MAX_SUBJECT_CHARS)}"')
        if show_urls:
            lines.append(f"   {candidate.url}")
    elif result.status is MessageStatus.DRY_RUN and candidate is not None:
        lines.append(f'   text: "{_truncate(candidate.text, _MAX_SUBJECT_CHARS)}"')
        if show_urls:
            lines.append(f"   url: {candidate.url}")
        lines.append(f"   confidence: {candidate.confidence:.2f}")
    elif result.detail and result.status not in (
        MessageStatus.REDEEMED,
        MessageStatus.ALREADY_PROCESSED,
    ):
        lines.append(f"   {_truncate(result.detail, _MAX_DETAIL_CHARS)}")
    return "\n".join(lines)


def format_report(
    report: RedeemReport,
    *,
    max_detail_lines: int = 15,
    show_urls: bool = True,
) -> str:
    """Render a full run as the chat/terminal summary."""
    lines: list[str] = ["🎁 Bidoo check completed"]
    if report.strategy == "manual":
        # dry_run is not a meaningful axis here: this mode never executes
        # anything by design, so the dry-run banner would only confuse.
        if report.count(MessageStatus.MANUAL):
            lines.append("🔗 Open the links yourself, then confirm.")
    elif report.dry_run:
        lines.append("🧪 DRY RUN — no action was executed")
    lines.append("")
    lines.append(f"📧 Emails found: {report.emails_found}")

    counts = report.counts
    for status in _SUMMARY_ORDER:
        count = counts.get(status, 0)
        if count:
            lines.append(f"{emoji_for(status)} {label_for(status)}: {count}")

    if report.errors:
        lines.append("")
        lines.append("Warnings:")
        lines.extend(f"‼️ {_truncate(error, _MAX_DETAIL_CHARS)}" for error in report.errors)

    if report.results and max_detail_lines > 0:
        lines.append("")
        lines.append("Details:")
        shown = report.results[:max_detail_lines]
        lines.extend(format_result_line(result, show_urls=show_urls) for result in shown)
        remaining = len(report.results) - len(shown)
        if remaining > 0:
            lines.append(f"… and {remaining} more")

    if not report.results and not report.errors:
        lines.append("")
        lines.append("Nothing to do: no email matched the configured query.")

    lines.append("")
    lines.append(f"⏱ {report.duration_seconds:.1f}s")
    return "\n".join(lines)


def format_confirm(report: ConfirmReport) -> str:
    """Render the outcome of confirming links you opened yourself."""
    if not report.results:
        return "Nothing to confirm."
    lines: list[str] = []
    if report.confirmed:
        moved = f", {report.trashed} moved to Trash" if report.trashed else ""
        lines.append(f"✅ {report.confirmed} email(s) marked as done{moved}.")
    for result in report.results:
        if not result.ok:
            lines.append(f"❌ {_truncate(result.detail, _MAX_DETAIL_CHARS)}")
    if report.trashed:
        lines.append("")
        lines.append("They are in Gmail's Trash and stay recoverable for 30 days.")
    return "\n".join(lines)


def format_status(status: StatusReport) -> str:
    """Render the ``/status`` snapshot. Contains no secret."""
    mailbox = "✅ connected" if status.mailbox_ok else "❌ not available"
    lines = [
        "ℹ️ bidoo-bot status",
        "",
        f"version: {status.version}",
        f"config: {status.config_path}",
        f"mode: {'🧪 dry-run' if status.dry_run else '🚀 live'}",
        f"strategy: {status.strategy}",
        f"gmail: {mailbox}",
    ]
    if status.mailbox_detail:
        lines.append(f"   {_truncate(status.mailbox_detail, _MAX_DETAIL_CHARS)}")
    lines.extend(
        [
            f"query: {status.query}",
            f"processed label: {status.processed_label}",
            f"allowed domains: {', '.join(status.allowed_domains)}",
        ]
    )
    return "\n".join(lines)


def report_to_dict(report: RedeemReport) -> dict[str, Any]:
    """JSON-friendly view, for ``--json`` and future machine consumers."""
    return {
        "dry_run": report.dry_run,
        "emails_found": report.emails_found,
        "duration_seconds": report.duration_seconds,
        "started_at": report.started_at.isoformat() if report.started_at else None,
        "counts": {status.value: count for status, count in report.counts.items()},
        "errors": list(report.errors),
        "results": [
            {
                # A short hash, not the Gmail id: enough to correlate with the
                # logs, useless to anyone else.
                "message": short_id(result.message_id),
                # The real id, needed by `bidoo-bot confirm`. Only ever printed
                # locally by --json, never sent to a chat.
                "message_id": result.message_id,
                "status": result.status.value,
                "detail": result.detail,
                "subject": result.subject,
                "labels_applied": list(result.labels_applied),
                "candidate": asdict(result.candidate) if result.candidate else None,
            }
            for result in report.results
        ],
    }
