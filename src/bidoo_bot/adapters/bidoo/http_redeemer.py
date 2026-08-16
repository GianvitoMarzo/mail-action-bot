"""HTTP redeemer: a single controlled GET on an already validated URL.

This is the default strategy. It is deliberately dumb -- it does not log in,
does not solve challenges and does not poke at endpoints. It requests the URL
that was in the email, follows redirects **one hop at a time** re-checking the
allowlist at every hop, and decides success from the status code and the
configured patterns.

If your Bidoo mails need an authenticated browser session, switch
``redeem.strategy`` to ``playwright`` instead of making this module smarter.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

import httpx

from bidoo_bot.config import HttpSettings
from bidoo_bot.logging_config import get_logger, redact_url
from bidoo_bot.models.candidate import ActionCandidate
from bidoo_bot.models.results import RedemptionAttempt
from bidoo_bot.security import UrlPolicy

logger = get_logger(__name__)

_TEXTUAL_TYPES = ("text/", "application/json", "application/xhtml")
_MAX_BODY_CHARS = 200_000


class HttpRedeemer:
    """Executes the redeem action with a plain HTTP request."""

    name = "http"

    def __init__(
        self,
        settings: HttpSettings,
        policy: UrlPolicy,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self._settings = settings
        self._policy = policy
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(settings.timeout_seconds),
            verify=settings.verify_tls,
            follow_redirects=False,
            headers={
                "User-Agent": settings.user_agent,
                "Accept-Language": settings.accept_language,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )

    def redeem(self, candidate: ActionCandidate) -> RedemptionAttempt:
        url = candidate.url
        # Belt and braces: the service already checked this, check again here
        # so the adapter is safe to use on its own.
        decision = self._policy.check(url)
        if not decision.allowed:
            return RedemptionAttempt(
                success=False, detail=f"URL refused by policy: {decision.reason}"
            )

        response: httpx.Response
        for hop in range(self._policy.max_redirects + 1):
            logger.debug("GET %s (hop %d)", redact_url(url), hop)
            try:
                response = self._client.get(url)
            except httpx.TimeoutException:
                return RedemptionAttempt(
                    success=False,
                    detail=f"timeout after {self._settings.timeout_seconds:.0f}s",
                    final_url=redact_url(url),
                )
            except httpx.HTTPError as exc:
                return RedemptionAttempt(
                    success=False,
                    detail=f"request failed: {type(exc).__name__}",
                    final_url=redact_url(url),
                )

            if not response.is_redirect:
                break

            location = response.headers.get("location", "")
            if not location:
                return RedemptionAttempt(
                    success=False,
                    detail=f"HTTP {response.status_code} redirect without a Location header",
                    status_code=response.status_code,
                    final_url=redact_url(url),
                )
            next_url = urljoin(str(response.url), location)
            hop_decision = self._policy.check(next_url)
            if not hop_decision.allowed:
                # This is the interesting failure mode: a link that looked fine
                # bouncing somewhere else. Stop, do not follow.
                logger.warning("Refusing redirect to a non-allowed URL: %s", hop_decision.reason)
                return RedemptionAttempt(
                    success=False,
                    detail=f"redirect refused: {hop_decision.reason}",
                    status_code=response.status_code,
                    final_url=redact_url(url),
                )
            url = next_url
        else:
            return RedemptionAttempt(
                success=False,
                detail=f"more than {self._policy.max_redirects} redirects",
                final_url=redact_url(url),
            )

        return self._evaluate(response)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> HttpRedeemer:
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    # -- internals ----------------------------------------------------------

    def _evaluate(self, response: httpx.Response) -> RedemptionAttempt:
        final_url = redact_url(str(response.url))
        status = response.status_code
        if status >= 400:
            return RedemptionAttempt(
                success=False,
                detail=f"server answered HTTP {status}",
                status_code=status,
                final_url=final_url,
            )

        body = self._body_text(response)
        for pattern in self._settings.failure_patterns:
            if body and pattern.search(body):
                return RedemptionAttempt(
                    success=False,
                    detail=f"page matched failure pattern /{pattern.pattern}/",
                    status_code=status,
                    final_url=final_url,
                )

        if self._settings.success_patterns:
            if not body:
                return RedemptionAttempt(
                    success=False,
                    detail="success patterns configured but the response has no text body",
                    status_code=status,
                    final_url=final_url,
                )
            matched = next(
                (p for p in self._settings.success_patterns if p.search(body)),
                None,
            )
            if matched is None:
                return RedemptionAttempt(
                    success=False,
                    detail="no configured success pattern matched the response",
                    status_code=status,
                    final_url=final_url,
                )
            return RedemptionAttempt(
                success=True,
                detail=f"free bid redeemed (matched /{matched.pattern}/)",
                status_code=status,
                final_url=final_url,
            )

        return RedemptionAttempt(
            success=True,
            detail=f"free bid redeemed (HTTP {status})",
            status_code=status,
            final_url=final_url,
        )

    @staticmethod
    def _body_text(response: httpx.Response) -> str:
        content_type = response.headers.get("content-type", "").lower()
        if not any(content_type.startswith(prefix) for prefix in _TEXTUAL_TYPES):
            return ""
        try:
            return response.text[:_MAX_BODY_CHARS]
        except (UnicodeDecodeError, httpx.HTTPError):  # pragma: no cover - defensive
            return ""
