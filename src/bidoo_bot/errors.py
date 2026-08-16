"""Exception hierarchy.

Every failure the bot can produce on purpose derives from :class:`BidooBotError`
so interfaces can show a clean message instead of a traceback.
"""

from __future__ import annotations


class BidooBotError(Exception):
    """Base class for every expected error."""


class ConfigError(BidooBotError):
    """The YAML config or the environment is invalid or incomplete."""


class AuthError(BidooBotError):
    """Gmail OAuth is missing, expired or was refused."""


class MailboxError(BidooBotError):
    """Talking to the mail provider failed."""


class SecurityPolicyError(BidooBotError):
    """An URL was refused by the security policy.

    Raised only where refusing must abort the whole operation; the per-email
    path reports :class:`~bidoo_bot.models.results.MessageStatus.REJECTED`
    instead.
    """


class RedemptionError(BidooBotError):
    """Executing the redeem action failed."""


class DependencyMissingError(BidooBotError):
    """An optional dependency (e.g. Playwright) is required but not installed."""
