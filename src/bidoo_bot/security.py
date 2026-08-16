"""URL security policy.

The whole point of this module: the bot must never follow a link just because
it appeared in an email. Every URL -- the candidate itself *and* every redirect
hop -- is checked against an explicit allowlist before any request is made.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from bidoo_bot.config import SecuritySettings
from bidoo_bot.errors import SecurityPolicyError


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    allowed: bool
    reason: str

    def __bool__(self) -> bool:
        return self.allowed


class UrlPolicy:
    """Allowlist-based check applied before any outbound request."""

    def __init__(self, settings: SecuritySettings) -> None:
        self._settings = settings
        self._domains = tuple(d.lower().lstrip(".") for d in settings.allowed_domains)

    @property
    def allowed_domains(self) -> tuple[str, ...]:
        return self._domains

    @property
    def max_redirects(self) -> int:
        return self._settings.max_redirects

    def check(self, url: str) -> PolicyDecision:
        """Decide whether ``url`` may be requested. Never raises."""
        if not url or not url.strip():
            return PolicyDecision(False, "empty URL")
        try:
            parts = urlsplit(url.strip())
        except ValueError as exc:
            return PolicyDecision(False, f"malformed URL ({exc})")

        scheme = parts.scheme.lower()
        if scheme not in ("http", "https"):
            return PolicyDecision(False, f"scheme '{scheme or '?'}' is not http(s)")
        if self._settings.require_https and scheme != "https":
            return PolicyDecision(False, "https is required by config (security.require_https)")

        netloc = parts.netloc
        if "@" in netloc:
            # https://bidoo.com@evil.example/ -- the real host is evil.example.
            return PolicyDecision(False, "URL carries userinfo, refusing (possible spoofing)")

        try:
            host = (parts.hostname or "").lower().rstrip(".")
        except ValueError as exc:
            return PolicyDecision(False, f"malformed host ({exc})")
        if not host:
            return PolicyDecision(False, "URL has no host")

        for domain in self._domains:
            if host == domain:
                return PolicyDecision(True, f"host matches allowed domain '{domain}'")
            if self._settings.allow_subdomains and host.endswith("." + domain):
                return PolicyDecision(True, f"host is a subdomain of allowed domain '{domain}'")

        return PolicyDecision(
            False,
            f"host '{host}' is not in security.allowed_domains ({', '.join(self._domains)})",
        )

    def ensure(self, url: str) -> None:
        """Raise :class:`SecurityPolicyError` when ``url`` is not allowed."""
        decision = self.check(url)
        if not decision.allowed:
            raise SecurityPolicyError(decision.reason)
