"""Ports: the interfaces the application core depends on.

The core never imports Gmail, Telegram, httpx or Playwright. It only knows
these two protocols, which is why every use case can be exercised with fakes
and why an adapter can be swapped without touching business logic.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from bidoo_bot.models.candidate import ActionCandidate
from bidoo_bot.models.email import EmailMessage
from bidoo_bot.models.results import RedemptionAttempt


@dataclass(frozen=True, slots=True)
class MailboxHealth:
    ok: bool
    detail: str = ""


@runtime_checkable
class MailboxPort(Protocol):
    """Read-mostly view over a mailbox, plus the labels used for idempotency."""

    def search(self, query: str, *, max_results: int) -> Sequence[EmailMessage]:
        """Return messages matching ``query``, newest first."""
        ...

    def add_label(self, message_id: str, label: str) -> None:
        """Apply ``label`` to a message, creating the label if needed."""
        ...

    def check_connection(self) -> MailboxHealth:
        """Cheap connectivity/credentials probe for ``/status``."""
        ...


@runtime_checkable
class RedeemerPort(Protocol):
    """Executes the action behind an already validated candidate URL."""

    @property
    def name(self) -> str:
        """Short strategy name, shown in reports (``http``, ``playwright``)."""
        ...

    def redeem(self, candidate: ActionCandidate) -> RedemptionAttempt:
        """Perform the action. Must not raise for ordinary failures."""
        ...

    def close(self) -> None:
        """Release any resource held (browser, HTTP client)."""
        ...
