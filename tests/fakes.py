"""In-memory adapters used by the tests.

The whole point of the ports in :mod:`bidoo_bot.application.ports` is that the
use case can be exercised with these instead of Gmail, Bidoo and Telegram. No
test in this suite touches the network or needs a credential.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace

from bidoo_bot.application.ports import MailboxHealth
from bidoo_bot.errors import MailboxError
from bidoo_bot.models.candidate import ActionCandidate
from bidoo_bot.models.email import EmailMessage
from bidoo_bot.models.results import RedemptionAttempt


@dataclass
class FakeMailbox:
    """A mailbox backed by a list, with Gmail-like labelling semantics."""

    messages: list[EmailMessage] = field(default_factory=list)
    search_error: str | None = None
    label_error: str | None = None
    connected: bool = True
    #: When true, behaves like the real query filter and hides labelled mails.
    exclude_processed: bool = False
    processed_label: str = "Bidoo/Processed"

    searches: list[tuple[str, int]] = field(default_factory=list)
    labelled: list[tuple[str, str]] = field(default_factory=list)

    def search(self, query: str, *, max_results: int) -> Sequence[EmailMessage]:
        self.searches.append((query, max_results))
        if self.search_error:
            raise MailboxError(self.search_error)
        found = self.messages
        if self.exclude_processed:
            found = [m for m in found if self.processed_label not in m.labels]
        return found[:max_results]

    def add_label(self, message_id: str, label: str) -> None:
        if self.label_error:
            raise MailboxError(self.label_error)
        self.labelled.append((message_id, label))
        # Mirror Gmail: the label sticks, so the next run sees it.
        for index, message in enumerate(self.messages):
            if message.id == message_id and label not in message.labels:
                self.messages[index] = replace(message, labels=(*message.labels, label))

    def check_connection(self) -> MailboxHealth:
        if not self.connected:
            raise MailboxError("not connected")
        return MailboxHealth(ok=True, detail="connected as f***e@example.invalid")


@dataclass
class FakeRedeemer:
    """Records what it was asked to redeem and returns a canned outcome."""

    name: str = "fake"
    attempt: RedemptionAttempt = field(
        default_factory=lambda: RedemptionAttempt(success=True, detail="free bid redeemed")
    )
    error: Exception | None = None
    calls: list[ActionCandidate] = field(default_factory=list)
    closed: int = 0

    def redeem(self, candidate: ActionCandidate) -> RedemptionAttempt:
        self.calls.append(candidate)
        if self.error is not None:
            raise self.error
        return self.attempt

    def close(self) -> None:
        self.closed += 1


@dataclass
class CountingFactory:
    """Wraps a redeemer so a test can assert it was built lazily (or not)."""

    redeemer: FakeRedeemer
    builds: int = 0
    error: Exception | None = None

    def __call__(self) -> FakeRedeemer:
        self.builds += 1
        if self.error is not None:
            raise self.error
        return self.redeemer


def make_email(
    message_id: str = "msg-1",
    *,
    html: str = "",
    text: str = "",
    subject: str = "Una puntata gratis per te",
    labels: tuple[str, ...] = (),
) -> EmailMessage:
    return EmailMessage(
        id=message_id,
        subject=subject,
        sender="Bidoo <noreply@example-bidoo.invalid>",
        html=html,
        text=text,
        labels=labels,
    )


#: A minimal body whose single link scores well above the default threshold.
GOOD_HTML = (
    "<html><body><p>Hai una <b>puntata gratis</b>!</p>"
    '<a class="btn" href="https://www.bidoo.com/promo/riscuoti?token=ABCDEFGH">'
    "Riscuoti la tua puntata gratis</a></body></html>"
)

#: A body with nothing actionable in it.
BORING_HTML = (
    "<html><body><p>Le aste della settimana</p>"
    '<a href="https://www.bidoo.com/aste">Vedi le aste</a></body></html>'
)

#: A convincing button pointing outside the allowlist.
OFFSITE_HTML = (
    "<html><body><p>Hai una <b>puntata gratis</b>!</p>"
    '<a class="btn" href="https://not-bidoo.example-phish.invalid/claim?token=ABCDEFGH">'
    "Riscuoti la tua puntata gratis</a></body></html>"
)
