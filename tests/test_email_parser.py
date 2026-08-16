"""Parser tests: the component that decides what gets clicked."""

from __future__ import annotations

import pytest

from bidoo_bot.config import AppConfig, build_config, default_config_dict
from bidoo_bot.models.candidate import ParseStatus
from bidoo_bot.parsing.action_parser import ActionParser
from bidoo_bot.parsing.html import canonical_url, clean_href, extract_links, linkify_plaintext
from tests.conftest import read_fixture


def parse_fixture(parser: ActionParser, name: str):
    return parser.parse(read_fixture(name))


# ---------------------------------------------------------------------------
# Happy paths, with deliberately different button wording
# ---------------------------------------------------------------------------


def test_italian_button_is_found(parser: ActionParser) -> None:
    result = parse_fixture(parser, "free_bid_it.html")

    assert result.status is ParseStatus.OK
    assert result.best is not None
    assert result.best.url == "https://www.bidoo.com/promo/riscuoti?token=Zm9vYmFyMTIzNDU2"
    assert result.best.confidence > 0.9
    assert "redeem-verb-it" in result.best.signals


def test_english_image_button_without_link_text_is_found(parser: ActionParser) -> None:
    """The label lives only in an <img alt>, and the host is a subdomain."""
    result = parse_fixture(parser, "free_bid_en_image_button.html")

    assert result.status is ParseStatus.OK
    assert result.best is not None
    assert result.best.text == "Claim your free bid"
    assert result.best.url.startswith("https://click.bidoo.com/")


@pytest.mark.parametrize(
    "label",
    [
        "Riscuoti",
        "RISCATTA IL TUO REGALO",
        "Ritira la tua puntata gratis",
        "Claim your free bid",
        "Redeem now",
        "Ottieni la puntata gratuita",
    ],
)
def test_many_different_button_labels_are_accepted(parser: ActionParser, label: str) -> None:
    """The bot must not assume the button is always called "Riscuoti"."""
    html = (
        "<html><body><p>Hai una puntata gratis in regalo.</p>"
        f'<a class="btn" href="https://www.bidoo.com/promo/redeem?token=ABCDEFGH">{label}</a>'
        "</body></html>"
    )

    result = parser.parse(html)

    assert result.status is ParseStatus.OK, f"{label!r} was not recognised"
    assert result.best is not None
    assert result.best.confidence >= 0.65


def test_footer_links_do_not_beat_the_button(parser: ActionParser) -> None:
    result = parse_fixture(parser, "free_bid_it.html")

    assert result.best is not None
    others = {c.url: c.confidence for c in result.candidates if c.url != result.best.url}
    assert all(score < result.best.confidence for score in others.values())
    unsubscribe = next(url for url in others if "unsubscribe" in url)
    assert others[unsubscribe] < 0.1


def test_a_footer_unsubscribe_does_not_penalise_the_button(parser: ActionParser) -> None:
    """Regression: 'any' rules must look at the link, not at its surroundings.

    In a short email every link shares the same ancestor text, so matching
    "Disiscriviti" from the footer used to zero out the real button.
    """
    html = (
        "<html><body><p>Hai una <b>puntata gratis</b>.</p>"
        '<a class="btn" href="https://www.bidoo.com/promo/riscuoti?token=ABCDEFGH">Riscuoti ora</a>'
        '<p><a href="https://news.example.invalid/u/1">Disiscriviti</a></p>'
        "</body></html>"
    )

    result = parser.parse(html)

    assert result.status is ParseStatus.OK
    assert result.best is not None
    assert result.best.confidence > 0.9


# ---------------------------------------------------------------------------
# Refusing is a feature
# ---------------------------------------------------------------------------


def test_newsletter_without_an_action_is_not_recognised(parser: ActionParser) -> None:
    result = parse_fixture(parser, "no_action.html")

    assert result.status is ParseStatus.LOW_CONFIDENCE
    assert not result.ok
    assert "below min_confidence" in result.detail


def test_two_equally_good_buttons_are_ambiguous(parser: ActionParser) -> None:
    result = parse_fixture(parser, "ambiguous.html")

    assert result.status is ParseStatus.AMBIGUOUS
    assert not result.ok
    assert len(result.candidates) == 2
    assert result.best is not None  # still reported, just not acted upon


def test_the_same_target_repeated_is_not_ambiguous(parser: ActionParser) -> None:
    """Image + button + fallback text link all point at one URL."""
    result = parse_fixture(parser, "duplicate_links.html")

    assert result.status is ParseStatus.OK
    assert len(result.candidates) == 1


def test_empty_body_is_reported(parser: ActionParser) -> None:
    assert parser.parse("").status is ParseStatus.NO_BODY
    assert parser.parse("   \n  ").status is ParseStatus.NO_BODY


def test_body_without_links_is_reported(parser: ActionParser) -> None:
    result = parser.parse("<html><body><p>Solo testo, nessun link.</p></body></html>")

    assert result.status is ParseStatus.NO_LINKS


def test_non_http_links_are_ignored(parser: ActionParser) -> None:
    html = (
        "<html><body>"
        '<a href="mailto:info@example.invalid">Scrivici</a>'
        '<a href="tel:+390000000">Chiamaci</a>'
        '<a href="javascript:void(0)">Riscuoti la puntata gratis</a>'
        '<a href="#top">Torna su</a>'
        '<a href="/promo/riscuoti">Riscuoti (relativo)</a>'
        "</body></html>"
    )

    result = parser.parse(html)

    assert result.status is ParseStatus.NO_LINKS


# ---------------------------------------------------------------------------
# Confidence and explainability
# ---------------------------------------------------------------------------


def test_confidence_stays_within_bounds_and_is_explained(parser: ActionParser) -> None:
    result = parse_fixture(parser, "free_bid_it.html")

    for candidate in result.candidates:
        assert 0.0 <= candidate.confidence <= 1.0
        assert candidate.reason
    assert result.best is not None
    assert "matched" in result.best.reason


def test_more_signals_means_more_confidence(parser: ActionParser) -> None:
    weak = parser.parse(
        '<html><body><a href="https://www.bidoo.com/promo/redeem">Vai</a></body></html>'
    )
    strong = parser.parse(
        "<html><body><p>La tua puntata gratis ti aspetta</p>"
        '<a class="btn" href="https://www.bidoo.com/promo/riscuoti?token=ABCDEFGH">'
        "Riscuoti la puntata gratis</a></body></html>"
    )

    assert weak.candidates[0].confidence < strong.candidates[0].confidence


def test_negative_signals_sink_a_candidate(parser: ActionParser) -> None:
    html = (
        "<html><body>"
        '<a href="https://www.bidoo.com/promo/riscuoti?token=ABCDEFGH">'
        "Riscuoti la puntata gratis: gestisci le preferenze privacy</a>"
        "</body></html>"
    )

    result = parser.parse(html)

    assert result.status is ParseStatus.LOW_CONFIDENCE
    assert "legal-or-preferences" in " ".join(result.candidates[0].signals)


def test_ambiguity_margin_can_be_disabled(config: AppConfig) -> None:
    data = default_config_dict()
    data["parser"]["ambiguity_margin"] = 0.0
    relaxed = ActionParser(build_config(data).parser)

    result = relaxed.parse(read_fixture("ambiguous.html"))

    assert result.status is ParseStatus.OK


def test_raising_min_confidence_rejects_a_good_button(config: AppConfig) -> None:
    data = default_config_dict()
    data["parser"]["min_confidence"] = 0.999
    strict = ActionParser(build_config(data).parser)

    assert strict.parse(read_fixture("free_bid_it.html")).status is ParseStatus.LOW_CONFIDENCE


def test_max_links_caps_the_work(config: AppConfig) -> None:
    data = default_config_dict()
    data["parser"]["max_links"] = 2
    capped = ActionParser(build_config(data).parser)
    html = "<html><body>" + "".join(
        f'<a href="https://www.bidoo.com/{i}">link {i}</a>' for i in range(50)
    )

    assert len(capped.parse(html).candidates) <= 2


# ---------------------------------------------------------------------------
# Plain-text fallback and URL helpers
# ---------------------------------------------------------------------------


def test_plaintext_body_is_linkified_and_parsed(parser: ActionParser) -> None:
    text = (
        "Hai una puntata gratis.\n"
        "Riscattala qui: https://www.bidoo.com/promo/riscuoti?token=ABCDEFGH\n"
    )

    result = parser.parse("", text_body=text)

    assert result.status is ParseStatus.OK
    assert result.best is not None
    assert result.best.url.endswith("token=ABCDEFGH")


def test_linkify_escapes_html_in_plain_text() -> None:
    html = linkify_plaintext("<script>alert(1)</script> https://www.bidoo.com/x")

    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert 'href="https://www.bidoo.com/x"' in html


def test_clean_href_normalises_wrapped_urls() -> None:
    assert clean_href("https://www.bidoo.com/promo\n/riscuoti") == (
        "https://www.bidoo.com/promo/riscuoti"
    )
    assert clean_href("  https://www.bidoo.com/x  ") == "https://www.bidoo.com/x"
    assert clean_href("HTTPS://WWW.BIDOO.COM/x") == "HTTPS://WWW.BIDOO.COM/x"
    assert clean_href("") is None
    assert clean_href("ftp://example.invalid/x") is None


def test_canonical_url_keeps_the_token_but_folds_case_and_fragment() -> None:
    assert canonical_url("HTTPS://WWW.Bidoo.com/p?token=AbC#frag") == (
        "https://www.bidoo.com/p?token=AbC"
    )
    assert canonical_url("https://www.bidoo.com:443/p") == "https://www.bidoo.com/p"


def test_extract_links_collects_attributes_and_context() -> None:
    html = (
        "<html><body><td><p>La tua puntata gratis ti aspetta subito</p>"
        '<a id="cta" class="btn primary" data-track="hero" '
        'href="https://www.bidoo.com/x">Riscuoti</a></td></body></html>'
    )

    links = extract_links(html)

    assert len(links) == 1
    assert links[0].text == "Riscuoti"
    assert "btn" in links[0].attrs
    assert "data-track" in links[0].attrs
    assert "puntata gratis" in links[0].context


# ---------------------------------------------------------------------------
# Offers expressed as a quantity of bids ("3 Puntate 🎁") rather than "gratis"
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label",
    [
        "🔓 Sblocca 3 Puntate 🎁",
        "Riscatta 2 Puntate 🎁",
        "Prendi 5 🎁 e vinci 💰",
        "Ritira le tue 10 puntate 🎁",
        "Claim your 3 bids 🎁",
    ],
)
def test_a_quantity_of_bids_is_recognised(parser: ActionParser, label: str) -> None:
    """Regression: "Sblocca 3 Puntate 🎁" used to score 0.55 and be refused."""
    html = (
        "<html><body><p>Le aste di oggi.</p>"
        f'<a href="https://elink.bidoo.com/vtrack?clientid=0000">{label}</a>'
        "</body></html>"
    )

    result = parser.parse(html)

    assert result.status is ParseStatus.OK, f"{label!r} was refused"
    assert result.best is not None
    assert result.best.confidence >= 0.65


def test_count_and_emoji_beat_the_unsubscribe_sharing_the_same_url_shape(
    parser: ActionParser,
) -> None:
    result = parse_fixture(parser, "free_bid_count_emoji.html")

    assert result.status is ParseStatus.OK
    assert result.best is not None
    assert "Sblocca 3 Puntate" in result.best.text
    # The fixture host is fictional, so bidoo-domain does not fire: this is the
    # text signals clearing the threshold entirely on their own.
    assert result.best.confidence >= 0.65
    assert "bidoo-domain" not in result.best.signals
    unsubscribe = next(c for c in result.candidates if c.text == "disiscriviti")
    assert unsubscribe.confidence < 0.1


def test_the_unsubscribe_context_mentioning_bids_does_not_lift_it(
    parser: ActionParser,
) -> None:
    """The footer says "...e quindi neanche puntate": a context rule would fire."""
    result = parse_fixture(parser, "free_bid_count_emoji.html")

    unsubscribe = next(c for c in result.candidates if c.text == "disiscriviti")
    assert "free-bid-count" not in unsubscribe.signals
    assert "gift-emoji" not in unsubscribe.signals


def test_a_bare_count_is_not_enough_on_its_own(parser: ActionParser) -> None:
    """0.40 + bidoo-domain = 0.64, just under the threshold: needs corroboration."""
    html = (
        '<html><body><a href="https://www.bidoo.com/storico">Hai usato 3 puntate</a></body></html>'
    )

    result = parser.parse(html)

    assert result.status is ParseStatus.LOW_CONFIDENCE
    assert result.candidates[0].confidence < 0.65


def test_the_gift_emoji_alone_is_not_enough(parser: ActionParser) -> None:
    html = '<html><body><a href="https://www.bidoo.com/aste">Le aste 🎁</a></body></html>'

    result = parser.parse(html)

    assert result.status is ParseStatus.LOW_CONFIDENCE


def test_a_plain_number_without_the_bid_noun_does_not_match(parser: ActionParser) -> None:
    html = '<html><body><a href="https://www.bidoo.com/aste">Vedi le 200 aste</a></body></html>'

    result = parser.parse(html)

    assert "free-bid-count" not in result.candidates[0].signals
