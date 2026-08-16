"""Chooses the redeem strategy from configuration.

Swapping HTTP for a browser is a one-line config change; nothing in the
application core knows which one is in use.
"""

from __future__ import annotations

from dataclasses import replace

from bidoo_bot.adapters.bidoo.http_redeemer import HttpRedeemer
from bidoo_bot.application.ports import RedeemerPort
from bidoo_bot.config import AppConfig
from bidoo_bot.errors import ConfigError
from bidoo_bot.security import UrlPolicy


def build_redeemer(config: AppConfig, policy: UrlPolicy) -> RedeemerPort:
    """Instantiate the configured redeemer.

    Called lazily, only when an action is actually going to be executed, so a
    dry run never starts a browser.
    """
    strategy = config.redeem.strategy
    if strategy == "http":
        return HttpRedeemer(config.redeem.http, policy)
    if strategy == "playwright":
        from bidoo_bot.adapters.bidoo.playwright_redeemer import PlaywrightRedeemer

        settings = config.redeem.playwright
        # Relative paths in config.yaml are relative to the config file.
        resolved = replace(
            settings,
            user_data_dir=config.resolve(settings.user_data_dir),
            screenshot_dir=(
                config.resolve(settings.screenshot_dir) if settings.screenshot_dir else None
            ),
        )
        return PlaywrightRedeemer(resolved, policy)
    raise ConfigError(f"unknown redeem.strategy '{strategy}'")  # pragma: no cover - validated
