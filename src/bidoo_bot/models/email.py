"""Provider-agnostic representation of an email message.

The application core only ever sees this type -- never a Gmail API payload.
That is what makes the use case testable without credentials and reusable
behind a different mail provider.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class EmailMessage:
    """A single email, reduced to what the redeem use case actually needs."""

    id: str
    """Provider-side message id (Gmail message id)."""

    subject: str = ""
    sender: str = ""
    received_at: datetime | None = None
    html: str = ""
    """HTML body, empty when the message only has a plain-text part."""

    text: str = ""
    """Plain-text body, used as a fallback when ``html`` is empty."""

    labels: tuple[str, ...] = field(default_factory=tuple)
    """Label *names* (not Gmail label ids) currently applied to the message."""

    @property
    def has_body(self) -> bool:
        return bool(self.html.strip() or self.text.strip())
