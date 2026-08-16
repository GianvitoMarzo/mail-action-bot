"""Shared pytest fixtures.

Every test runs against the *packaged defaults*, never against a config.yaml
that may exist in the working copy, so results do not depend on the machine.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from bidoo_bot.application.redeem import RedeemService
from bidoo_bot.config import AppConfig, build_config, default_config_dict
from bidoo_bot.logging_config import clear_registered_secrets
from bidoo_bot.parsing.action_parser import ActionParser
from bidoo_bot.security import UrlPolicy
from tests.fakes import CountingFactory, FakeMailbox, FakeRedeemer

FIXTURES = Path(__file__).parent / "fixtures"

_LEAKY_ENV = (
    "BIDOO_CONFIG",
    "BIDOO_DRY_RUN",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ALLOWED_USER_IDS",
    "TELEGRAM_ALLOWED_USER_ID",
)


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep the developer's own environment out of the tests."""
    for name in _LEAKY_ENV:
        monkeypatch.delenv(name, raising=False)
    clear_registered_secrets()
    yield
    clear_registered_secrets()


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def config() -> AppConfig:
    """The packaged defaults, validated."""
    return build_config(default_config_dict())


@pytest.fixture
def parser(config: AppConfig) -> ActionParser:
    return ActionParser(config.parser)


@pytest.fixture
def policy(config: AppConfig) -> UrlPolicy:
    return UrlPolicy(config.security)


@pytest.fixture
def mailbox() -> FakeMailbox:
    return FakeMailbox()


@pytest.fixture
def redeemer() -> FakeRedeemer:
    return FakeRedeemer()


@pytest.fixture
def factory(redeemer: FakeRedeemer) -> CountingFactory:
    return CountingFactory(redeemer=redeemer)


@pytest.fixture
def service(
    config: AppConfig,
    mailbox: FakeMailbox,
    parser: ActionParser,
    policy: UrlPolicy,
    factory: CountingFactory,
) -> RedeemService:
    """A fully wired service whose adapters are all fakes."""
    return RedeemService(
        config=config,
        mailbox=mailbox,
        parser=parser,
        policy=policy,
        redeemer_factory=factory,
        sleep=lambda _seconds: None,
    )


def read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text("utf-8")
