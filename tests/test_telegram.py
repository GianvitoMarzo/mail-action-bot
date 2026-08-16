"""Telegram interface tests: the allowlist and the command handlers.

The handlers are driven with lightweight stand-ins for ``Update``. Nothing
here talks to the Telegram API.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from bidoo_bot.adapters.telegram.authorization import ACCESS_DENIED_MESSAGE, Authorizer
from bidoo_bot.adapters.telegram.bot import BidooTelegramBot, _chunks
from bidoo_bot.application.redeem import RedeemService
from bidoo_bot.config import AppConfig, Secrets
from bidoo_bot.errors import BidooBotError, ConfigError, MailboxError
from tests.fakes import GOOD_HTML, FakeMailbox, make_email

OWNER = 424242
STRANGER = 999999


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------


def test_only_listed_users_are_authorized() -> None:
    authorizer = Authorizer(allowed_user_ids=frozenset({OWNER}))

    assert authorizer.is_authorized(OWNER)
    assert not authorizer.is_authorized(STRANGER)


def test_missing_user_id_is_refused() -> None:
    assert not Authorizer(allowed_user_ids=frozenset({OWNER})).is_authorized(None)


def test_an_empty_allowlist_authorizes_nobody() -> None:
    """Fail closed: a misconfigured bot must not become an open one."""
    authorizer = Authorizer(allowed_user_ids=frozenset())

    assert not authorizer.is_authorized(OWNER)
    assert not authorizer.is_authorized(None)


def test_secrets_refuse_to_start_a_bot_without_an_allowlist() -> None:
    with pytest.raises(ConfigError, match="TELEGRAM_ALLOWED_USER_IDS"):
        Secrets(telegram_bot_token="123456:token-shaped-value").require_telegram()


def test_secrets_refuse_to_start_a_bot_without_a_token() -> None:
    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN"):
        Secrets(telegram_allowed_user_ids=frozenset({OWNER})).require_telegram()


# ---------------------------------------------------------------------------
# Update stand-ins
# ---------------------------------------------------------------------------


@dataclass
class StubMessage:
    replies: list[str] = field(default_factory=list)

    async def reply_text(self, text: str, **_kwargs: Any) -> None:
        self.replies.append(text)


@dataclass
class StubChat:
    actions: list[str] = field(default_factory=list)

    async def send_action(self, action: Any) -> None:
        self.actions.append(str(action))


@dataclass
class StubUser:
    id: int


@dataclass
class StubUpdate:
    effective_user: StubUser | None
    effective_message: StubMessage = field(default_factory=StubMessage)
    effective_chat: StubChat = field(default_factory=StubChat)

    @property
    def replies(self) -> list[str]:
        return self.effective_message.replies


async def invoke(handler: Any, update: Any) -> None:
    """Call a handler with a stand-in Update. Duck typed on purpose."""
    await handler(update, None)


def make_bot(
    config: AppConfig,
    service: RedeemService,
    *,
    allowed: frozenset[int] = frozenset({OWNER}),
    factory_error: Exception | None = None,
) -> BidooTelegramBot:
    def service_factory() -> RedeemService:
        if factory_error is not None:
            raise factory_error
        return service

    return BidooTelegramBot(
        config=config,
        authorizer=Authorizer(allowed_user_ids=allowed),
        service_factory=service_factory,
    )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def test_stranger_gets_nothing_but_access_denied(
    config: AppConfig, service: RedeemService, mailbox: FakeMailbox
) -> None:
    mailbox.messages = [make_email(html=GOOD_HTML)]
    bot = make_bot(config, service)
    update = StubUpdate(effective_user=StubUser(STRANGER))

    await invoke(bot.bidoo, update)

    assert update.replies == [ACCESS_DENIED_MESSAGE]
    assert mailbox.searches == [], "an unauthorised user must not trigger any work"


@pytest.mark.parametrize("command", ["start", "help", "status", "bidoo"])
async def test_every_command_is_gated(
    config: AppConfig, service: RedeemService, command: str
) -> None:
    bot = make_bot(config, service)
    update = StubUpdate(effective_user=StubUser(STRANGER))

    await invoke(getattr(bot, command), update)

    assert update.replies == [ACCESS_DENIED_MESSAGE]


async def test_access_denied_reveals_nothing_about_the_system() -> None:
    lowered = ACCESS_DENIED_MESSAGE.lower()

    for leak in ("bidoo", "gmail", "telegram", "config", "user", "id"):
        assert leak not in lowered


async def test_start_and_help_answer_the_owner(config: AppConfig, service: RedeemService) -> None:
    bot = make_bot(config, service)
    update = StubUpdate(effective_user=StubUser(OWNER))

    await invoke(bot.start, update)
    await invoke(bot.help, update)

    assert len(update.replies) == 2
    assert "/bidoo" in update.replies[1]


async def test_bidoo_acknowledges_then_summarises(
    config: AppConfig, service: RedeemService, mailbox: FakeMailbox
) -> None:
    mailbox.messages = [make_email(html=GOOD_HTML)]
    bot = make_bot(config, service)
    update = StubUpdate(effective_user=StubUser(OWNER))

    await invoke(bot.bidoo, update)

    assert len(update.replies) == 2
    assert "Checking your mailbox" in update.replies[0]
    assert "dry run" in update.replies[0], "the ack must say when nothing will be executed"
    summary = update.replies[1]
    assert "Bidoo check completed" in summary
    assert "Emails found: 1" in summary


async def test_bidoo_summary_never_contains_the_email_body(
    config: AppConfig, service: RedeemService, mailbox: FakeMailbox
) -> None:
    mailbox.messages = [make_email(html=GOOD_HTML)]
    bot = make_bot(config, service)
    update = StubUpdate(effective_user=StubUser(OWNER))

    await invoke(bot.bidoo, update)

    assert "<html>" not in update.replies[1]
    assert "<a class" not in update.replies[1]


async def test_status_is_rendered(config: AppConfig, service: RedeemService) -> None:
    bot = make_bot(config, service)
    update = StubUpdate(effective_user=StubUser(OWNER))

    await invoke(bot.status, update)

    assert "bidoo-bot status" in update.replies[0]
    assert "allowed domains" in update.replies[0]


async def test_expected_errors_become_a_readable_reply(
    config: AppConfig, service: RedeemService
) -> None:
    bot = make_bot(config, service, factory_error=MailboxError("Gmail is not authorised yet"))
    update = StubUpdate(effective_user=StubUser(OWNER))

    await invoke(bot.bidoo, update)

    assert "Gmail is not authorised yet" in update.replies[-1]


async def test_unexpected_errors_do_not_leak_internals(
    config: AppConfig, service: RedeemService
) -> None:
    bot = make_bot(config, service, factory_error=RuntimeError("psycopg2 connection string leak"))
    update = StubUpdate(effective_user=StubUser(OWNER))

    await invoke(bot.bidoo, update)

    assert update.replies[-1] == "⚠️ Something went wrong. Check the logs."
    assert "psycopg2" not in " ".join(update.replies)


async def test_a_second_bidoo_while_one_runs_is_told_to_wait(
    config: AppConfig, service: RedeemService, mailbox: FakeMailbox
) -> None:
    bot = make_bot(config, service)
    update = StubUpdate(effective_user=StubUser(OWNER))
    await bot._lock.acquire()
    try:
        await invoke(bot.bidoo, update)
    finally:
        bot._lock.release()

    assert "already running" in update.replies[0]
    assert mailbox.searches == []


# ---------------------------------------------------------------------------
# Message chunking
# ---------------------------------------------------------------------------


def test_short_messages_are_not_split() -> None:
    assert _chunks("hello", 100) == ["hello"]


def test_long_messages_are_split_on_line_boundaries() -> None:
    text = "\n".join(f"line {i}" for i in range(500))

    parts = _chunks(text, 200)

    assert len(parts) > 1
    assert all(len(part) <= 200 for part in parts)
    assert "".join(parts) == text


# ---------------------------------------------------------------------------
# First-time setup: discovering your own user id
# ---------------------------------------------------------------------------


def test_the_empty_allowlist_error_says_how_to_fix_it() -> None:
    """Regression: the bot refuses to start without an allowlist, so the error
    must point at the command that finds your id -- otherwise setup dead-ends."""
    with pytest.raises(ConfigError) as excinfo:
        Secrets(telegram_bot_token="123456:token-shaped-value").require_telegram()

    message = str(excinfo.value)
    assert "telegram-whoami" in message
    assert "send any message to your bot" in message


def test_fetch_recent_user_ids_dedupes_and_keeps_order(monkeypatch: pytest.MonkeyPatch) -> None:
    from bidoo_bot.adapters.telegram import bot as bot_module

    @dataclass
    class StubUpdateUser:
        id: int
        full_name: str

    @dataclass
    class StubGetUpdate:
        effective_user: StubUpdateUser | None

    class StubBot:
        def __init__(self, token: str) -> None:
            self.token = token

        async def __aenter__(self) -> StubBot:
            return self

        async def __aexit__(self, *_exc: Any) -> None:
            return None

        async def get_updates(self, **_kwargs: Any) -> list[StubGetUpdate]:
            return [
                StubGetUpdate(StubUpdateUser(OWNER, "Owner")),
                StubGetUpdate(StubUpdateUser(OWNER, "Owner")),  # duplicate
                StubGetUpdate(None),  # no user (e.g. a channel post)
                StubGetUpdate(StubUpdateUser(STRANGER, "Someone Else")),
            ]

    monkeypatch.setattr(bot_module, "Bot", StubBot)

    senders = bot_module.fetch_recent_user_ids("123456:abcdefghijklmnopqrstuvwxyz0123456789")

    assert [s.user_id for s in senders] == [OWNER, STRANGER]
    assert senders[0].name == "Owner"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ("invalid-token", "Check TELEGRAM_BOT_TOKEN"),
        ("conflict", "already delivering updates elsewhere"),
        ("other", "could not reach Telegram"),
    ],
)
def test_fetch_recent_user_ids_translates_telegram_errors(
    monkeypatch: pytest.MonkeyPatch, error: str, expected: str
) -> None:
    from telegram.error import Conflict, InvalidToken, TelegramError

    from bidoo_bot.adapters.telegram import bot as bot_module

    raised = {
        "invalid-token": InvalidToken(),
        "conflict": Conflict("terminated by other getUpdates request"),
        "other": TelegramError("network is down"),
    }[error]

    class ExplodingBot:
        def __init__(self, token: str) -> None:
            pass

        async def __aenter__(self) -> ExplodingBot:
            raise raised

        async def __aexit__(self, *_exc: Any) -> None:
            return None

    monkeypatch.setattr(bot_module, "Bot", ExplodingBot)

    with pytest.raises(BidooBotError, match=expected):
        bot_module.fetch_recent_user_ids("123456:abcdefghijklmnopqrstuvwxyz0123456789")


def test_the_token_is_registered_for_redaction(monkeypatch: pytest.MonkeyPatch) -> None:
    from bidoo_bot.adapters.telegram import bot as bot_module
    from bidoo_bot.logging_config import redact

    token = "7891234567:AAH9xKq2LmNoPqRsTuVwXyZ0123456789abc"

    class StubBot:
        def __init__(self, token: str) -> None:
            pass

        async def __aenter__(self) -> StubBot:
            return self

        async def __aexit__(self, *_exc: Any) -> None:
            return None

        async def get_updates(self, **_kwargs: Any) -> list[Any]:
            return []

    monkeypatch.setattr(bot_module, "Bot", StubBot)
    bot_module.fetch_recent_user_ids(token)

    assert token not in redact(f"using {token}")
