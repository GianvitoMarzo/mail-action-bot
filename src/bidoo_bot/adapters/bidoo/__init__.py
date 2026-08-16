"""Bidoo adapters: the strategies that actually execute a redeem action."""

from bidoo_bot.adapters.bidoo.factory import build_redeemer
from bidoo_bot.adapters.bidoo.http_redeemer import HttpRedeemer

__all__ = ["HttpRedeemer", "build_redeemer"]
