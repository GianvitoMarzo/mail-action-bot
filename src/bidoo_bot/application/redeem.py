"""The one and only use case: find free-bid emails and redeem them.

Every interface (CLI, Telegram, a future HTTP function) calls
:meth:`RedeemService.run`. There is no business logic anywhere else.

Decision flow per email::

    already labelled processed?  -> ALREADY_PROCESSED
    parser finds no good link?   -> UNRECOGNIZED / AMBIGUOUS
    URL fails the allowlist?     -> REJECTED
    strategy is manual?          -> MANUAL (handed to you; confirm() finishes it)
    dry run?                     -> DRY_RUN
    otherwise                    -> execute, then label the message
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from bidoo_bot.application.ports import MailboxHealth, MailboxPort, RedeemerPort
from bidoo_bot.config import AppConfig
from bidoo_bot.errors import BidooBotError
from bidoo_bot.logging_config import get_logger, redact_url, short_id
from bidoo_bot.models.candidate import ActionCandidate, ParseStatus
from bidoo_bot.models.email import EmailMessage
from bidoo_bot.models.results import (
    ConfirmReport,
    ConfirmResult,
    MessageResult,
    MessageStatus,
    RedeemReport,
    StatusReport,
)
from bidoo_bot.parsing.action_parser import ActionParser
from bidoo_bot.security import UrlPolicy

logger = get_logger(__name__)

#: Maps a non-OK parse status to the reported message status.
_PARSE_STATUS_MAP: dict[ParseStatus, MessageStatus] = {
    ParseStatus.NO_BODY: MessageStatus.UNRECOGNIZED,
    ParseStatus.NO_LINKS: MessageStatus.UNRECOGNIZED,
    ParseStatus.LOW_CONFIDENCE: MessageStatus.UNRECOGNIZED,
    ParseStatus.AMBIGUOUS: MessageStatus.AMBIGUOUS,
}


@dataclass(frozen=True, slots=True)
class RedeemOptions:
    """Per-invocation overrides. ``None`` means "use the config value"."""

    dry_run: bool | None = None
    max_results: int | None = None
    query: str | None = None


@dataclass(slots=True)
class _RunContext:
    """Mutable state for a single run, so nothing lives on the service."""

    dry_run: bool
    redeemer: RedeemerPort | None = None
    warnings: list[str] = field(default_factory=list)


class RedeemService:
    """Application service. Transport agnostic, synchronous, no global state."""

    def __init__(
        self,
        *,
        config: AppConfig,
        mailbox: MailboxPort,
        parser: ActionParser,
        policy: UrlPolicy,
        redeemer_factory: Callable[[], RedeemerPort],
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._config = config
        self._mailbox = mailbox
        self._parser = parser
        self._policy = policy
        self._redeemer_factory = redeemer_factory
        self._sleep = sleep
        self._clock = clock

    # -- use case -----------------------------------------------------------

    def run(self, options: RedeemOptions | None = None) -> RedeemReport:
        """Execute one full check. Never raises for expected failures."""
        options = options or RedeemOptions()
        dry_run = self._config.redeem.dry_run if options.dry_run is None else options.dry_run
        max_results = options.max_results or self._config.gmail.max_results
        query = options.query or self._config.gmail.effective_query()

        started_at = self._clock()
        started_monotonic = time.monotonic()
        logger.info(
            "Starting redeem run (dry_run=%s, strategy=%s, max_results=%d)",
            dry_run,
            self._config.redeem.strategy,
            max_results,
        )
        logger.debug("Gmail query: %s", query)

        try:
            messages = list(self._mailbox.search(query, max_results=max_results))
        except BidooBotError as exc:
            logger.error("Mailbox search failed: %s", exc)
            return RedeemReport(
                dry_run=dry_run,
                query=query,
                started_at=started_at,
                duration_seconds=round(time.monotonic() - started_monotonic, 3),
                errors=(str(exc),),
            )

        logger.info("Found %d matching email(s)", len(messages))

        context = _RunContext(dry_run=dry_run)
        results: list[MessageResult] = []
        try:
            for message in messages:
                if results and self._should_delay(results[-1]):
                    self._sleep(self._config.redeem.delay_between_actions_seconds)
                results.append(self._process(message, context))
        finally:
            self._close(context)

        report = RedeemReport(
            results=tuple(results),
            dry_run=dry_run,
            strategy=self._config.redeem.strategy,
            query=query,
            started_at=started_at,
            duration_seconds=round(time.monotonic() - started_monotonic, 3),
            errors=tuple(context.warnings),
        )
        logger.info(
            "Run finished in %.2fs: %d email(s), %d redeemed, %d failed",
            report.duration_seconds,
            report.emails_found,
            report.redeemed,
            report.failed,
        )
        return report

    def confirm(self, message_ids: Sequence[str]) -> ConfirmReport:
        """Record that *you* opened these links yourself.

        The counterpart of ``strategy: manual``: the bot cannot observe your
        click, so nothing happens to a message until you say so. Each message
        gets the processed label -- which is what makes the next run skip it --
        and, when ``redeem.manual.on_confirm`` is ``trash``, is moved to the
        provider's Trash.

        Trash, never permanent deletion: it stays recoverable, and the OAuth
        scope the bot holds could not delete it for good anyway.
        """
        settings = self._config.redeem.manual
        label = self._config.gmail.processed_label
        results: list[ConfirmResult] = []

        for message_id in message_ids:
            handle = short_id(message_id)
            labels: tuple[str, ...] = ()
            try:
                if label:
                    self._mailbox.add_label(message_id, label)
                    labels = (label,)
                if settings.trash_on_confirm:
                    self._mailbox.trash(message_id)
                    logger.info("Message %s confirmed and moved to Trash", handle)
                else:
                    logger.info("Message %s confirmed and labelled", handle)
            except BidooBotError as exc:
                logger.error("Could not confirm message %s: %s", handle, exc)
                results.append(ConfirmResult(message_id=message_id, ok=False, detail=str(exc)))
                continue
            results.append(
                ConfirmResult(
                    message_id=message_id,
                    ok=True,
                    detail="moved to Trash" if settings.trash_on_confirm else "labelled",
                    labels_applied=labels,
                    trashed=settings.trash_on_confirm,
                )
            )

        return ConfirmReport(results=tuple(results))

    def status(self) -> StatusReport:
        """Non-sensitive configuration snapshot plus a mailbox probe."""
        from bidoo_bot import __version__

        try:
            health = self._mailbox.check_connection()
        except BidooBotError as exc:
            health = MailboxHealth(ok=False, detail=str(exc))
        except Exception as exc:
            logger.debug("Mailbox probe raised: %r", exc)
            health = MailboxHealth(ok=False, detail="unexpected error while contacting Gmail")

        return StatusReport(
            dry_run=self._config.redeem.dry_run,
            query=self._config.gmail.effective_query(),
            strategy=self._config.redeem.strategy,
            processed_label=self._config.gmail.processed_label,
            allowed_domains=self._policy.allowed_domains,
            mailbox_ok=health.ok,
            mailbox_detail=health.detail,
            config_path=str(self._config.source_path) if self._config.source_path else "(defaults)",
            version=__version__,
        )

    # -- per message --------------------------------------------------------

    def _process(self, message: EmailMessage, context: _RunContext) -> MessageResult:
        handle = short_id(message.id)
        logger.info("Processing message %s", handle)

        already = self._already_processed(message)
        if already is not None:
            return already

        parse_result = self._parser.parse(message.html, text_body=message.text)
        if not parse_result.ok or parse_result.best is None:
            status = _PARSE_STATUS_MAP.get(parse_result.status, MessageStatus.UNRECOGNIZED)
            logger.info("Message %s -> %s (%s)", handle, status.value, parse_result.detail)
            return self._result(message, status, parse_result.detail, parse_result.best)

        candidate = parse_result.best
        logger.info(
            "Action candidate found for %s: confidence=%.2f url=%s",
            handle,
            candidate.confidence,
            redact_url(candidate.url),
        )

        decision = self._policy.check(candidate.url)
        if not decision.allowed:
            logger.warning("Message %s -> REJECTED: %s", handle, decision.reason)
            return self._result(message, MessageStatus.REJECTED, decision.reason, candidate)

        if self._config.redeem.is_manual:
            # The link is validated but never opened: it is handed to you, and
            # the mail is left untouched until confirm() is called.
            logger.info("Message %s -> MANUAL: link handed over, nothing executed", handle)
            return self._result(message, MessageStatus.MANUAL, "open this link yourself", candidate)

        if context.dry_run:
            logger.info("Message %s -> DRY RUN: action not executed", handle)
            return self._result(
                message, MessageStatus.DRY_RUN, "dry run: action not executed", candidate
            )

        return self._execute(message, candidate, context, handle)

    def _execute(
        self,
        message: EmailMessage,
        candidate: ActionCandidate,
        context: _RunContext,
        handle: str,
    ) -> MessageResult:
        if context.redeemer is None:
            try:
                context.redeemer = self._redeemer_factory()
            except BidooBotError as exc:
                logger.error("Could not build the redeemer: %s", exc)
                return self._failure(message, str(exc), candidate, context)

        logger.info("Redeeming action for %s via %s", handle, context.redeemer.name)
        try:
            attempt = context.redeemer.redeem(candidate)
        except BidooBotError as exc:
            return self._failure(message, str(exc), candidate, context)
        except Exception as exc:
            logger.exception("Unexpected redeemer error for %s", handle)
            return self._failure(
                message, f"unexpected {type(exc).__name__} while redeeming", candidate, context
            )

        if not attempt.success:
            return self._failure(message, attempt.detail or "action rejected", candidate, context)

        logger.info("Redemption successful for %s", handle)
        detail = attempt.detail or "free bid redeemed"
        labels = self._apply_label(message.id, self._config.gmail.processed_label, context)
        if not labels and self._config.gmail.processed_label:
            detail = f"{detail} (warning: label not applied)"
        return self._result(message, MessageStatus.REDEEMED, detail, candidate, labels)

    # -- helpers ------------------------------------------------------------

    def _already_processed(self, message: EmailMessage) -> MessageResult | None:
        """Second line of defence: the query normally excludes these already."""
        label = self._config.gmail.processed_label
        if not label or label not in message.labels:
            return None
        logger.info("Message %s already carries '%s', skipping", short_id(message.id), label)
        return self._result(message, MessageStatus.ALREADY_PROCESSED, f"already labelled '{label}'")

    def _failure(
        self,
        message: EmailMessage,
        detail: str,
        candidate: ActionCandidate | None,
        context: _RunContext,
    ) -> MessageResult:
        logger.warning("Message %s failed: %s", short_id(message.id), detail)
        labels = self._apply_label(message.id, self._config.gmail.failed_label, context)
        return self._result(message, MessageStatus.FAILED, detail, candidate, labels)

    def _apply_label(
        self, message_id: str, label: str | None, context: _RunContext
    ) -> tuple[str, ...]:
        if not label:
            return ()
        try:
            self._mailbox.add_label(message_id, label)
        except BidooBotError as exc:
            # The action already happened; failing to record it risks a repeat
            # on the next run, so make it loud rather than silent.
            message = f"could not apply label '{label}': {exc}"
            logger.error("%s (message %s)", message, short_id(message_id))
            context.warnings.append(message)
            return ()
        return (label,)

    @staticmethod
    def _result(
        message: EmailMessage,
        status: MessageStatus,
        detail: str = "",
        candidate: ActionCandidate | None = None,
        labels: tuple[str, ...] = (),
    ) -> MessageResult:
        return MessageResult(
            message_id=message.id,
            status=status,
            detail=detail,
            subject=message.subject,
            candidate=candidate,
            labels_applied=labels,
        )

    def _should_delay(self, previous: MessageResult) -> bool:
        """Only pause after an action was actually executed."""
        if self._config.redeem.delay_between_actions_seconds <= 0:
            return False
        return previous.status in (MessageStatus.REDEEMED, MessageStatus.FAILED)

    @staticmethod
    def _close(context: _RunContext) -> None:
        if context.redeemer is None:
            return
        try:
            context.redeemer.close()
        except Exception as exc:
            logger.debug("Redeemer cleanup failed: %s", exc)
