"""Logging setup and redaction helpers.

The bot handles a Gmail OAuth token, a Telegram bot token and URLs that may
carry single-use redeem codes. None of that may end up in a log file, so every
handler installed here goes through :class:`RedactingFilter`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit

REDACTED = "[redacted]"

# Values registered at runtime (the real Telegram token, ...). Kept module level
# so any logger in the process benefits, whatever order things are built in.
_KNOWN_SECRETS: set[str] = set()

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Telegram bot token: <digits>:<35ish base64url chars>
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"), REDACTED),
    # Google OAuth access / refresh tokens
    (re.compile(r"\bya29\.[A-Za-z0-9._-]+"), REDACTED),
    (re.compile(r"\b1//[A-Za-z0-9._-]{10,}"), REDACTED),
    # Authorization headers and cookie dumps. These consume the rest of the
    # line on purpose: "Authorization: Bearer <token>" must not leave the token
    # behind, and no part of a cookie header is ever worth keeping.
    (
        re.compile(r"(?i)\b(authorization|proxy-authorization)\s*[:=]\s*[^\r\n]+"),
        r"\1: " + REDACTED,
    ),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 " + REDACTED),
    (re.compile(r"(?i)\b(set-)?cookie\s*[:=]\s*[^\r\n]+"), REDACTED),
    # key=value shaped secrets, in JSON or query strings
    (
        re.compile(
            r"(?i)([\"']?\b(?:access_token|refresh_token|id_token|client_secret|api_?key|"
            r"password|passwd|secret|token|auth|session)\b[\"']?\s*[:=]\s*[\"']?)"
            r"([A-Za-z0-9._~+/=-]{6,})"
        ),
        r"\1" + REDACTED,
    ),
    # Bare email addresses. The lookbehind keeps already-masked addresses
    # produced by redact_email() ("m***o@example.com") intact.
    (re.compile(r"(?<![*\w.+-])[\w.+-]+@[\w-]+\.[\w.-]+\b"), REDACTED),
)


def register_secret(value: str | None) -> None:
    """Register a literal value that must never be printed."""
    if value and len(value) >= 6:
        _KNOWN_SECRETS.add(value)


def clear_registered_secrets() -> None:
    """Testing helper."""
    _KNOWN_SECRETS.clear()


def redact(text: str) -> str:
    """Remove known secrets and secret-shaped substrings from ``text``."""
    if not text:
        return text
    for secret in _KNOWN_SECRETS:
        if secret in text:
            text = text.replace(secret, REDACTED)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_email(address: str) -> str:
    """``mario.rossi@example.com`` -> ``m***i@example.com``."""
    address = address.strip()
    if "@" not in address:
        return REDACTED
    local, _, domain = address.partition("@")
    masked = "*" * len(local) if len(local) <= 2 else f"{local[0]}***{local[-1]}"
    return f"{masked}@{domain}"


def redact_url(url: str, *, keep_path: bool = True) -> str:
    """Keep scheme, host and (optionally) path; drop query and fragment.

    Redeem links often carry a single-use code in the query string, so the
    query is never logged.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return REDACTED
    if not parts.scheme or not parts.netloc:
        return REDACTED
    path = parts.path if keep_path else ""
    suffix = "?" + REDACTED if parts.query else ""
    return f"{parts.scheme}://{parts.netloc}{path}{suffix}"


def short_id(value: str, *, length: int = 8) -> str:
    """Stable, non-reversible short handle for a message id.

    Lets you correlate log lines without writing the real Gmail id down.
    """
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return digest[:length]


class RedactingFilter(logging.Filter):
    """Rewrites every record's rendered message through :func:`redact`."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - defensive, never break logging
            return True
        cleaned = redact(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = ()
        if record.exc_info is not None and record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


class JsonFormatter(logging.Formatter):
    """Minimal structured formatter; no external dependency."""

    _RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
        "message",
        "asctime",
        "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in self._RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(
    *,
    level: str = "INFO",
    fmt: str = "text",
    redact_records: bool = True,
    secrets: Iterable[str | None] = (),
    stream: Any = None,
) -> None:
    """Install a single stderr handler on the root logger.

    Safe to call more than once: previous handlers installed here are replaced.
    """
    for secret in secrets:
        register_secret(secret)

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s %(levelname)-7s %(name)-28s %(message)s",
                datefmt="%H:%M:%S",
            )
        )
    if redact_records:
        handler.addFilter(RedactingFilter())

    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # These libraries are chatty and occasionally echo request URLs / headers.
    for noisy in (
        "httpx",
        "httpcore",
        "googleapiclient",
        "google",
        "telegram",
        "httpx._client",
        "urllib3",
        "google_auth_oauthlib",
        "asyncio",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
