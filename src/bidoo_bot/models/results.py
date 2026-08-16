"""Result types returned by the application core.

These are the only things an interface (Telegram, CLI, an HTTP function, ...)
needs to know how to render.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from bidoo_bot.models.candidate import ActionCandidate


class MessageStatus(StrEnum):
    """What happened to a single email."""

    REDEEMED = "REDEEMED"
    """The action was executed and the site accepted it."""

    DRY_RUN = "DRY_RUN"
    """A candidate was found and validated, but nothing was executed."""

    ALREADY_PROCESSED = "ALREADY_PROCESSED"
    """The message already carries the "processed" label."""

    UNRECOGNIZED = "UNRECOGNIZED"
    """No link could be identified with enough confidence."""

    AMBIGUOUS = "AMBIGUOUS"
    """Several links were equally plausible; refusing to guess."""

    REJECTED = "REJECTED"
    """A candidate was found but its URL failed the security policy."""

    FAILED = "FAILED"
    """The action was attempted and did not succeed."""

    @property
    def is_success(self) -> bool:
        return self is MessageStatus.REDEEMED


@dataclass(frozen=True, slots=True)
class RedemptionAttempt:
    """Outcome of handing one candidate to a redeemer implementation."""

    success: bool
    detail: str = ""
    status_code: int | None = None
    final_url: str | None = None
    """Where the request ended up, already domain-checked. May be redacted."""


@dataclass(frozen=True, slots=True)
class MessageResult:
    """Per-email outcome, safe to render in a chat message."""

    message_id: str
    status: MessageStatus
    detail: str = ""
    subject: str = ""
    candidate: ActionCandidate | None = None
    labels_applied: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class RedeemReport:
    """Aggregate result of one ``redeem`` run."""

    results: tuple[MessageResult, ...] = field(default_factory=tuple)
    dry_run: bool = False
    query: str = ""
    started_at: datetime | None = None
    duration_seconds: float = 0.0
    errors: tuple[str, ...] = field(default_factory=tuple)
    """Run-level failures (auth, network, ...) that are not tied to one email."""

    @property
    def emails_found(self) -> int:
        return len(self.results)

    @property
    def counts(self) -> dict[MessageStatus, int]:
        counter: Counter[MessageStatus] = Counter(r.status for r in self.results)
        return dict(counter)

    def count(self, status: MessageStatus) -> int:
        return sum(1 for r in self.results if r.status is status)

    @property
    def redeemed(self) -> int:
        return self.count(MessageStatus.REDEEMED)

    @property
    def failed(self) -> int:
        return self.count(MessageStatus.FAILED)

    @property
    def ok(self) -> bool:
        """True when nothing went wrong (candidates may still be unrecognized)."""
        return not self.errors and self.failed == 0


@dataclass(frozen=True, slots=True)
class StatusReport:
    """Non-sensitive snapshot of how the bot is configured, for ``/status``."""

    dry_run: bool
    query: str
    strategy: str
    processed_label: str
    allowed_domains: tuple[str, ...]
    mailbox_ok: bool
    mailbox_detail: str = ""
    config_path: str = ""
    version: str = ""
