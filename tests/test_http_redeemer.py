"""HTTP redeemer tests. All traffic is mocked; nothing leaves the machine."""

from __future__ import annotations

import httpx
import pytest
import respx

from bidoo_bot.adapters.bidoo.http_redeemer import HttpRedeemer
from bidoo_bot.config import AppConfig, build_config, default_config_dict
from bidoo_bot.models.candidate import ActionCandidate
from bidoo_bot.security import UrlPolicy

TARGET = "https://www.bidoo.com/promo/riscuoti?token=ABCDEFGH"


def candidate(url: str = TARGET) -> ActionCandidate:
    return ActionCandidate(text="Riscuoti", url=url, confidence=0.95, reason="test")


@pytest.fixture
def redeemer(config: AppConfig, policy: UrlPolicy) -> HttpRedeemer:
    return HttpRedeemer(config.redeem.http, policy)


def config_with(**http_overrides: object) -> AppConfig:
    data = default_config_dict()
    data["redeem"]["http"].update(http_overrides)
    return build_config(data)


# ---------------------------------------------------------------------------
# Success and failure
# ---------------------------------------------------------------------------


@respx.mock
def test_successful_get_is_a_redemption(redeemer: HttpRedeemer) -> None:
    route = respx.get(TARGET).mock(return_value=httpx.Response(200, html="<p>Fatto!</p>"))

    attempt = redeemer.redeem(candidate())

    assert attempt.success
    assert attempt.status_code == 200
    assert route.called


@respx.mock
def test_server_error_is_a_failure(redeemer: HttpRedeemer) -> None:
    respx.get(TARGET).mock(return_value=httpx.Response(500, text="oops"))

    attempt = redeemer.redeem(candidate())

    assert not attempt.success
    assert "HTTP 500" in attempt.detail


@respx.mock
def test_timeout_is_reported_not_raised(redeemer: HttpRedeemer) -> None:
    respx.get(TARGET).mock(side_effect=httpx.ConnectTimeout("too slow"))

    attempt = redeemer.redeem(candidate())

    assert not attempt.success
    assert "timeout" in attempt.detail


@respx.mock
def test_connection_error_is_reported_not_raised(redeemer: HttpRedeemer) -> None:
    respx.get(TARGET).mock(side_effect=httpx.ConnectError("no route"))

    attempt = redeemer.redeem(candidate())

    assert not attempt.success
    assert "request failed" in attempt.detail


# ---------------------------------------------------------------------------
# Redirects are re-checked at every hop
# ---------------------------------------------------------------------------


@respx.mock
def test_redirect_within_the_allowlist_is_followed(redeemer: HttpRedeemer) -> None:
    respx.get(TARGET).mock(
        return_value=httpx.Response(302, headers={"location": "https://www.bidoo.com/grazie"})
    )
    final = respx.get("https://www.bidoo.com/grazie").mock(
        return_value=httpx.Response(200, html="<p>Grazie</p>")
    )

    attempt = redeemer.redeem(candidate())

    assert attempt.success
    assert final.called


@respx.mock
def test_redirect_leaving_the_allowlist_is_refused(redeemer: HttpRedeemer) -> None:
    respx.get(TARGET).mock(
        return_value=httpx.Response(302, headers={"location": "https://evil.example.invalid/x"})
    )
    offsite = respx.get("https://evil.example.invalid/x").mock(
        return_value=httpx.Response(200, text="pwned")
    )

    attempt = redeemer.redeem(candidate())

    assert not attempt.success
    assert "redirect refused" in attempt.detail
    assert not offsite.called, "the offsite hop must never be requested"


@respx.mock
def test_redirect_loop_stops_at_max_redirects(config: AppConfig, policy: UrlPolicy) -> None:
    data = default_config_dict()
    data["security"]["max_redirects"] = 2
    limited = build_config(data)
    redeemer = HttpRedeemer(limited.redeem.http, UrlPolicy(limited.security))
    respx.get(url__startswith="https://www.bidoo.com/").mock(
        return_value=httpx.Response(302, headers={"location": "https://www.bidoo.com/again"})
    )

    attempt = redeemer.redeem(candidate())

    assert not attempt.success
    assert "more than 2 redirects" in attempt.detail


@respx.mock
def test_redirect_without_location_is_a_failure(redeemer: HttpRedeemer) -> None:
    respx.get(TARGET).mock(return_value=httpx.Response(302))

    attempt = redeemer.redeem(candidate())

    assert not attempt.success
    assert "without a Location" in attempt.detail


# ---------------------------------------------------------------------------
# Body patterns
# ---------------------------------------------------------------------------


@respx.mock
def test_failure_pattern_turns_a_200_into_a_failure(policy: UrlPolicy) -> None:
    settings = config_with(failure_patterns=["(?i)sessione scaduta"]).redeem.http
    redeemer = HttpRedeemer(settings, policy)
    respx.get(TARGET).mock(return_value=httpx.Response(200, html="<p>Sessione scaduta</p>"))

    attempt = redeemer.redeem(candidate())

    assert not attempt.success
    assert "failure pattern" in attempt.detail


@respx.mock
def test_success_pattern_must_match_when_configured(policy: UrlPolicy) -> None:
    settings = config_with(success_patterns=["puntata (gratis )?riscattata"]).redeem.http
    redeemer = HttpRedeemer(settings, policy)
    respx.get(TARGET).mock(return_value=httpx.Response(200, html="<p>Qualcos'altro</p>"))

    attempt = redeemer.redeem(candidate())

    assert not attempt.success
    assert "no configured success pattern" in attempt.detail


@respx.mock
def test_success_pattern_match_is_reported(policy: UrlPolicy) -> None:
    settings = config_with(success_patterns=["puntata (gratis )?riscattata"]).redeem.http
    redeemer = HttpRedeemer(settings, policy)
    respx.get(TARGET).mock(
        return_value=httpx.Response(200, html="<p>Puntata gratis riscattata!</p>")
    )

    attempt = redeemer.redeem(candidate())

    assert attempt.success
    assert "matched" in attempt.detail


# ---------------------------------------------------------------------------
# The adapter enforces the policy on its own too
# ---------------------------------------------------------------------------


@respx.mock
def test_offsite_candidate_is_refused_without_a_request(redeemer: HttpRedeemer) -> None:
    route = respx.get("https://evil.example.invalid/claim").mock(return_value=httpx.Response(200))

    attempt = redeemer.redeem(candidate("https://evil.example.invalid/claim"))

    assert not attempt.success
    assert "refused by policy" in attempt.detail
    assert not route.called


@respx.mock
def test_final_url_is_logged_without_the_token(redeemer: HttpRedeemer) -> None:
    respx.get(TARGET).mock(return_value=httpx.Response(200, html="ok"))

    attempt = redeemer.redeem(candidate())

    assert attempt.final_url is not None
    assert "ABCDEFGH" not in attempt.final_url
    assert attempt.final_url.startswith("https://www.bidoo.com/promo/riscuoti")


def test_close_is_idempotent(redeemer: HttpRedeemer) -> None:
    redeemer.close()
    redeemer.close()


def test_injected_client_is_not_closed(config: AppConfig, policy: UrlPolicy) -> None:
    client = httpx.Client()
    HttpRedeemer(config.redeem.http, policy, client=client).close()

    assert not client.is_closed
    client.close()
