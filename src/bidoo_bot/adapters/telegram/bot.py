"""Telegram interface.

This module is a *thin* adapter: it checks who is asking, calls
:meth:`RedeemService.run`, and renders the result. It contains no parsing, no
Gmail knowledge and no redeem logic -- replacing it with a web UI would not
touch anything else.

The service is built inside the worker thread, once per command, so the Gmail
client is never shared across threads.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass

from telegram import Bot, BotCommand, Update
from telegram.constants import ChatAction
from telegram.error import Conflict, InvalidToken, TelegramError
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes

from bidoo_bot.adapters.telegram.authorization import ACCESS_DENIED_MESSAGE, Authorizer
from bidoo_bot.application.redeem import RedeemOptions, RedeemService
from bidoo_bot.config import AppConfig, Secrets
from bidoo_bot.errors import BidooBotError
from bidoo_bot.logging_config import get_logger, register_secret
from bidoo_bot.models.results import StatusReport
from bidoo_bot.reporting import format_report, format_status

logger = get_logger(__name__)

ServiceFactory = Callable[[], RedeemService]

_TELEGRAM_MAX_CHARS = 4000

START_TEXT = (
    "🎁 bidoo-bot\n\n"
    "I look for Bidoo free-bid emails in your Gmail and redeem them, "
    "but only when you ask me to.\n\n"
    "Send /help to see the commands."
)

HELP_TEXT = (
    "Commands:\n"
    "/bidoo — check the mailbox now and redeem what I find\n"
    "/status — show the current configuration and Gmail connectivity\n"
    "/help — this message\n\n"
    "Nothing runs on a schedule: I only act on /bidoo."
)

_COMMANDS = [
    BotCommand("bidoo", "Check Gmail and redeem free bids"),
    BotCommand("status", "Show configuration and Gmail status"),
    BotCommand("help", "Show the available commands"),
]


class BidooTelegramBot:
    """Command handlers, wired to a service factory."""

    def __init__(
        self,
        *,
        config: AppConfig,
        authorizer: Authorizer,
        service_factory: ServiceFactory,
    ) -> None:
        self._config = config
        self._authorizer = authorizer
        self._service_factory = service_factory
        self._lock = asyncio.Lock()

    # -- handlers -----------------------------------------------------------

    async def start(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update, "/start"):
            return
        await self._reply(update, START_TEXT)

    async def help(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update, "/help"):
            return
        await self._reply(update, HELP_TEXT)

    async def status(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update, "/status"):
            return
        try:
            report = await asyncio.to_thread(self._status_blocking)
        except BidooBotError as exc:
            await self._reply(update, f"⚠️ {exc}")
            return
        await self._reply(update, format_status(report))

    async def bidoo(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update, "/bidoo"):
            return

        if self._lock.locked():
            await self._reply(update, "⏳ A check is already running, hold on.")
            return

        async with self._lock:
            mode = " (dry run)" if self._config.redeem.dry_run else ""
            await self._reply(update, f"🔎 Checking your mailbox{mode}…")
            await self._typing(update)
            try:
                text = await asyncio.to_thread(self._run_blocking)
            except BidooBotError as exc:
                logger.error("Redeem run failed: %s", exc)
                await self._reply(update, f"⚠️ {exc}")
                return
            except Exception:
                logger.exception("Unexpected error during a redeem run")
                await self._reply(update, "⚠️ Something went wrong. Check the logs.")
                return
        await self._reply(update, text)

    # -- blocking work (runs in a worker thread) ----------------------------

    def _run_blocking(self) -> str:
        service = self._service_factory()
        report = service.run(RedeemOptions())
        return format_report(
            report,
            max_detail_lines=self._config.telegram.max_detail_lines,
            show_urls=self._config.telegram.show_urls_in_dry_run,
        )

    def _status_blocking(self) -> StatusReport:
        return self._service_factory().status()

    # -- helpers ------------------------------------------------------------

    async def _guard(self, update: Update, command: str) -> bool:
        """Allowlist check. Denied users get four words and nothing else."""
        user = update.effective_user
        user_id = user.id if user else None
        if self._authorizer.is_authorized(user_id):
            return True
        self._authorizer.log_denied(user_id, command)
        await self._reply(update, ACCESS_DENIED_MESSAGE)
        return False

    @staticmethod
    async def _reply(update: Update, text: str) -> None:
        message = update.effective_message
        if message is None:  # pragma: no cover - edited/channel updates
            return
        for chunk in _chunks(text, _TELEGRAM_MAX_CHARS):
            try:
                # No parse_mode on purpose: email subjects would need escaping
                # and a broken entity would leak as a delivery failure.
                await message.reply_text(chunk, disable_web_page_preview=True)
            except TelegramError as exc:
                logger.error("Could not send a Telegram reply: %s", exc)
                return

    @staticmethod
    async def _typing(update: Update) -> None:
        chat = update.effective_chat
        if chat is None:  # pragma: no cover - defensive
            return
        # Purely cosmetic; a failure here must never affect the command.
        with contextlib.suppress(TelegramError):
            await chat.send_action(ChatAction.TYPING)


def _chunks(text: str, size: int) -> list[str]:
    """Split on line boundaries so a summary never breaks mid-word."""
    if len(text) <= size:
        return [text]
    parts: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > size and current:
            parts.append(current)
            current = ""
        current += line
    if current:
        parts.append(current)
    return parts


async def _post_init(application: Application) -> None:
    try:
        await application.bot.set_my_commands(_COMMANDS)
    except TelegramError as exc:  # pragma: no cover - cosmetic only
        logger.debug("Could not publish the command list: %s", exc)


def build_application(
    *,
    config: AppConfig,
    secrets: Secrets,
    service_factory: ServiceFactory,
) -> Application:
    """Wire the handlers into a python-telegram-bot application."""
    token, allowed_ids = secrets.require_telegram()
    register_secret(token)

    bot = BidooTelegramBot(
        config=config,
        authorizer=Authorizer(allowed_user_ids=allowed_ids),
        service_factory=service_factory,
    )

    application = ApplicationBuilder().token(token).post_init(_post_init).build()
    application.add_handler(CommandHandler("start", bot.start))
    application.add_handler(CommandHandler("help", bot.help))
    application.add_handler(CommandHandler("bidoo", bot.bidoo))
    application.add_handler(CommandHandler("status", bot.status))
    return application


@dataclass(frozen=True, slots=True)
class TelegramSender:
    """Someone who has sent a message to the bot."""

    user_id: int
    name: str = ""


def fetch_recent_user_ids(token: str, *, limit: int = 20) -> list[TelegramSender]:
    """Report the Telegram ids that have messaged the bot recently.

    Solves the chicken-and-egg of the first setup: the bot refuses to start
    without an allowlist, but you need it running to learn your own id. This
    reads the pending updates directly instead, so no third-party "what is my
    id" bot is involved and the token never leaves the process.

    ``getUpdates`` is called without confirming an offset, so the updates stay
    queued and the bot will still see them later.
    """
    register_secret(token)

    async def _read() -> list[TelegramSender]:
        bot = Bot(token)
        async with bot:
            updates = await bot.get_updates(timeout=0, limit=limit)
        senders: dict[int, TelegramSender] = {}
        for update in updates:
            user = update.effective_user
            if user is not None and user.id not in senders:
                senders[user.id] = TelegramSender(user_id=user.id, name=user.full_name or "")
        return list(senders.values())

    try:
        return asyncio.run(_read())
    except InvalidToken as exc:
        raise BidooBotError(
            "Telegram rejected the token. Check TELEGRAM_BOT_TOKEN in your .env."
        ) from exc
    except Conflict as exc:
        raise BidooBotError(
            "Telegram is already delivering updates elsewhere: stop any running "
            "`bidoo-bot bot` (or delete the webhook) and try again."
        ) from exc
    except TelegramError as exc:
        raise BidooBotError(f"could not reach Telegram: {exc}") from exc


def run_bot(*, config: AppConfig, secrets: Secrets, service_factory: ServiceFactory) -> None:
    """Start long polling. Blocks until interrupted."""
    application = build_application(config=config, secrets=secrets, service_factory=service_factory)
    logger.info(
        "Telegram bot started (dry_run=%s, %d allowed user id(s))",
        config.redeem.dry_run,
        len(secrets.telegram_allowed_user_ids),
    )
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
