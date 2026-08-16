"""Telegram allowlist.

Deliberately free of any python-telegram-bot import so the rule can be unit
tested on its own -- this is the only thing standing between a stranger and
your mailbox.
"""

from __future__ import annotations

from dataclasses import dataclass

from bidoo_bot.logging_config import get_logger

logger = get_logger(__name__)

#: Shown to anyone not on the list. Says nothing about the bot or the system.
ACCESS_DENIED_MESSAGE = "Access denied."


@dataclass(frozen=True, slots=True)
class Authorizer:
    """Allows a fixed set of Telegram user ids and nobody else."""

    allowed_user_ids: frozenset[int]

    def is_authorized(self, user_id: int | None) -> bool:
        """Fail closed: an unknown user, or an empty allowlist, means no."""
        if user_id is None or not self.allowed_user_ids:
            return False
        return user_id in self.allowed_user_ids

    def log_denied(self, user_id: int | None, command: str) -> None:
        """Record the attempt. The id is logged so you can allowlist yourself."""
        logger.warning(
            "Denied '%s' from Telegram user id %s (not in TELEGRAM_ALLOWED_USER_IDS)",
            command,
            user_id if user_id is not None else "unknown",
        )
