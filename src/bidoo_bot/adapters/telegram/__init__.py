"""Telegram interface. One of several possible front-ends, not the core."""

from bidoo_bot.adapters.telegram.authorization import ACCESS_DENIED_MESSAGE, Authorizer

__all__ = ["ACCESS_DENIED_MESSAGE", "Authorizer"]
