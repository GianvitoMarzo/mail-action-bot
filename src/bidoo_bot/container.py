"""Composition root.

The single place where concrete adapters are chosen and wired into the
application service. Every interface -- CLI, Telegram, a future HTTP function
-- goes through here, which is why adding one does not touch business logic.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from bidoo_bot.adapters.bidoo.factory import build_redeemer
from bidoo_bot.application.ports import MailboxHealth, MailboxPort, RedeemerPort
from bidoo_bot.application.redeem import RedeemService
from bidoo_bot.config import AppConfig
from bidoo_bot.models.email import EmailMessage
from bidoo_bot.parsing.action_parser import ActionParser
from bidoo_bot.security import UrlPolicy


class LazyMailbox:
    """Builds the real mailbox on first use.

    Without this, a missing OAuth token would make *constructing* the service
    fail, so ``status`` -- the command you reach for precisely when something
    is wrong -- could not even print the configuration. Deferring turns that
    into an ordinary reported failure instead of an exception at wiring time.
    """

    def __init__(self, factory: Callable[[], MailboxPort]) -> None:
        self._factory = factory
        self._mailbox: MailboxPort | None = None

    def _target(self) -> MailboxPort:
        if self._mailbox is None:
            self._mailbox = self._factory()
        return self._mailbox

    def search(self, query: str, *, max_results: int) -> Sequence[EmailMessage]:
        return self._target().search(query, max_results=max_results)

    def add_label(self, message_id: str, label: str) -> None:
        self._target().add_label(message_id, label)

    def check_connection(self) -> MailboxHealth:
        return self._target().check_connection()


def build_service(
    config: AppConfig,
    *,
    mailbox: MailboxPort | None = None,
    redeemer_factory: Callable[[], RedeemerPort] | None = None,
    allow_interactive_auth: bool = False,
) -> RedeemService:
    """Build a ready-to-use :class:`RedeemService`.

    ``mailbox`` and ``redeemer_factory`` exist so tests (and any future
    interface) can inject fakes without a Gmail account.
    """
    policy = UrlPolicy(config.security)

    if mailbox is None:

        def build_mailbox() -> MailboxPort:
            # Imported lazily: nothing should need googleapiclient to run
            # `analyze-email` or the test suite.
            from bidoo_bot.adapters.gmail.client import GmailMailbox

            return GmailMailbox.from_config(config, allow_interactive=allow_interactive_auth)

        mailbox = LazyMailbox(build_mailbox)

    if redeemer_factory is None:
        # A callable, not an instance: a dry run must never start a browser.
        def redeemer_factory() -> RedeemerPort:
            return build_redeemer(config, policy)

    return RedeemService(
        config=config,
        mailbox=mailbox,
        parser=ActionParser(config.parser),
        policy=policy,
        redeemer_factory=redeemer_factory,
    )


def build_service_factory(config: AppConfig) -> Callable[[], RedeemService]:
    """A factory that builds a fresh service per call.

    The Telegram bot uses this so each command gets its own Gmail client
    inside its own worker thread instead of sharing one across threads.
    """

    def factory() -> RedeemService:
        return build_service(config)

    return factory
