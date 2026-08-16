"""Loading a saved message from disk.

Used by ``bidoo-bot analyze-email <file>``: point it at a real ``.eml`` (or a
raw ``.html`` dump) and see exactly which candidates the parser finds, without
touching Gmail.
"""

from __future__ import annotations

from email import policy
from email.message import Message
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from pathlib import Path

from bidoo_bot.errors import BidooBotError
from bidoo_bot.models.email import EmailMessage

HTML_SUFFIXES = frozenset({".html", ".htm"})
EML_SUFFIXES = frozenset({".eml", ".mime", ".msg.eml"})


def _decode(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if not isinstance(payload, bytes):
        # Undecodable or multipart container: fall back to the raw payload.
        raw = part.get_payload()
        return raw if isinstance(raw, str) else ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _bodies(message: Message) -> tuple[str, str]:
    """Return ``(html, text)`` bodies, skipping attachments."""
    html_parts: list[str] = []
    text_parts: list[str] = []
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        disposition = str(part.get("Content-Disposition") or "").lower()
        if "attachment" in disposition:
            continue
        content_type = part.get_content_type()
        if content_type == "text/html":
            html_parts.append(_decode(part))
        elif content_type == "text/plain":
            text_parts.append(_decode(part))
    return "\n".join(html_parts), "\n".join(text_parts)


def load_email_file(path: Path | str) -> EmailMessage:
    """Load an ``.eml`` or ``.html`` file into an :class:`EmailMessage`."""
    file_path = Path(path).expanduser()
    if not file_path.is_file():
        raise BidooBotError(f"file not found: {file_path}")

    raw = file_path.read_bytes()
    suffix = file_path.suffix.lower()

    if suffix in HTML_SUFFIXES:
        return EmailMessage(
            id=f"file:{file_path.name}",
            subject=file_path.stem,
            html=raw.decode("utf-8", errors="replace"),
        )

    if suffix not in EML_SUFFIXES:
        # Be forgiving: sniff for MIME headers, otherwise treat it as HTML.
        head = raw[:200].lstrip().lower()
        if not (
            head.startswith(b"from:") or head.startswith(b"received:") or b"mime-version" in head
        ):
            return EmailMessage(
                id=f"file:{file_path.name}",
                subject=file_path.stem,
                html=raw.decode("utf-8", errors="replace"),
            )

    message = BytesParser(policy=policy.default).parsebytes(raw)
    html, text = _bodies(message)

    received_at = None
    date_header = message.get("Date")
    if date_header:
        try:
            received_at = parsedate_to_datetime(str(date_header))
        except (TypeError, ValueError):
            received_at = None

    return EmailMessage(
        id=f"file:{file_path.name}",
        subject=str(message.get("Subject") or file_path.stem),
        sender=str(message.get("From") or ""),
        received_at=received_at,
        html=html,
        text=text,
    )
