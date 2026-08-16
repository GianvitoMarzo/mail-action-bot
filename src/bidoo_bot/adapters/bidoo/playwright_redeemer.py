"""Playwright redeemer, for when a real browser session is required.

Use this only if the HTTP strategy turns out not to work, i.e. the redeem page
needs cookies from a logged-in session or finishes the action in JavaScript.

What this module deliberately does **not** do:

* it never types a password and never stores credentials;
* it never touches CAPTCHAs, MFA or bot-detection;
* it never creates a session -- you log in by hand once, into a persistent
  profile directory, and the bot merely reuses it.

If the site presents a login page, the run simply fails with a clear message.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bidoo_bot.config import PlaywrightSettings
from bidoo_bot.errors import DependencyMissingError, RedemptionError
from bidoo_bot.logging_config import get_logger, redact_url
from bidoo_bot.models.candidate import ActionCandidate
from bidoo_bot.models.results import RedemptionAttempt
from bidoo_bot.security import UrlPolicy

logger = get_logger(__name__)

_INSTALL_HINT = (
    "Playwright is not installed. Run `pip install 'bidoo-bot[playwright]'` and "
    "`playwright install chromium`, or set redeem.strategy back to 'http'."
)


class PlaywrightRedeemer:
    """Opens the candidate URL in a persistent, manually authenticated profile."""

    name = "playwright"

    def __init__(self, settings: PlaywrightSettings, policy: UrlPolicy) -> None:
        self._settings = settings
        self._policy = policy
        self._playwright: Any = None
        self._context: Any = None

    # -- lifecycle ----------------------------------------------------------

    def _ensure_context(self) -> Any:
        if self._context is not None:
            return self._context
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise DependencyMissingError(_INSTALL_HINT) from exc

        profile_dir = self._settings.user_data_dir
        if not profile_dir.exists():
            raise RedemptionError(
                f"the Playwright profile {profile_dir} does not exist yet. "
                "Run `bidoo-bot browser-login` once and sign in manually."
            )

        self._playwright = sync_playwright().start()
        browser_type = getattr(self._playwright, self._settings.browser)
        self._context = browser_type.launch_persistent_context(
            str(profile_dir),
            headless=self._settings.headless,
        )
        self._context.set_default_timeout(self._settings.timeout_seconds * 1000)
        self._install_navigation_guard(self._context)
        return self._context

    def close(self) -> None:
        for resource, closer in ((self._context, "close"), (self._playwright, "stop")):
            if resource is None:
                continue
            try:
                getattr(resource, closer)()
            except Exception as exc:
                logger.debug("Playwright cleanup (%s) failed: %s", closer, exc)
        self._context = None
        self._playwright = None

    def __enter__(self) -> PlaywrightRedeemer:
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    # -- redeem -------------------------------------------------------------

    def redeem(self, candidate: ActionCandidate) -> RedemptionAttempt:
        decision = self._policy.check(candidate.url)
        if not decision.allowed:
            return RedemptionAttempt(
                success=False, detail=f"URL refused by policy: {decision.reason}"
            )

        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeout

        context = self._ensure_context()
        page = context.new_page()
        try:
            try:
                page.goto(
                    candidate.url,
                    wait_until=self._settings.wait_until,
                    timeout=self._settings.timeout_seconds * 1000,
                )
            except PlaywrightTimeout:
                return RedemptionAttempt(
                    success=False,
                    detail=f"page did not load within {self._settings.timeout_seconds:.0f}s",
                    final_url=redact_url(candidate.url),
                )
            except PlaywrightError as exc:
                return RedemptionAttempt(
                    success=False,
                    detail=f"navigation failed: {_short_error(exc)}",
                    final_url=redact_url(candidate.url),
                )

            if self._settings.settle_seconds:
                page.wait_for_timeout(self._settings.settle_seconds * 1000)

            final_url = str(page.url)
            final_decision = self._policy.check(final_url)
            if not final_decision.allowed:
                return RedemptionAttempt(
                    success=False,
                    detail=f"page ended on a non-allowed URL: {final_decision.reason}",
                    final_url=redact_url(final_url),
                )

            body = _page_text(page)
            self._maybe_screenshot(page, candidate)
            return self._evaluate(body, final_url)
        finally:
            try:
                page.close()
            except Exception as exc:
                logger.debug("Could not close the page: %s", exc)

    # -- internals ----------------------------------------------------------

    def _install_navigation_guard(self, context: Any) -> None:
        """Abort top-level navigations that leave the allowlist."""
        policy = self._policy

        def handler(route: Any) -> None:
            request = route.request
            try:
                is_navigation = request.is_navigation_request()
            except Exception:
                is_navigation = False
            leaves_allowlist = not policy.check(request.url).allowed
            if is_navigation and request.resource_type == "document" and leaves_allowlist:
                logger.warning("Blocked navigation to %s", redact_url(request.url))
                route.abort()
                return
            route.continue_()

        try:
            context.route("**/*", handler)
        except Exception as exc:
            logger.debug("Could not install the navigation guard: %s", exc)

    def _evaluate(self, body: str, final_url: str) -> RedemptionAttempt:
        for pattern in self._settings.failure_patterns:
            if pattern.search(body):
                return RedemptionAttempt(
                    success=False,
                    detail=f"page matched failure pattern /{pattern.pattern}/",
                    final_url=redact_url(final_url),
                )
        if self._settings.success_patterns:
            matched = next((p for p in self._settings.success_patterns if p.search(body)), None)
            if matched is None:
                return RedemptionAttempt(
                    success=False,
                    detail="no configured success pattern matched the page",
                    final_url=redact_url(final_url),
                )
            return RedemptionAttempt(
                success=True,
                detail=f"free bid redeemed (matched /{matched.pattern}/)",
                final_url=redact_url(final_url),
            )
        return RedemptionAttempt(
            success=True,
            detail="page opened successfully",
            final_url=redact_url(final_url),
        )

    def _maybe_screenshot(self, page: Any, candidate: ActionCandidate) -> None:
        directory = self._settings.screenshot_dir
        if directory is None:
            return
        try:
            Path(directory).mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
            path = Path(directory) / f"redeem-{stamp}-{candidate.position}.png"
            page.screenshot(path=str(path), full_page=False)
            logger.debug("Saved a debug screenshot to %s", path)
        except Exception as exc:
            logger.debug("Could not save the screenshot: %s", exc)


def _page_text(page: Any) -> str:
    try:
        return str(page.inner_text("body"))
    except Exception:
        try:
            return str(page.content())
        except Exception:
            return ""


def _short_error(exc: Exception) -> str:
    first_line = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
    return first_line[:200]


def open_login_browser(settings: PlaywrightSettings, url: str) -> None:
    """Open a visible browser on the persistent profile so you can log in.

    Used by ``bidoo-bot browser-login``. The bot never types credentials: it
    just opens the window and waits for you to close it.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise DependencyMissingError(_INSTALL_HINT) from exc

    settings.user_data_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Opening %s -- sign in manually, then close the window", url)
    with sync_playwright() as playwright:
        browser_type = getattr(playwright, settings.browser)
        context = browser_type.launch_persistent_context(
            str(settings.user_data_dir), headless=False
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(url, wait_until="load", timeout=settings.timeout_seconds * 1000)
        try:
            page.wait_for_event("close", timeout=0)
        except Exception as exc:
            logger.debug("Login window closed (%s)", type(exc).__name__)
        finally:
            context.close()
    logger.info("Session stored in %s", settings.user_data_dir)
