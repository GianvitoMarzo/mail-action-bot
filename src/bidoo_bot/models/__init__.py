"""Plain data types shared by every layer."""

from bidoo_bot.models.candidate import ActionCandidate, ParseResult, ParseStatus
from bidoo_bot.models.email import EmailMessage
from bidoo_bot.models.results import (
    MessageResult,
    MessageStatus,
    RedeemReport,
    RedemptionAttempt,
    StatusReport,
)

__all__ = [
    "ActionCandidate",
    "EmailMessage",
    "MessageResult",
    "MessageStatus",
    "ParseResult",
    "ParseStatus",
    "RedeemReport",
    "RedemptionAttempt",
    "StatusReport",
]
