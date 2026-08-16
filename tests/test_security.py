"""URL allowlist tests -- the last thing between an email and a request."""

from __future__ import annotations

import pytest

from bidoo_bot.config import build_config, default_config_dict
from bidoo_bot.errors import SecurityPolicyError
from bidoo_bot.security import UrlPolicy


@pytest.fixture
def relaxed_policy() -> UrlPolicy:
    """Same allowlist, but http is tolerated and subdomains are not."""
    data = default_config_dict()
    data["security"]["require_https"] = False
    data["security"]["allow_subdomains"] = False
    return UrlPolicy(build_config(data).security)


@pytest.mark.parametrize(
    "url",
    [
        "https://bidoo.com/promo",
        "https://www.bidoo.com/promo/riscuoti?token=abc",
        "https://click.bidoo.com/r/xyz",
        "https://deep.nested.bidoo.it/x",
        "https://bidoo.com",
    ],
)
def test_allowed_urls(policy: UrlPolicy, url: str) -> None:
    assert policy.check(url).allowed


@pytest.mark.parametrize(
    ("url", "expected_reason"),
    [
        ("https://evil.example.invalid/claim", "not in security.allowed_domains"),
        ("http://www.bidoo.com/promo", "https is required"),
        ("ftp://www.bidoo.com/promo", "not http(s)"),
        ("javascript:alert(1)", "not http(s)"),
        ("", "empty URL"),
        ("https:///nohost", "no host"),
        # The classic look-alikes.
        ("https://bidoo.com.evil.invalid/x", "not in security.allowed_domains"),
        ("https://notbidoo.com/x", "not in security.allowed_domains"),
        ("https://bidoo.com@evil.invalid/x", "userinfo"),
    ],
)
def test_refused_urls(policy: UrlPolicy, url: str, expected_reason: str) -> None:
    decision = policy.check(url)

    assert not decision.allowed
    assert expected_reason in decision.reason


def test_subdomains_can_be_disallowed(relaxed_policy: UrlPolicy) -> None:
    assert relaxed_policy.check("https://bidoo.com/x").allowed
    assert not relaxed_policy.check("https://www.bidoo.com/x").allowed


def test_http_can_be_allowed(relaxed_policy: UrlPolicy) -> None:
    assert relaxed_policy.check("http://bidoo.com/x").allowed


def test_host_matching_ignores_case_and_trailing_dot(policy: UrlPolicy) -> None:
    assert policy.check("https://WWW.BIDOO.COM/x").allowed
    assert policy.check("https://www.bidoo.com./x").allowed


def test_ensure_raises_on_refusal(policy: UrlPolicy) -> None:
    policy.ensure("https://www.bidoo.com/x")

    with pytest.raises(SecurityPolicyError, match=r"not in security\.allowed_domains"):
        policy.ensure("https://evil.example.invalid/x")


def test_decision_is_falsy_when_refused(policy: UrlPolicy) -> None:
    assert not policy.check("https://evil.example.invalid/x")
    assert policy.check("https://bidoo.com/x")
