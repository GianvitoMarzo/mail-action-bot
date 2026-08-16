"""Types produced by the email HTML parser."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class ParseStatus(StrEnum):
    """Outcome of trying to find *the* actionable link inside an email."""

    OK = "OK"
    """A single candidate stands out with enough confidence."""

    NO_BODY = "NO_BODY"
    """The message carries no usable body."""

    NO_LINKS = "NO_LINKS"
    """The body has no usable ``http(s)`` links at all."""

    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    """Links exist but none scores above ``min_confidence``."""

    AMBIGUOUS = "AMBIGUOUS"
    """Two or more distinct URLs score too closely to pick one safely."""


@dataclass(frozen=True, slots=True)
class ActionCandidate:
    """A link that might be the "redeem your free bid" button."""

    text: str
    """Visible link text (or image alt / aria-label when there is none)."""

    url: str
    confidence: float
    """0.0 - 1.0. Combination of the matched signals, see ``reason``."""

    reason: str
    """Human readable explanation of how ``confidence`` was reached."""

    signals: tuple[str, ...] = field(default_factory=tuple)
    """Names of the rules that matched, e.g. ``("redeem-verb-it", "bidoo-domain")``."""

    position: int = 0
    """Index of the ``<a>`` element in document order, used as a tie-breaker."""

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return f"{self.confidence:.2f} {self.text!r} -> {self.url}"


@dataclass(frozen=True, slots=True)
class ParseResult:
    """Everything the parser learned about one email body."""

    status: ParseStatus
    detail: str = ""
    best: ActionCandidate | None = None
    candidates: tuple[ActionCandidate, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.status is ParseStatus.OK and self.best is not None
