"""CLI tests.

The CLI must go through the same application service as Telegram, so the
``redeem`` test injects fakes by patching the composition root only.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from bidoo_bot import cli
from bidoo_bot.application.redeem import RedeemService
from bidoo_bot.config import AppConfig
from bidoo_bot.parsing.action_parser import ActionParser
from bidoo_bot.security import UrlPolicy
from tests.fakes import GOOD_HTML, CountingFactory, FakeMailbox, FakeRedeemer, make_email


def run(argv: list[str]) -> tuple[int, str]:
    out = io.StringIO()
    code = cli.main(argv, out=out)
    return code, out.getvalue()


@pytest.fixture
def wired(
    monkeypatch: pytest.MonkeyPatch, mailbox: FakeMailbox, redeemer: FakeRedeemer
) -> FakeMailbox:
    """Point ``build_service`` at the fakes, leaving everything else real."""

    def fake_build_service(config: AppConfig, **_kwargs: object) -> RedeemService:
        return RedeemService(
            config=config,
            mailbox=mailbox,
            parser=ActionParser(config.parser),
            policy=UrlPolicy(config.security),
            redeemer_factory=CountingFactory(redeemer=redeemer),
            sleep=lambda _seconds: None,
        )

    monkeypatch.setattr(cli, "build_service", fake_build_service)
    return mailbox


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------


def test_no_command_prints_help() -> None:
    code, output = run([])

    assert code == 0
    assert "usage: bidoo-bot" in output
    assert "analyze-email" in output


def test_version_is_exposed() -> None:
    with pytest.raises(SystemExit) as excinfo:
        run(["--version"])

    assert excinfo.value.code == 0


def test_check_config_summarises_the_configuration() -> None:
    code, output = run(["check-config"])

    assert code == 0
    assert "Config OK" in output
    assert "allowed domains:  bidoo.com, bidoo.it" in output
    assert "dry run:          True" in output


def test_a_broken_config_exits_cleanly(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    bad = tmp_path / "config.yaml"
    bad.write_text("security:\n  allowed_domains: []\n", "utf-8")

    code, _ = run(["--config", str(bad), "check-config"])

    assert code == cli.EXIT_ERROR
    assert "Configuration error" in capsys.readouterr().err


def test_a_missing_config_file_exits_cleanly(capsys: pytest.CaptureFixture) -> None:
    code, _ = run(["--config", "/nowhere/config.yaml", "check-config"])

    assert code == cli.EXIT_ERROR
    assert "not found" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# analyze-email
# ---------------------------------------------------------------------------


def test_analyze_email_reports_a_good_candidate(fixtures_dir: Path) -> None:
    code, output = run(["analyze-email", str(fixtures_dir / "free_bid_it.html")])

    assert code == cli.EXIT_OK
    assert "Status:  OK" in output
    assert "RISCUOTI LA TUA PUNTATA GRATIS" in output
    assert "✅ allowed" in output
    assert "would be redeemed" in output


def test_analyze_email_flags_an_offsite_candidate(fixtures_dir: Path) -> None:
    code, output = run(["analyze-email", str(fixtures_dir / "external_domain.html")])

    assert code == cli.EXIT_NO_CANDIDATE
    assert "⛔ REJECTED" in output
    assert "NOT on the allowlist" in output


def test_analyze_email_flags_ambiguity(fixtures_dir: Path) -> None:
    code, output = run(["analyze-email", str(fixtures_dir / "ambiguous.html")])

    assert code == cli.EXIT_NO_CANDIDATE
    assert "Status:  AMBIGUOUS" in output


def test_analyze_email_reads_an_eml_file(fixtures_dir: Path) -> None:
    code, output = run(["analyze-email", str(fixtures_dir / "sample_email.eml")])

    assert code == cli.EXIT_OK
    assert "La tua puntata gratis ti aspetta" in output


def test_analyze_email_json_output(fixtures_dir: Path) -> None:
    code, output = run(["analyze-email", str(fixtures_dir / "free_bid_it.html"), "--json"])

    payload = json.loads(output)
    assert code == cli.EXIT_OK
    assert payload["status"] == "OK"
    best = next(c for c in payload["candidates"] if c["best"])
    assert best["allowed"] is True
    assert best["confidence"] > 0.9


def test_analyze_email_shows_reasons_with_all(fixtures_dir: Path) -> None:
    _code, output = run(["analyze-email", str(fixtures_dir / "free_bid_it.html"), "--all"])

    assert "matched redeem-verb-it" in output


def test_analyze_email_on_a_missing_file(capsys: pytest.CaptureFixture) -> None:
    code, _ = run(["analyze-email", "/nowhere/mail.eml"])

    assert code == cli.EXIT_ERROR
    assert "file not found" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# redeem
# ---------------------------------------------------------------------------


def test_redeem_defaults_to_dry_run(wired: FakeMailbox, redeemer: FakeRedeemer) -> None:
    wired.messages = [make_email(html=GOOD_HTML)]

    code, output = run(["redeem"])

    assert code == cli.EXIT_OK
    assert "DRY RUN" in output
    assert redeemer.calls == []


def test_redeem_no_dry_run_executes(wired: FakeMailbox, redeemer: FakeRedeemer) -> None:
    wired.messages = [make_email(html=GOOD_HTML)]

    code, output = run(["redeem", "--no-dry-run"])

    assert code == cli.EXIT_OK
    assert "✅ Redeemed: 1" in output
    assert len(redeemer.calls) == 1


def test_redeem_ignores_the_removed_env_var(
    wired: FakeMailbox, redeemer: FakeRedeemer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BIDOO_DRY_RUN is gone: config.yaml decides, and dry-run stays on."""
    monkeypatch.setenv("BIDOO_DRY_RUN", "0")
    wired.messages = [make_email(html=GOOD_HTML)]

    code, output = run(["redeem"])

    assert code == cli.EXIT_OK
    assert redeemer.calls == [], "a stale env var must never enable execution"
    assert "DRY RUN" in output


def test_a_stale_env_var_is_warned_about(
    wired: FakeMailbox, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    # Asserted on stderr rather than caplog: configure_logging() installs its
    # own handler and drops the ones already on the root logger.
    monkeypatch.setenv("BIDOO_DRY_RUN", "0")
    wired.messages = []

    run(["redeem"])

    stderr = capsys.readouterr().err
    assert "BIDOO_DRY_RUN" in stderr
    assert "no longer does anything" in stderr


def test_the_flag_still_overrides_the_config(wired: FakeMailbox, redeemer: FakeRedeemer) -> None:
    wired.messages = [make_email(html=GOOD_HTML)]

    run(["redeem", "--no-dry-run"])
    assert len(redeemer.calls) == 1

    run(["redeem", "--dry-run"])
    assert len(redeemer.calls) == 1, "--dry-run must put it back"


def test_redeem_json_output(wired: FakeMailbox) -> None:
    wired.messages = [make_email(html=GOOD_HTML)]

    code, output = run(["redeem", "--json"])

    payload = json.loads(output)
    assert code == cli.EXIT_OK
    assert payload["dry_run"] is True
    assert payload["counts"]["DRY_RUN"] == 1


def test_redeem_overrides_query_and_limit(wired: FakeMailbox) -> None:
    wired.messages = [make_email(f"m{i}", html=GOOD_HTML) for i in range(5)]

    run(["redeem", "--query", "label:Altro", "--max-results", "2"])

    assert wired.searches == [("label:Altro", 2)]


def test_redeem_exits_nonzero_when_something_failed(wired: FakeMailbox) -> None:
    wired.search_error = "Gmail API error while searching messages"

    code, output = run(["redeem"])

    assert code == cli.EXIT_ERROR
    assert "Warnings:" in output


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_command(wired: FakeMailbox) -> None:
    code, output = run(["status"])

    assert code == cli.EXIT_OK
    assert "bidoo-bot status" in output
    assert "gmail: ✅ connected" in output


def test_status_json(wired: FakeMailbox) -> None:
    code, output = run(["status", "--json"])

    payload = json.loads(output)
    assert code == cli.EXIT_OK
    assert payload["dry_run"] is True
    assert payload["allowed_domains"] == ["bidoo.com", "bidoo.it"]


def test_status_exits_nonzero_when_gmail_is_unreachable(wired: FakeMailbox) -> None:
    wired.connected = False

    code, output = run(["status"])

    assert code == cli.EXIT_ERROR
    assert "not available" in output


# ---------------------------------------------------------------------------
# bot
# ---------------------------------------------------------------------------


def test_bot_refuses_to_start_without_secrets(capsys: pytest.CaptureFixture) -> None:
    code, _ = run(["bot"])

    assert code == cli.EXIT_ERROR
    assert "TELEGRAM_BOT_TOKEN" in capsys.readouterr().err


def test_bot_starts_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:abcdefghijklmnopqrstuvwxyz0123456789")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "42")
    started: dict[str, object] = {}

    def fake_run_bot(*, config, secrets, service_factory) -> None:
        started["dry_run"] = config.redeem.dry_run
        started["ids"] = secrets.telegram_allowed_user_ids

    monkeypatch.setattr("bidoo_bot.adapters.telegram.bot.run_bot", fake_run_bot)

    code, output = run(["bot"])

    assert code == cli.EXIT_OK
    assert started["ids"] == frozenset({42})
    assert "DRY RUN" in output


# ---------------------------------------------------------------------------
# Logging flags
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("flags", "expected"),
    [([], "INFO"), (["-v"], "INFO"), (["-vv"], "DEBUG"), (["-q"], "WARNING")],
)
@pytest.mark.parametrize("position", ["before", "after"])
def test_verbosity_flags_work_on_either_side_of_the_subcommand(
    flags: list[str], expected: str, position: str
) -> None:
    """Regression: `redeem --no-dry-run -v` used to fail with
    "unrecognized arguments: -v", because the flag was declared only on the
    top-level parser. Writing it after the subcommand is the natural order."""
    import logging

    argv = [*flags, "check-config"] if position == "before" else ["check-config", *flags]

    code, _ = run(argv)

    assert code == cli.EXIT_OK
    assert logging.getLogger().level == getattr(logging, expected)


@pytest.mark.parametrize("position", ["before", "after"])
def test_config_flag_works_on_either_side(tmp_path: Path, position: str) -> None:
    import yaml

    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.safe_dump({"gmail": {"query": "label:Ordering"}}), "utf-8")
    flags = ["--config", str(config_file)]
    argv = [*flags, "check-config"] if position == "before" else ["check-config", *flags]

    code, output = run(argv)

    assert code == cli.EXIT_OK
    assert "label:Ordering" in output


def test_a_flag_after_the_subcommand_does_not_undo_one_given_before() -> None:
    """The subparser copies default to SUPPRESS precisely for this."""
    import logging

    code, _ = run(["-vv", "check-config"])

    assert code == cli.EXIT_OK
    assert logging.getLogger().level == logging.DEBUG


def test_redeem_accepts_verbosity_after_its_own_flags(
    wired: FakeMailbox, redeemer: FakeRedeemer
) -> None:
    """The exact shape that used to fail."""
    wired.messages = [make_email(html=GOOD_HTML)]

    code, output = run(["redeem", "--no-dry-run", "-v"])

    assert code == cli.EXIT_OK
    assert len(redeemer.calls) == 1
    assert "Redeemed: 1" in output


# ---------------------------------------------------------------------------
# telegram-whoami
# ---------------------------------------------------------------------------


def test_whoami_needs_a_token_first(capsys: pytest.CaptureFixture) -> None:
    code, _ = run(["telegram-whoami"])

    assert code == cli.EXIT_ERROR
    assert "TELEGRAM_BOT_TOKEN is not set" in capsys.readouterr().err


def test_whoami_lists_senders_and_the_line_to_paste(monkeypatch: pytest.MonkeyPatch) -> None:
    from bidoo_bot.adapters.telegram.bot import TelegramSender

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:abcdefghijklmnopqrstuvwxyz0123456789")
    monkeypatch.setattr(
        "bidoo_bot.adapters.telegram.bot.fetch_recent_user_ids",
        lambda _token: [TelegramSender(424242, "Owner"), TelegramSender(999, "Someone")],
    )

    code, output = run(["telegram-whoami"])

    assert code == cli.EXIT_OK
    assert "424242 — Owner" in output
    assert "TELEGRAM_ALLOWED_USER_IDS=424242,999" in output
    assert "do not recognise" in output, "must warn before pasting a stranger's id"


def test_whoami_tells_you_to_message_the_bot_first(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:abcdefghijklmnopqrstuvwxyz0123456789")
    monkeypatch.setattr("bidoo_bot.adapters.telegram.bot.fetch_recent_user_ids", lambda _token: [])

    code, output = run(["telegram-whoami"])

    assert code == cli.EXIT_ERROR
    assert "send any message to your bot" in output


def test_bot_error_without_an_allowlist_points_at_whoami(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """The exact dead-end a first-time user hits."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:abcdefghijklmnopqrstuvwxyz0123456789")

    code, _ = run(["bot"])

    assert code == cli.EXIT_ERROR
    assert "telegram-whoami" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# check-config: pinned scoring rules
# ---------------------------------------------------------------------------


def test_check_config_flags_a_config_that_pins_the_rules(tmp_path: Path) -> None:
    """A config.yaml copied wholesale freezes the rule set; say so."""
    import yaml

    from bidoo_bot.config import default_config_dict

    data = default_config_dict()
    data["parser"]["signals"] = data["parser"]["signals"][:3]
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.safe_dump(data), "utf-8")

    _code, output = run(["--config", str(config_file), "check-config"])

    assert "pinned by your config.yaml" in output
    assert "delete the parser.signals block" in output


def test_check_config_is_quiet_for_a_minimal_config(tmp_path: Path) -> None:
    import yaml

    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.safe_dump({"gmail": {"query": "label:Bidoo"}}), "utf-8")

    _code, output = run(["--config", str(config_file), "check-config"])

    assert "packaged defaults" in output
    assert "pinned" not in output


def test_check_config_reports_defaults_when_there_is_no_config_file() -> None:
    _code, output = run(["check-config"])

    assert "(packaged defaults)" in output
