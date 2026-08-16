"""Email body parsing: pure functions, no I/O, no provider dependency."""

from bidoo_bot.parsing.action_parser import ActionParser
from bidoo_bot.parsing.eml import load_email_file
from bidoo_bot.parsing.html import LinkFeatures, canonical_url, extract_links, linkify_plaintext

__all__ = [
    "ActionParser",
    "LinkFeatures",
    "canonical_url",
    "extract_links",
    "linkify_plaintext",
    "load_email_file",
]
