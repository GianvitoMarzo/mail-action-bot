"""Loading saved messages from disk for ``analyze-email``."""

from __future__ import annotations

from pathlib import Path

import pytest

from bidoo_bot.errors import BidooBotError
from bidoo_bot.parsing.eml import load_email_file


def test_multipart_eml_gives_both_bodies(fixtures_dir: Path) -> None:
    message = load_email_file(fixtures_dir / "sample_email.eml")

    assert message.subject == "La tua puntata gratis ti aspetta"
    assert "Riscuoti ora" in message.html
    assert "Riscattala qui" in message.text
    assert message.sender.startswith("Bidoo")
    assert message.received_at is not None
    assert message.received_at.year == 2025


def test_plain_text_only_eml(fixtures_dir: Path) -> None:
    message = load_email_file(fixtures_dir / "plaintext_only.eml")

    assert message.html == ""
    assert "puntata gratis" in message.text
    assert message.has_body


def test_raw_html_file_is_accepted(fixtures_dir: Path) -> None:
    message = load_email_file(fixtures_dir / "free_bid_it.html")

    assert message.id == "file:free_bid_it.html"
    assert "RISCUOTI" in message.html
    assert message.text == ""


def test_an_unknown_extension_holding_html_is_sniffed(tmp_path: Path) -> None:
    path = tmp_path / "dump.txt"
    path.write_text("<html><body><a href='https://www.bidoo.com/x'>Riscuoti</a></body></html>")

    message = load_email_file(path)

    assert "Riscuoti" in message.html


def test_an_unknown_extension_holding_mime_is_parsed(tmp_path: Path) -> None:
    path = tmp_path / "dump.txt"
    path.write_text(
        "From: Bidoo <noreply@example.invalid>\n"
        "Subject: Puntata\n"
        "MIME-Version: 1.0\n"
        'Content-Type: text/html; charset="utf-8"\n\n'
        "<p>Riscuoti</p>\n"
    )

    message = load_email_file(path)

    assert message.subject == "Puntata"
    assert "<p>Riscuoti</p>" in message.html


def test_attachments_are_skipped(tmp_path: Path) -> None:
    path = tmp_path / "with_attachment.eml"
    path.write_text(
        "MIME-Version: 1.0\n"
        "Subject: Con allegato\n"
        'Content-Type: multipart/mixed; boundary="B"\n\n'
        "--B\n"
        'Content-Type: text/html; charset="utf-8"\n\n'
        "<p>corpo</p>\n"
        "--B\n"
        'Content-Type: text/plain; charset="utf-8"\n'
        'Content-Disposition: attachment; filename="note.txt"\n\n'
        "NON DEVE ESSERE LETTO\n"
        "--B--\n"
    )

    message = load_email_file(path)

    assert "corpo" in message.html
    assert "NON DEVE ESSERE LETTO" not in message.text


def test_a_missing_file_is_a_clean_error() -> None:
    with pytest.raises(BidooBotError, match="file not found"):
        load_email_file("/nowhere/mail.eml")


def test_a_broken_date_header_does_not_crash(tmp_path: Path) -> None:
    path = tmp_path / "bad_date.eml"
    path.write_text(
        "Subject: Test\nDate: not a date\nMIME-Version: 1.0\n"
        'Content-Type: text/plain; charset="utf-8"\n\ncorpo\n'
    )

    message = load_email_file(path)

    assert message.received_at is None
