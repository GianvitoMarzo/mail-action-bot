"""Turning an email body into a flat list of inspectable links.

Kept separate from the scoring logic so that "what can we observe about a
link" and "how much do we trust it" stay independently testable.
"""

from __future__ import annotations

import html as html_module
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

_WS_RE = re.compile(r"\s+")
_URL_IN_TEXT_RE = re.compile(r"https?://[^\s<>\"'\)\]]+")

#: Attributes worth feeding to the ``attrs`` signal field.
_INTERESTING_ATTRS = ("class", "id", "name", "role", "aria-label", "title", "alt", "style")

#: Ancestors we are willing to climb to build the link context.
_CONTEXT_MAX_DEPTH = 4
_CONTEXT_MIN_CHARS = 25


@dataclass(frozen=True, slots=True)
class LinkFeatures:
    """Everything observable about a single ``<a>`` element."""

    index: int
    """Position in document order."""

    text: str
    """Visible text, falling back to image alt / aria-label / title."""

    url: str
    attrs: str
    """Flattened interesting attributes, for pattern matching."""

    context: str
    """Text of the closest meaningful ancestor, truncated."""


def normalize_whitespace(value: str) -> str:
    return _WS_RE.sub(" ", value).strip()


def make_soup(markup: str) -> BeautifulSoup:
    """Parse with lxml when available, falling back to the stdlib parser."""
    try:
        return BeautifulSoup(markup, "lxml")
    except Exception:  # pragma: no cover - only when lxml is missing/broken
        return BeautifulSoup(markup, "html.parser")


def canonical_url(url: str) -> str:
    """Normalize a URL enough to detect duplicates of the same target.

    Lower-cases scheme and host and drops a trailing empty query/fragment.
    Path, query and fragment are otherwise preserved byte for byte: they can
    carry the redeem token and must not be rewritten.
    """
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    netloc = parts.netloc.lower()
    if (parts.scheme == "https" and netloc.endswith(":443")) or (
        parts.scheme == "http" and netloc.endswith(":80")
    ):
        netloc = netloc.rsplit(":", 1)[0]
    return urlunsplit((parts.scheme.lower(), netloc, parts.path, parts.query, ""))


def clean_href(raw: str) -> str | None:
    """Return a usable absolute http(s) URL, or ``None`` when unusable.

    Mail clients wrap long hrefs, so embedded newlines are stripped. Anything
    that is not http/https (``mailto:``, ``tel:``, ``javascript:``, fragments,
    relative paths) is refused: the bot only ever acts on absolute web URLs.
    """
    if not raw:
        return None
    value = _WS_RE.sub("", html_module.unescape(raw.strip()))
    if not value or value.startswith("#"):
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https"):
        return None
    if not parts.netloc:
        return None
    return value


def _first_non_empty(*values: str | None) -> str:
    for value in values:
        if value:
            cleaned = normalize_whitespace(value)
            if cleaned:
                return cleaned
    return ""


def _attr_text(tag: Tag) -> str:
    chunks: list[str] = [tag.name]
    for name in _INTERESTING_ATTRS:
        value: Any = tag.get(name)
        if value is None:
            continue
        if isinstance(value, list):
            chunks.append(" ".join(str(item) for item in value))
        else:
            chunks.append(str(value))
    for name, value in tag.attrs.items():
        if name.startswith("data-"):
            chunks.append(f"{name} {value}")
    for child in tag.find_all("img", limit=3):
        if isinstance(child, Tag):
            chunks.append(_attr_text_of_img(child))
    return normalize_whitespace(" ".join(chunks))


def _attr_text_of_img(img: Tag) -> str:
    parts = [str(img.get(name, "")) for name in ("alt", "title", "src", "class", "id")]
    return " ".join(part for part in parts if part)


def _link_text(tag: Tag) -> str:
    text = normalize_whitespace(tag.get_text(" ", strip=True))
    if text:
        return text
    images = [child for child in tag.find_all("img", limit=3) if isinstance(child, Tag)]
    image_text = _first_non_empty(
        *[str(img.get("alt") or img.get("title") or "") for img in images]
    )
    return _first_non_empty(
        image_text,
        str(tag.get("aria-label") or ""),
        str(tag.get("title") or ""),
    )


def _window(text: str, needle: str, limit: int) -> str:
    """Slice ``limit`` characters of ``text`` centred on ``needle``.

    Without this, climbing to a ``<body>`` in a short email would return the
    whole message as "context" for every link, which is not context at all.
    """
    if len(text) <= limit:
        return text
    start = text.find(needle) if needle else -1
    if start < 0:
        return text[:limit]
    padding = max(0, (limit - len(needle)) // 2)
    begin = max(0, start - padding)
    return text[begin : begin + limit]


def _context_text(tag: Tag, limit: int) -> str:
    if limit <= 0:
        return ""
    node: Tag = tag
    best = ""
    for _ in range(_CONTEXT_MAX_DEPTH):
        parent = node.parent
        if parent is None or not isinstance(parent, Tag) or parent.name in ("html", "[document]"):
            break
        node = parent
        text = normalize_whitespace(node.get_text(" ", strip=True))
        best = text
        if len(text) >= _CONTEXT_MIN_CHARS:
            break
    return _window(best, normalize_whitespace(tag.get_text(" ", strip=True)), limit)


def extract_links(
    html: str, *, max_links: int = 300, context_chars: int = 200
) -> list[LinkFeatures]:
    """Extract every usable ``<a href>`` from an email body."""
    if not html or not html.strip():
        return []
    soup = make_soup(html)
    features: list[LinkFeatures] = []
    for index, anchor in enumerate(soup.find_all("a", href=True)):
        if len(features) >= max_links:
            break
        url = clean_href(str(anchor.get("href") or ""))
        if url is None:
            continue
        features.append(
            LinkFeatures(
                index=index,
                text=_link_text(anchor),
                url=url,
                attrs=_attr_text(anchor),
                context=_context_text(anchor, context_chars),
            )
        )
    return features


def linkify_plaintext(text: str) -> str:
    """Wrap the URLs of a plain-text body in ``<a>`` tags.

    Some senders only ship a ``text/plain`` part. Rather than giving up, the
    text is converted to a minimal HTML document so the same scoring rules
    apply -- the surrounding line becomes the link context.
    """
    if not text.strip():
        return ""
    lines: list[str] = []
    for line in text.splitlines():
        escaped = html_module.escape(line)
        linked = _URL_IN_TEXT_RE.sub(lambda m: f'<a href="{m.group(0)}">{m.group(0)}</a>', escaped)
        lines.append(f"<p>{linked}</p>")
    return "<html><body>" + "".join(lines) + "</body></html>"
