"""Gmail adapter: implements :class:`~bidoo_bot.application.ports.MailboxPort`.

Uses the official Gmail API only -- no scraping of the web UI. The adapter's
job is to translate Gmail payloads into :class:`EmailMessage` and to hide every
``googleapiclient`` detail from the core.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from bidoo_bot.application.ports import MailboxHealth
from bidoo_bot.config import AppConfig
from bidoo_bot.errors import AuthError, MailboxError
from bidoo_bot.logging_config import get_logger, redact_email, short_id
from bidoo_bot.models.email import EmailMessage

logger = get_logger(__name__)

_MAX_PAGE_SIZE = 100


def _decode_body(data: str | None, charset: str = "utf-8") -> str:
    if not data:
        return ""
    padded = data + "=" * (-len(data) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded)
    except (binascii.Error, ValueError):
        logger.debug("Could not base64-decode a message part")
        return ""
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _charset_of(part: Mapping[str, Any]) -> str:
    for header in part.get("headers", []) or []:
        if str(header.get("name", "")).lower() == "content-type":
            value = str(header.get("value", ""))
            if "charset=" in value.lower():
                charset = value.lower().split("charset=", 1)[1]
                return charset.strip().strip("\";'").split(";")[0] or "utf-8"
    return "utf-8"


def _walk_parts(payload: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    yield payload
    for part in payload.get("parts", []) or []:
        if isinstance(part, Mapping):
            yield from _walk_parts(part)


def extract_bodies(payload: Mapping[str, Any]) -> tuple[str, str]:
    """Return ``(html, text)`` from a Gmail ``payload``, skipping attachments."""
    html_parts: list[str] = []
    text_parts: list[str] = []
    for part in _walk_parts(payload):
        mime = str(part.get("mimeType", ""))
        if part.get("filename"):
            continue
        body = part.get("body") or {}
        data = body.get("data") if isinstance(body, Mapping) else None
        if not data:
            continue
        if mime == "text/html":
            html_parts.append(_decode_body(str(data), _charset_of(part)))
        elif mime == "text/plain":
            text_parts.append(_decode_body(str(data), _charset_of(part)))
    return "\n".join(html_parts), "\n".join(text_parts)


def _header(payload: Mapping[str, Any], name: str) -> str:
    for header in payload.get("headers", []) or []:
        if str(header.get("name", "")).lower() == name.lower():
            return str(header.get("value", ""))
    return ""


def _internal_date(message: Mapping[str, Any]) -> datetime | None:
    raw = message.get("internalDate")
    if raw is None:
        return None
    try:
        return datetime.fromtimestamp(int(raw) / 1000, tz=UTC)
    except (TypeError, ValueError, OSError):
        return None


class GmailMailbox:
    """Gmail-backed mailbox. Labels double as the idempotency store."""

    def __init__(self, service: Any) -> None:
        self._service = service
        self._label_names: dict[str, str] | None = None
        self._label_ids: dict[str, str] | None = None

    # -- construction -------------------------------------------------------

    @classmethod
    def from_config(cls, config: AppConfig, *, allow_interactive: bool = False) -> GmailMailbox:
        from googleapiclient.discovery import build

        from bidoo_bot.adapters.gmail.auth import load_credentials

        credentials = load_credentials(
            credentials_file=config.resolve(config.gmail.credentials_file),
            token_file=config.resolve(config.gmail.token_file),
            allow_interactive=allow_interactive,
        )
        service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
        return cls(service)

    # -- MailboxPort --------------------------------------------------------

    def search(self, query: str, *, max_results: int) -> Sequence[EmailMessage]:
        listing = self._call(
            self._service.users()
            .messages()
            .list(
                userId="me",
                q=query,
                maxResults=min(max_results, _MAX_PAGE_SIZE),
                includeSpamTrash=False,
            ),
            "searching messages",
        )
        ids = [str(item["id"]) for item in listing.get("messages", []) if item.get("id")]
        logger.debug("Gmail returned %d message id(s)", len(ids))

        messages: list[EmailMessage] = []
        for message_id in ids[:max_results]:
            payload = self._call(
                self._service.users().messages().get(userId="me", id=message_id, format="full"),
                f"fetching message {short_id(message_id)}",
            )
            messages.append(self._to_email(payload))
        return messages

    def add_label(self, message_id: str, label: str) -> None:
        label_id = self._ensure_label(label)
        self._call(
            self._service.users()
            .messages()
            .modify(userId="me", id=message_id, body={"addLabelIds": [label_id]}),
            f"labelling message {short_id(message_id)}",
        )
        logger.info("Applied label '%s' to message %s", label, short_id(message_id))

    def check_connection(self) -> MailboxHealth:
        profile = self._call(
            self._service.users().getProfile(userId="me"), "reading the Gmail profile"
        )
        address = str(profile.get("emailAddress", ""))
        return MailboxHealth(
            ok=True,
            detail=f"connected as {redact_email(address)}" if address else "connected",
        )

    # -- internals ----------------------------------------------------------

    def _to_email(self, message: Mapping[str, Any]) -> EmailMessage:
        payload = message.get("payload") or {}
        html, text = extract_bodies(payload) if isinstance(payload, Mapping) else ("", "")
        label_names = self._resolve_label_names(message.get("labelIds", []) or [])
        return EmailMessage(
            id=str(message.get("id", "")),
            subject=_header(payload, "Subject") if isinstance(payload, Mapping) else "",
            sender=_header(payload, "From") if isinstance(payload, Mapping) else "",
            received_at=_internal_date(message),
            html=html,
            text=text,
            labels=label_names,
        )

    def _resolve_label_names(self, label_ids: Sequence[Any]) -> tuple[str, ...]:
        names = self._labels_by_id()
        return tuple(names.get(str(label_id), str(label_id)) for label_id in label_ids)

    def _load_labels(self) -> None:
        listing = self._call(self._service.users().labels().list(userId="me"), "listing labels")
        by_id: dict[str, str] = {}
        by_name: dict[str, str] = {}
        for label in listing.get("labels", []) or []:
            label_id = str(label.get("id", ""))
            name = str(label.get("name", ""))
            if label_id and name:
                by_id[label_id] = name
                by_name[name] = label_id
        self._label_names = by_id
        self._label_ids = by_name

    def _labels_by_id(self) -> dict[str, str]:
        if self._label_names is None:
            self._load_labels()
        return self._label_names or {}

    def _labels_by_name(self) -> dict[str, str]:
        if self._label_ids is None:
            self._load_labels()
        return self._label_ids or {}

    def _ensure_label(self, name: str) -> str:
        existing = self._labels_by_name().get(name)
        if existing:
            return existing
        logger.info("Creating Gmail label '%s'", name)
        created = self._call(
            self._service.users()
            .labels()
            .create(
                userId="me",
                body={
                    "name": name,
                    "labelListVisibility": "labelShow",
                    "messageListVisibility": "show",
                },
            ),
            f"creating label '{name}'",
        )
        label_id = str(created.get("id", ""))
        if not label_id:
            raise MailboxError(f"Gmail did not return an id for the new label '{name}'")
        self._load_labels()
        return label_id

    @staticmethod
    def _call(request: Any, what: str) -> Mapping[str, Any]:
        """Execute a Gmail API request, translating errors into our own."""
        from googleapiclient.errors import HttpError

        try:
            result = request.execute()
        except HttpError as exc:
            status = getattr(getattr(exc, "resp", None), "status", None)
            if status in (401, 403):
                raise AuthError(
                    f"Gmail refused the request while {what} (HTTP {status}). "
                    "The token may be revoked or missing the gmail.modify scope; "
                    "run `bidoo-bot gmail-auth` again."
                ) from exc
            raise MailboxError(f"Gmail API error while {what} (HTTP {status or '?'})") from exc
        except OSError as exc:
            raise MailboxError(f"network error while {what}: {exc}") from exc
        if not isinstance(result, Mapping):  # pragma: no cover - defensive
            raise MailboxError(f"unexpected Gmail response while {what}")
        return result
