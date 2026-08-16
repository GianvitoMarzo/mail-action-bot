"""Configuration loading, merging and validation."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from bidoo_bot.config import (
    build_config,
    default_config_dict,
    dry_run_from_env,
    find_config_file,
    load_config,
    load_secrets,
)
from bidoo_bot.errors import ConfigError

REPO_ROOT = Path(__file__).resolve().parents[1]


def write_config(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), "utf-8")
    return path


# ---------------------------------------------------------------------------
# Defaults and the committed example
# ---------------------------------------------------------------------------


def test_packaged_defaults_are_valid() -> None:
    config = build_config(default_config_dict())

    assert config.redeem.dry_run is True, "shipping a live-by-default bot would be reckless"
    assert config.security.allowed_domains
    assert config.parser.signals


def test_config_example_matches_the_packaged_defaults() -> None:
    """Keeps the committed example from drifting away from the real defaults."""
    example = yaml.safe_load((REPO_ROOT / "config.example.yaml").read_text("utf-8"))

    assert example == default_config_dict()


def test_config_example_is_loadable_on_its_own(tmp_path: Path) -> None:
    target = tmp_path / "config.yaml"
    target.write_text((REPO_ROOT / "config.example.yaml").read_text("utf-8"), "utf-8")

    config = load_config(target)

    assert config.source_path == target


# ---------------------------------------------------------------------------
# Merging
# ---------------------------------------------------------------------------


def test_user_config_is_deep_merged_over_the_defaults(tmp_path: Path) -> None:
    path = write_config(tmp_path, {"gmail": {"query": "label:MieAste"}})

    config = load_config(path)

    assert config.gmail.query == "label:MieAste"
    # Untouched keys keep their default.
    assert config.gmail.processed_label == "Bidoo/Processed"
    assert config.parser.signals


def test_lists_are_replaced_not_merged(tmp_path: Path) -> None:
    path = write_config(tmp_path, {"security": {"allowed_domains": ["only.example.invalid"]}})

    config = load_config(path)

    assert config.security.allowed_domains == ("only.example.invalid",)


def test_an_empty_config_file_is_fine(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("", "utf-8")

    assert load_config(path).gmail.query == default_config_dict()["gmail"]["query"]


def test_relative_paths_resolve_against_the_config_file(tmp_path: Path) -> None:
    path = write_config(tmp_path, {"gmail": {"token_file": "secrets/token.json"}})

    config = load_config(path)

    assert config.resolve(config.gmail.token_file) == tmp_path / "secrets" / "token.json"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_missing_config_file_is_an_error() -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config("/nowhere/config.yaml")


def test_broken_yaml_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("gmail: [unclosed", "utf-8")

    with pytest.raises(ConfigError, match="could not parse"):
        load_config(path)


def test_unknown_sections_are_rejected() -> None:
    data = default_config_dict()
    data["typo_section"] = {}

    with pytest.raises(ConfigError, match="unknown config section"):
        build_config(data)


def test_an_empty_allowlist_is_rejected() -> None:
    data = default_config_dict()
    data["security"]["allowed_domains"] = []

    with pytest.raises(ConfigError, match="at least one domain"):
        build_config(data)


def test_an_empty_query_is_rejected() -> None:
    """Refusing to scan the whole mailbox is a configuration invariant."""
    data = default_config_dict()
    data["gmail"]["query"] = "   "

    with pytest.raises(ConfigError, match="never scans the whole mailbox"):
        build_config(data)


def test_an_empty_processed_label_is_rejected() -> None:
    data = default_config_dict()
    data["gmail"]["processed_label"] = ""

    with pytest.raises(ConfigError, match="idempotency store"):
        build_config(data)


@pytest.mark.parametrize(
    ("section", "key", "value", "message"),
    [
        ("redeem", "strategy", "carrier-pigeon", "redeem.strategy"),
        ("parser", "min_confidence", 1.5, "must be <= 1"),
        ("parser", "min_confidence", "high", "must be a number"),
        ("gmail", "max_results", 0, "must be >= 1"),
        ("gmail", "max_results", "many", "must be an integer"),
        ("gmail", "query", 42, "must be a string"),
        ("redeem", "dry_run", "yes", "must be true or false"),
        ("logging", "level", "CHATTY", "standard logging level"),
        ("logging", "format", "xml", "must be 'text' or 'json'"),
    ],
)
def test_invalid_values_are_reported_precisely(
    section: str, key: str, value: object, message: str
) -> None:
    data = default_config_dict()
    data[section][key] = value

    with pytest.raises(ConfigError, match=message):
        build_config(data)


def test_a_bad_regex_is_reported_with_its_signal_name() -> None:
    data = default_config_dict()
    data["parser"]["signals"] = [
        {"name": "broken", "field": "text", "pattern": "([unclosed", "weight": 0.5}
    ]

    with pytest.raises(ConfigError, match="invalid regex"):
        build_config(data)


def test_duplicate_signal_names_are_rejected() -> None:
    data = default_config_dict()
    rule = {"name": "dup", "field": "text", "pattern": "a", "weight": 0.5}
    data["parser"]["signals"] = [rule, dict(rule)]

    with pytest.raises(ConfigError, match="duplicate signal name"):
        build_config(data)


def test_an_unknown_signal_field_is_rejected() -> None:
    data = default_config_dict()
    data["parser"]["signals"] = [{"name": "x", "field": "subject", "pattern": "a", "weight": 0.5}]

    with pytest.raises(ConfigError, match="field' must be one of"):
        build_config(data)


def test_a_domain_with_a_path_is_rejected() -> None:
    data = default_config_dict()
    data["security"]["allowed_domains"] = ["bidoo.com/promo"]

    with pytest.raises(ConfigError, match="bare hostname"):
        build_config(data)


# ---------------------------------------------------------------------------
# Effective query
# ---------------------------------------------------------------------------


def test_the_processed_label_is_excluded_from_the_query() -> None:
    config = build_config(default_config_dict())

    assert config.gmail.effective_query() == 'label:Bidoo newer_than:30d -label:"Bidoo/Processed"'


def test_the_exclusion_is_not_added_twice() -> None:
    data = default_config_dict()
    data["gmail"]["query"] = 'label:Bidoo -label:"Bidoo/Processed"'

    assert build_config(data).gmail.effective_query().count("-label:") == 1


def test_the_exclusion_can_be_turned_off() -> None:
    data = default_config_dict()
    data["gmail"]["exclude_processed_in_query"] = False

    assert build_config(data).gmail.effective_query() == "label:Bidoo newer_than:30d"


def test_with_dry_run_returns_a_copy() -> None:
    config = build_config(default_config_dict())

    live = config.with_dry_run(False)

    assert config.redeem.dry_run is True, "the original must not be mutated"
    assert live.redeem.dry_run is False


# ---------------------------------------------------------------------------
# Secrets and environment
# ---------------------------------------------------------------------------


def test_secrets_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:abcdefghijklmnopqrstuvwxyz0123456789")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "111, 222 333")

    secrets = load_secrets(load_dotenv_file=False)

    assert secrets.telegram_bot_token is not None
    assert secrets.telegram_allowed_user_ids == frozenset({111, 222, 333})


def test_the_singular_env_var_name_also_works(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_ID", "777")

    assert load_secrets(load_dotenv_file=False).telegram_allowed_user_ids == frozenset({777})


def test_missing_secrets_are_empty_not_an_error() -> None:
    secrets = load_secrets(load_dotenv_file=False)

    assert secrets.telegram_bot_token is None
    assert secrets.telegram_allowed_user_ids == frozenset()


def test_a_non_numeric_user_id_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "111,@mario")

    with pytest.raises(ConfigError, match="non numeric"):
        load_secrets(load_dotenv_file=False)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1", True), ("true", True), ("YES", True), ("0", False), ("off", False), ("", None)],
)
def test_dry_run_env_override(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool | None
) -> None:
    monkeypatch.setenv("BIDOO_DRY_RUN", raw)

    assert dry_run_from_env() is expected


def test_an_unparseable_dry_run_override_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BIDOO_DRY_RUN", "maybe")

    with pytest.raises(ConfigError, match="BIDOO_DRY_RUN"):
        dry_run_from_env()


def test_bidoo_config_env_var_is_honoured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_config(tmp_path, {"gmail": {"query": "label:FromEnv"}})
    monkeypatch.setenv("BIDOO_CONFIG", str(path))

    assert load_config().gmail.query == "label:FromEnv"
    assert find_config_file() == path


def test_a_missing_env_config_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BIDOO_CONFIG", "/nowhere/nope.yaml")

    with pytest.raises(ConfigError, match="BIDOO_CONFIG"):
        find_config_file()


# ---------------------------------------------------------------------------
# .env discovery
# ---------------------------------------------------------------------------


def test_load_secrets_does_not_climb_to_a_parent_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stray .env in a parent folder must not feed this tool a token.

    python-dotenv's default discovery walks *up* the tree, which would let an
    unrelated project's credentials leak into bidoo-bot.
    """
    import dotenv

    from tests.conftest import real_load_dotenv

    monkeypatch.setattr(dotenv, "load_dotenv", real_load_dotenv)

    (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=999:not-ours\n", "utf-8")
    workdir = tmp_path / "project"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    assert load_secrets().telegram_bot_token is None


def test_load_secrets_reads_the_env_file_in_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import dotenv

    from tests.conftest import real_load_dotenv

    monkeypatch.setattr(dotenv, "load_dotenv", real_load_dotenv)

    (tmp_path / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=123456:ours\nTELEGRAM_ALLOWED_USER_IDS=42\n", "utf-8"
    )
    monkeypatch.chdir(tmp_path)

    secrets = load_secrets()

    assert secrets.telegram_bot_token == "123456:ours"
    assert secrets.telegram_allowed_user_ids == frozenset({42})


def test_an_explicit_env_file_path_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import dotenv

    from tests.conftest import real_load_dotenv

    monkeypatch.setattr(dotenv, "load_dotenv", real_load_dotenv)

    elsewhere = tmp_path / "custom.env"
    elsewhere.write_text("TELEGRAM_ALLOWED_USER_IDS=7\n", "utf-8")
    monkeypatch.chdir(tmp_path)

    assert load_secrets(env_file=elsewhere).telegram_allowed_user_ids == frozenset({7})
