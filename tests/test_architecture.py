"""Executable version of the layering rule.

The core must stay free of adapters and vendor SDKs, otherwise "swap Telegram
for a web UI without rewriting the logic" quietly stops being true.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from bidoo_bot.application.redeem import RedeemService
from bidoo_bot.config import AppConfig
from bidoo_bot.container import build_service, build_service_factory
from tests.fakes import GOOD_HTML, CountingFactory, FakeMailbox, make_email

SRC = Path(__file__).resolve().parents[1] / "src" / "bidoo_bot"

#: Modules that make up the transport-independent core.
CORE_PATHS = (
    "application",
    "parsing",
    "models",
    "config.py",
    "security.py",
    "errors.py",
    "logging_config.py",
    "reporting.py",
)

#: Things the core must never reach for.
FORBIDDEN_PREFIXES = (
    "bidoo_bot.adapters",
    "telegram",
    "googleapiclient",
    "google",
    "google_auth_oauthlib",
    "httpx",
    "playwright",
    "requests",
)


def core_modules() -> list[Path]:
    modules: list[Path] = []
    for entry in CORE_PATHS:
        path = SRC / entry
        modules.extend(sorted(path.rglob("*.py")) if path.is_dir() else [path])
    return modules


def imported_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text("utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


@pytest.mark.parametrize("module", core_modules(), ids=lambda p: p.name)
def test_the_core_does_not_import_adapters_or_sdks(module: Path) -> None:
    offenders = sorted(
        name
        for name in imported_names(module)
        if any(name == prefix or name.startswith(prefix + ".") for prefix in FORBIDDEN_PREFIXES)
    )

    assert not offenders, f"{module.relative_to(SRC)} imports {offenders}"


def test_the_use_case_module_only_knows_ports() -> None:
    imports = imported_names(SRC / "application" / "redeem.py")

    assert "bidoo_bot.application.ports" in imports
    assert not any(name.startswith("bidoo_bot.adapters") for name in imports)


def test_ports_are_importable_without_any_sdk() -> None:
    """A serverless entry point should be able to import the core alone."""
    imports = imported_names(SRC / "application" / "ports.py")

    assert all(
        name.startswith(("bidoo_bot", "collections", "dataclasses", "typing", "__future__"))
        for name in imports
    )


# ---------------------------------------------------------------------------
# The composition root
# ---------------------------------------------------------------------------


def test_build_service_accepts_injected_adapters(config: AppConfig) -> None:
    mailbox = FakeMailbox(messages=[make_email(html=GOOD_HTML)])
    factory = CountingFactory(redeemer=None)  # type: ignore[arg-type]

    service = build_service(config, mailbox=mailbox, redeemer_factory=factory)
    report = service.run()

    assert isinstance(service, RedeemService)
    assert report.emails_found == 1
    assert factory.builds == 0


def test_build_service_does_not_touch_gmail_until_asked(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Injecting a mailbox must avoid the Gmail import path entirely."""

    def explode(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Gmail must not be constructed when a mailbox is injected")

    monkeypatch.setattr("bidoo_bot.adapters.gmail.client.GmailMailbox.from_config", explode)

    build_service(config, mailbox=FakeMailbox(), redeemer_factory=CountingFactory(redeemer=None))  # type: ignore[arg-type]


def test_service_factory_builds_a_fresh_service_each_time(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[int] = []

    def fake_build(cfg: AppConfig, **_kwargs: object) -> RedeemService:
        built.append(1)
        return build_service(
            cfg,
            mailbox=FakeMailbox(),
            redeemer_factory=CountingFactory(redeemer=None),  # type: ignore[arg-type]
        )

    monkeypatch.setattr("bidoo_bot.container.build_service", fake_build)
    factory = build_service_factory(config)

    factory()
    factory()

    assert len(built) == 2, "each Telegram command must get its own Gmail client"


def test_lazy_mailbox_defers_construction_until_used() -> None:
    """A missing OAuth token must not break service construction itself."""
    from bidoo_bot.container import LazyMailbox
    from bidoo_bot.errors import AuthError

    builds: list[int] = []

    def factory() -> FakeMailbox:
        builds.append(1)
        return FakeMailbox()

    mailbox = LazyMailbox(factory)
    assert builds == [], "nothing should be built before the first call"

    mailbox.check_connection()
    mailbox.search("label:Bidoo", max_results=1)

    assert builds == [1], "the real mailbox is built once and reused"

    def failing_factory() -> FakeMailbox:
        raise AuthError("Gmail is not authorised yet")

    with pytest.raises(AuthError):
        LazyMailbox(failing_factory).search("label:Bidoo", max_results=1)


def test_status_still_works_without_gmail_credentials(config: AppConfig) -> None:
    """`status` is what you run when things are broken; it must not raise."""
    from bidoo_bot.container import LazyMailbox
    from bidoo_bot.errors import AuthError

    def failing_factory() -> FakeMailbox:
        raise AuthError("Gmail is not authorised yet. Run `bidoo-bot gmail-auth`")

    service = build_service(
        config,
        mailbox=LazyMailbox(failing_factory),
        redeemer_factory=CountingFactory(redeemer=None),  # type: ignore[arg-type]
    )
    status = service.status()

    assert not status.mailbox_ok
    assert "gmail-auth" in status.mailbox_detail
    assert status.query, "the configuration snapshot must still be there"
    assert status.allowed_domains


def test_a_missing_credential_becomes_a_reported_error_not_a_crash(config: AppConfig) -> None:
    from bidoo_bot.container import LazyMailbox
    from bidoo_bot.errors import AuthError

    def failing_factory() -> FakeMailbox:
        raise AuthError("Gmail is not authorised yet")

    service = build_service(
        config,
        mailbox=LazyMailbox(failing_factory),
        redeemer_factory=CountingFactory(redeemer=None),  # type: ignore[arg-type]
    )
    report = service.run()

    assert report.errors == ("Gmail is not authorised yet",)
    assert report.emails_found == 0
