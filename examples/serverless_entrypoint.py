"""Example: driving the core from a serverless HTTP function.

This file is **not** wired into any deployment and ships no provider config on
purpose (see the "Deployment" section of the README). It exists to show that
the application core is already transport agnostic:

    Telegram webhook  ->  HTTP function  ->  RedeemService.run()

The same three lines would work behind Flask, FastAPI, AWS Lambda, or a cron
job. Nothing below reaches into Gmail, Telegram or Bidoo directly.

Before deploying anything, read the "Deployment" section of the README: a
long-lived Gmail refresh token belongs in a secret manager, not in the image.
"""

from __future__ import annotations

import json
import os
from typing import Any

from bidoo_bot.application.redeem import RedeemOptions
from bidoo_bot.config import load_config, load_secrets
from bidoo_bot.container import build_service
from bidoo_bot.errors import BidooBotError
from bidoo_bot.logging_config import configure_logging
from bidoo_bot.reporting import format_report, report_to_dict

# Built once per cold start, reused across invocations.
_CONFIG = load_config(os.environ.get("BIDOO_CONFIG"))
_SECRETS = load_secrets()

configure_logging(
    level=_CONFIG.logging.level,
    fmt="json",  # structured logs are easier to read in a cloud log viewer
    redact_records=_CONFIG.logging.redact,
    secrets=(_SECRETS.telegram_bot_token,),
)


def redeem_now(*, dry_run: bool | None = None) -> dict[str, Any]:
    """Run the use case once and return a JSON-serialisable report."""
    service = build_service(_CONFIG)
    report = service.run(RedeemOptions(dry_run=dry_run))
    return report_to_dict(report)


def handle_telegram_update(update: dict[str, Any]) -> str | None:
    """Minimal webhook handler.

    Real deployments should verify Telegram's secret token header before
    trusting the payload -- see ``setWebhook``'s ``secret_token`` parameter.
    Returns the text to reply with, or ``None`` when the update is ignored.
    """
    message = update.get("message") or {}
    user_id = (message.get("from") or {}).get("id")
    text = str(message.get("text", "")).strip()

    if user_id not in _SECRETS.telegram_allowed_user_ids:
        return "Access denied."
    if not text.startswith("/bidoo"):
        return None

    try:
        service = build_service(_CONFIG)
        report = service.run(RedeemOptions())
    except BidooBotError as exc:
        return f"⚠️ {exc}"
    return format_report(
        report,
        max_detail_lines=_CONFIG.telegram.max_detail_lines,
        show_urls=_CONFIG.telegram.show_urls_in_dry_run,
    )


# --- Provider glue -------------------------------------------------------
# Deliberately left as a sketch: check your provider's current Python runtime
# support and function signature before using any of this.
#
# def main(request):                     # e.g. a GCP HTTP function
#     payload = request.get_json(silent=True) or {}
#     reply = handle_telegram_update(payload)
#     return (json.dumps({"text": reply}), 200, {"Content-Type": "application/json"})


if __name__ == "__main__":
    print(json.dumps(redeem_now(dry_run=True), indent=2, ensure_ascii=False))
