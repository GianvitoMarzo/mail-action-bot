"""Command line interface.

Uses exactly the same application service as the Telegram bot -- the CLI is
just another caller of :meth:`RedeemService.run`. No logic lives here.

    python -m bidoo_bot --help
    python -m bidoo_bot redeem --dry-run
    python -m bidoo_bot analyze-email tests/fixtures/free_bid_it.html
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TextIO

from bidoo_bot import __version__
from bidoo_bot.application.redeem import RedeemOptions
from bidoo_bot.config import AppConfig, dry_run_from_env, load_config, load_secrets
from bidoo_bot.container import build_service, build_service_factory
from bidoo_bot.errors import BidooBotError
from bidoo_bot.logging_config import configure_logging, get_logger
from bidoo_bot.models.candidate import ParseStatus
from bidoo_bot.parsing.action_parser import ActionParser
from bidoo_bot.parsing.eml import load_email_file
from bidoo_bot.reporting import format_report, format_status, report_to_dict
from bidoo_bot.security import UrlPolicy

logger = get_logger(__name__)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NO_CANDIDATE = 3

DEFAULT_LOGIN_URL = "https://www.bidoo.com/"


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bidoo-bot",
        description=(
            "Check Gmail for Bidoo free-bid emails and redeem them. Runs only when you ask it to."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  bidoo-bot redeem --dry-run        analyse without executing anything\n"
            "  bidoo-bot analyze-email mail.eml  see what the parser finds in a saved email\n"
            "  bidoo-bot bot                     start the Telegram bot (long polling)\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"bidoo-bot {__version__}")
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=None,
        help="path to config.yaml (default: ./config.yaml)",
    )
    parser.add_argument(
        "-v", "--verbose", action="count", default=0, help="more logging (-vv for debug)"
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="only log warnings and errors")
    parser.add_argument(
        "--log-format", choices=("text", "json"), default=None, help="override logging.format"
    )

    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    redeem = subparsers.add_parser(
        "redeem", help="check the mailbox and redeem the free bids found"
    )
    redeem.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="analyse only (--no-dry-run to actually execute); defaults to redeem.dry_run",
    )
    redeem.add_argument("--max-results", type=int, default=None, help="override gmail.max_results")
    redeem.add_argument("--query", default=None, help="override the Gmail query for this run")
    redeem.add_argument("--json", action="store_true", help="print the report as JSON")
    redeem.set_defaults(func=cmd_redeem)

    analyze = subparsers.add_parser(
        "analyze-email",
        help="run the parser against a saved .eml/.html file and show the candidates",
    )
    analyze.add_argument("file", type=Path, help="path to a .eml or .html file")
    analyze.add_argument("--json", action="store_true", help="print the analysis as JSON")
    analyze.add_argument(
        "--all", action="store_true", help="list every link, not just the ranked candidates"
    )
    analyze.set_defaults(func=cmd_analyze_email)

    status = subparsers.add_parser("status", help="show the configuration and Gmail connectivity")
    status.add_argument("--json", action="store_true", help="print the status as JSON")
    status.set_defaults(func=cmd_status)

    bot = subparsers.add_parser("bot", help="run the Telegram bot (long polling)")
    bot.set_defaults(func=cmd_bot)

    auth = subparsers.add_parser("gmail-auth", help="run the Gmail OAuth flow once, interactively")
    auth.set_defaults(func=cmd_gmail_auth)

    login = subparsers.add_parser(
        "browser-login",
        help="open a visible browser on the Playwright profile so you can sign in by hand",
    )
    login.add_argument("--url", default=DEFAULT_LOGIN_URL, help="page to open")
    login.set_defaults(func=cmd_browser_login)

    check = subparsers.add_parser("check-config", help="validate the configuration and exit")
    check.set_defaults(func=cmd_check_config)

    return parser


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_redeem(args: argparse.Namespace, config: AppConfig, out: TextIO) -> int:
    dry_run = args.dry_run if args.dry_run is not None else dry_run_from_env()
    service = build_service(config)
    report = service.run(
        RedeemOptions(dry_run=dry_run, max_results=args.max_results, query=args.query)
    )
    if args.json:
        json.dump(report_to_dict(report), out, indent=2, ensure_ascii=False)
        out.write("\n")
    else:
        out.write(format_report(report, max_detail_lines=100) + "\n")
    return EXIT_OK if report.ok else EXIT_ERROR


def cmd_analyze_email(args: argparse.Namespace, config: AppConfig, out: TextIO) -> int:
    """Offline parser inspection -- no Gmail, no network, nothing executed."""
    message = load_email_file(args.file)
    parser = ActionParser(config.parser)
    policy = UrlPolicy(config.security)
    result = parser.parse(message.html, text_body=message.text)

    decisions = {c.url: policy.check(c.url) for c in result.candidates}
    allowed_best = (
        result.best is not None
        and decisions.get(result.best.url, policy.check(result.best.url)).allowed
    )

    if args.json:
        payload = {
            "file": str(args.file),
            "subject": message.subject,
            "status": result.status.value,
            "detail": result.detail,
            "candidates": [
                {
                    "text": candidate.text,
                    "url": candidate.url,
                    "confidence": candidate.confidence,
                    "reason": candidate.reason,
                    "signals": list(candidate.signals),
                    "allowed": decisions[candidate.url].allowed,
                    "policy": decisions[candidate.url].reason,
                    "best": result.best is not None and candidate.url == result.best.url,
                }
                for candidate in result.candidates
            ],
        }
        json.dump(payload, out, indent=2, ensure_ascii=False)
        out.write("\n")
        return EXIT_OK if (result.ok and allowed_best) else EXIT_NO_CANDIDATE

    out.write(f"File:    {args.file}\n")
    out.write(f"Subject: {message.subject or '(none)'}\n")
    out.write(f"Body:    {'html' if message.html.strip() else 'plain text'}\n")
    out.write(f"Status:  {result.status.value}\n")
    out.write(f"Detail:  {result.detail}\n\n")

    if result.best is not None:
        decision = decisions.get(result.best.url) or policy.check(result.best.url)
        verdict = "✅ allowed" if decision.allowed else "⛔ REJECTED"
        out.write("Best candidate:\n")
        out.write(f'  text:       "{result.best.text}"\n')
        out.write(f"  url:        {result.best.url}\n")
        out.write(f"  confidence: {result.best.confidence:.2f}\n")
        out.write(f"  reason:     {result.best.reason}\n")
        out.write(f"  policy:     {verdict} — {decision.reason}\n\n")

    if result.candidates:
        out.write(f"Ranked candidates ({len(result.candidates)}):\n")
        for position, candidate in enumerate(result.candidates, start=1):
            mark = "✅" if decisions[candidate.url].allowed else "⛔"
            out.write(f'  {position:>2}. {candidate.confidence:.2f} {mark} "{candidate.text}"\n')
            out.write(f"      {candidate.url}\n")
            if args.all:
                out.write(f"      {candidate.reason}\n")

    if result.status is ParseStatus.OK and not allowed_best:
        out.write("\n⛔ The best candidate is NOT on the allowlist: nothing would be executed.\n")
    elif result.ok:
        out.write("\n✅ This email would be redeemed (subject to dry-run mode).\n")
    else:
        out.write("\n⚠️ No action would be taken for this email.\n")

    return EXIT_OK if (result.ok and allowed_best) else EXIT_NO_CANDIDATE


def cmd_status(args: argparse.Namespace, config: AppConfig, out: TextIO) -> int:
    service = build_service(config)
    status = service.status()
    if args.json:
        json.dump(
            {
                "version": status.version,
                "config": status.config_path,
                "dry_run": status.dry_run,
                "strategy": status.strategy,
                "query": status.query,
                "processed_label": status.processed_label,
                "allowed_domains": list(status.allowed_domains),
                "mailbox_ok": status.mailbox_ok,
                "mailbox_detail": status.mailbox_detail,
            },
            out,
            indent=2,
            ensure_ascii=False,
        )
        out.write("\n")
    else:
        out.write(format_status(status) + "\n")
    return EXIT_OK if status.mailbox_ok else EXIT_ERROR


def cmd_bot(_args: argparse.Namespace, config: AppConfig, out: TextIO) -> int:
    from bidoo_bot.adapters.telegram.bot import run_bot

    secrets = load_secrets()
    secrets.require_telegram()  # fail fast, with a readable message
    if config.redeem.dry_run:
        out.write("Starting in DRY RUN mode: /bidoo will analyse but not execute.\n")
    run_bot(config=config, secrets=secrets, service_factory=build_service_factory(config))
    return EXIT_OK


def cmd_gmail_auth(_args: argparse.Namespace, config: AppConfig, out: TextIO) -> int:
    service = build_service(config, allow_interactive_auth=True)
    status = service.status()
    if not status.mailbox_ok:
        out.write(f"Gmail is still not reachable: {status.mailbox_detail}\n")
        return EXIT_ERROR
    out.write(f"Gmail authorised — {status.mailbox_detail}\n")
    out.write(f"Token cached at {config.resolve(config.gmail.token_file)} (keep it out of git).\n")
    return EXIT_OK


def cmd_browser_login(args: argparse.Namespace, config: AppConfig, out: TextIO) -> int:
    from bidoo_bot.adapters.bidoo.playwright_redeemer import open_login_browser

    settings = config.redeem.playwright
    from dataclasses import replace

    resolved = replace(settings, user_data_dir=config.resolve(settings.user_data_dir))
    out.write(
        "A browser window will open. Sign in yourself — bidoo-bot never types "
        "credentials — then close the window.\n"
    )
    open_login_browser(resolved, args.url)
    out.write(f"Session saved in {resolved.user_data_dir}\n")
    return EXIT_OK


def cmd_check_config(_args: argparse.Namespace, config: AppConfig, out: TextIO) -> int:
    out.write(f"Config OK: {config.source_path or '(packaged defaults)'}\n")
    out.write(f"  query:            {config.gmail.effective_query()}\n")
    out.write(f"  processed label:  {config.gmail.processed_label}\n")
    out.write(f"  failed label:     {config.gmail.failed_label or '(disabled)'}\n")
    out.write(f"  allowed domains:  {', '.join(config.security.allowed_domains)}\n")
    out.write(f"  strategy:         {config.redeem.strategy}\n")
    out.write(f"  dry run:          {config.redeem.dry_run}\n")
    out.write(f"  min confidence:   {config.parser.min_confidence}\n")
    out.write(f"  parser signals:   {len(config.parser.signals)}\n")
    out.write(f"  credentials file: {config.resolve(config.gmail.credentials_file)}\n")
    out.write(f"  token file:       {config.resolve(config.gmail.token_file)}\n")
    return EXIT_OK


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _log_level(args: argparse.Namespace, config: AppConfig) -> str:
    if args.quiet:
        return "WARNING"
    if args.verbose >= 2:
        return "DEBUG"
    if args.verbose == 1:
        return "INFO"
    return config.logging.level


def main(argv: list[str] | None = None, *, out: TextIO | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    stream = out or sys.stdout

    if getattr(args, "func", None) is None:
        parser.print_help(stream)
        return EXIT_OK

    try:
        config = load_config(args.config)
    except BidooBotError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    # Secrets are loaded even for commands that do not need them, so their
    # values can be registered with the log redactor before anything runs.
    secrets = load_secrets()
    configure_logging(
        level=_log_level(args, config),
        fmt=args.log_format or config.logging.format,
        redact_records=config.logging.redact,
        secrets=(secrets.telegram_bot_token,),
    )

    try:
        return int(args.func(args, config, stream))
    except BidooBotError as exc:
        logger.debug("Command failed", exc_info=True)
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:  # pragma: no cover - interactive
        print("Interrupted.", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
