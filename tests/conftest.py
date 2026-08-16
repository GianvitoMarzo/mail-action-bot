"""Shared pytest fixtures.

Every test runs against the *packaged defaults*, never against a config.yaml
that may exist in the working copy, so results do not depend on the machine.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import dotenv
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


#: The real python-dotenv loader, kept so the one test that needs it can put it
#: back (see test_config.py::test_load_secrets_does_not_climb_to_a_parent_env).
real_load_dotenv = dotenv.load_dotenv


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Keep the developer's working copy out of the tests.

    Three separate leaks, all of which have actually bitten:

    * environment variables -- cleared;
    * ``.env`` -- ``load_secrets()`` reads one, and a developer running the
      suite from a configured checkout has a real Telegram token sitting right
      there. Neutralising the loader is what makes the suite hermetic; without
      it a test made a live API call with a real credential;
    * ``config.yaml`` -- ``load_config()`` falls back to the current directory,
      so the developer's own strategy and query silently changed what the CLI
      tests exercised. Running each test from an empty directory fixes that
      for good.

    Tests that need a config or a ``.env`` create one explicitly.
    """
    for name in _LEAKY_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *_args, **_kwargs: False)
    monkeypatch.chdir(tmp_path)
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
