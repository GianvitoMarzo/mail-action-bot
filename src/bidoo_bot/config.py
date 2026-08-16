"""Configuration loading.

Two clearly separated sources:

* **config.yaml** -- non sensitive application settings, safe to commit.
  Deep-merged on top of the defaults packaged in ``default_config.yaml``.
* **environment / .env** -- secrets only (Telegram token, allowed user ids).

Nothing sensitive is ever read from the YAML file, and nothing from the YAML
file is ever a secret.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from importlib import resources
from pathlib import Path
from typing import Any, Literal, cast

import yaml

from bidoo_bot.errors import ConfigError

DEFAULT_CONFIG_RESOURCE = "default_config.yaml"
DEFAULT_CONFIG_FILENAMES = ("config.yaml", "config.yml")

SignalField = Literal["text", "url", "attrs", "context", "any"]
_SIGNAL_FIELDS: tuple[str, ...] = ("text", "url", "attrs", "context", "any")
_STRATEGIES: tuple[str, ...] = ("http", "playwright", "manual")
_ON_CONFIRM: tuple[str, ...] = ("trash", "label")


# ---------------------------------------------------------------------------
# Settings dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SignalRule:
    """One scoring rule used by the email parser."""

    name: str
    field: SignalField
    pattern: re.Pattern[str]
    weight: float
    negative: bool = False


@dataclass(frozen=True, slots=True)
class GmailSettings:
    query: str
    max_results: int
    processed_label: str
    failed_label: str | None
    exclude_processed_in_query: bool
    credentials_file: Path
    token_file: Path

    def effective_query(self) -> str:
        """The query actually sent to Gmail, with the idempotency filter."""
        query = self.query.strip()
        if self.exclude_processed_in_query and self.processed_label:
            exclusion = f'-label:"{self.processed_label}"'
            if exclusion not in query:
                query = f"{query} {exclusion}".strip()
        return query


@dataclass(frozen=True, slots=True)
class SecuritySettings:
    allowed_domains: tuple[str, ...]
    allow_subdomains: bool
    require_https: bool
    max_redirects: int


@dataclass(frozen=True, slots=True)
class ParserSettings:
    min_confidence: float
    ambiguity_margin: float
    max_links: int
    context_chars: int
    case_sensitive: bool
    signals: tuple[SignalRule, ...]


@dataclass(frozen=True, slots=True)
class HttpSettings:
    timeout_seconds: float
    verify_tls: bool
    user_agent: str
    accept_language: str
    success_patterns: tuple[re.Pattern[str], ...]
    failure_patterns: tuple[re.Pattern[str], ...]


@dataclass(frozen=True, slots=True)
class PlaywrightSettings:
    browser: str
    headless: bool
    user_data_dir: Path
    timeout_seconds: float
    wait_until: str
    settle_seconds: float
    success_patterns: tuple[re.Pattern[str], ...]
    failure_patterns: tuple[re.Pattern[str], ...]
    screenshot_dir: Path | None


@dataclass(frozen=True, slots=True)
class ManualSettings:
    """Settings for ``strategy: manual``, where *you* open the link."""

    on_confirm: str
    """``trash`` moves the mail to Gmail's Trash, ``label`` only labels it."""

    @property
    def trash_on_confirm(self) -> bool:
        return self.on_confirm == "trash"


@dataclass(frozen=True, slots=True)
class RedeemSettings:
    strategy: str
    dry_run: bool
    delay_between_actions_seconds: float
    http: HttpSettings
    playwright: PlaywrightSettings
    manual: ManualSettings

    @property
    def is_manual(self) -> bool:
        return self.strategy == "manual"


@dataclass(frozen=True, slots=True)
class TelegramSettings:
    max_detail_lines: int
    show_urls_in_dry_run: bool


@dataclass(frozen=True, slots=True)
class LoggingSettings:
    level: str
    format: str
    redact: bool


@dataclass(frozen=True, slots=True)
class AppConfig:
    gmail: GmailSettings
    security: SecuritySettings
    parser: ParserSettings
    redeem: RedeemSettings
    telegram: TelegramSettings
    logging: LoggingSettings
    source_path: Path | None = None
    base_dir: Path = field(default_factory=Path.cwd)

    def with_dry_run(self, dry_run: bool) -> AppConfig:
        """Return a copy with the dry-run flag forced to ``dry_run``."""
        return replace(self, redeem=replace(self.redeem, dry_run=dry_run))

    def resolve(self, path: Path) -> Path:
        """Resolve a configured relative path against the config's base dir."""
        return path if path.is_absolute() else (self.base_dir / path)


@dataclass(frozen=True, slots=True)
class Secrets:
    """Values that must never appear in the repository or in a log line."""

    telegram_bot_token: str | None = None
    telegram_allowed_user_ids: frozenset[int] = frozenset()

    def require_telegram(self) -> tuple[str, frozenset[int]]:
        if not self.telegram_bot_token:
            raise ConfigError(
                "TELEGRAM_BOT_TOKEN is not set. Copy .env.example to .env and fill it in."
            )
        if not self.telegram_allowed_user_ids:
            raise ConfigError(
                "TELEGRAM_ALLOWED_USER_IDS is empty. Refusing to start a bot anyone "
                "could use.\nTo find your own id: send any message to your bot in "
                "Telegram, then run `bidoo-bot telegram-whoami`."
            )
        return self.telegram_bot_token, self.telegram_allowed_user_ids


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` into ``base``. Lists are replaced, not merged."""
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def _section(data: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, Mapping):
        raise ConfigError(f"config section '{name}' must be a mapping, got {type(value).__name__}")
    return value


def _get(data: Mapping[str, Any], key: str, where: str) -> Any:
    if key not in data:
        raise ConfigError(f"missing config key '{where}.{key}'")
    return data[key]


def _as_str(data: Mapping[str, Any], key: str, where: str) -> str:
    value = _get(data, key, where)
    if not isinstance(value, str):
        raise ConfigError(f"'{where}.{key}' must be a string")
    return value


def _as_opt_str(data: Mapping[str, Any], key: str, where: str) -> str | None:
    value = _get(data, key, where)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"'{where}.{key}' must be a string or null")
    return value


def _as_bool(data: Mapping[str, Any], key: str, where: str) -> bool:
    value = _get(data, key, where)
    if not isinstance(value, bool):
        raise ConfigError(f"'{where}.{key}' must be true or false")
    return value


def _as_int(data: Mapping[str, Any], key: str, where: str, *, minimum: int | None = None) -> int:
    value = _get(data, key, where)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"'{where}.{key}' must be an integer")
    if minimum is not None and value < minimum:
        raise ConfigError(f"'{where}.{key}' must be >= {minimum}")
    return value


def _as_float(
    data: Mapping[str, Any],
    key: str,
    where: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    value = _get(data, key, where)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"'{where}.{key}' must be a number")
    number = float(value)
    if minimum is not None and number < minimum:
        raise ConfigError(f"'{where}.{key}' must be >= {minimum}")
    if maximum is not None and number > maximum:
        raise ConfigError(f"'{where}.{key}' must be <= {maximum}")
    return number


def _as_str_tuple(data: Mapping[str, Any], key: str, where: str) -> tuple[str, ...]:
    value = _get(data, key, where)
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ConfigError(f"'{where}.{key}' must be a list of strings")
    items: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise ConfigError(f"'{where}.{key}[{index}]' must be a string")
        items.append(item)
    return tuple(items)


def _compile(
    patterns: Sequence[str], where: str, *, flags: int = re.IGNORECASE
) -> tuple[re.Pattern[str], ...]:
    compiled: list[re.Pattern[str]] = []
    for index, pattern in enumerate(patterns):
        try:
            compiled.append(re.compile(pattern, flags))
        except re.error as exc:
            raise ConfigError(f"invalid regex in '{where}[{index}]': {exc}") from exc
    return tuple(compiled)


def _parse_signals(raw: Any, *, case_sensitive: bool) -> tuple[SignalRule, ...]:
    if isinstance(raw, str) or not isinstance(raw, Sequence):
        raise ConfigError("'parser.signals' must be a list")
    flags = 0 if case_sensitive else re.IGNORECASE
    rules: list[SignalRule] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        where = f"parser.signals[{index}]"
        if not isinstance(item, Mapping):
            raise ConfigError(f"'{where}' must be a mapping")
        name = _as_str(item, "name", where)
        if name in seen:
            raise ConfigError(f"duplicate signal name '{name}' in parser.signals")
        seen.add(name)
        field_name = _as_str(item, "field", where)
        if field_name not in _SIGNAL_FIELDS:
            raise ConfigError(f"'{where}.field' must be one of {', '.join(_SIGNAL_FIELDS)}")
        kind = item.get("kind", "positive")
        if kind not in ("positive", "negative"):
            raise ConfigError(f"'{where}.kind' must be 'positive' or 'negative'")
        weight = _as_float(item, "weight", where, minimum=0.0, maximum=1.0)
        pattern_source = _as_str(item, "pattern", where)
        try:
            pattern = re.compile(pattern_source, flags)
        except re.error as exc:
            raise ConfigError(f"invalid regex in '{where}.pattern': {exc}") from exc
        rules.append(
            SignalRule(
                name=name,
                field=cast(SignalField, field_name),
                pattern=pattern,
                weight=weight,
                negative=kind == "negative",
            )
        )
    if not rules:
        raise ConfigError("'parser.signals' must contain at least one rule")
    return tuple(rules)


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------


def _build_gmail(data: Mapping[str, Any]) -> GmailSettings:
    where = "gmail"
    query = _as_str(data, "query", where).strip()
    if not query:
        raise ConfigError("'gmail.query' must not be empty: the bot never scans the whole mailbox")
    processed_label = _as_str(data, "processed_label", where).strip()
    if not processed_label:
        raise ConfigError("'gmail.processed_label' must not be empty: it is the idempotency store")
    failed_label = _as_opt_str(data, "failed_label", where)
    return GmailSettings(
        query=query,
        max_results=_as_int(data, "max_results", where, minimum=1),
        processed_label=processed_label,
        failed_label=failed_label.strip() if failed_label else None,
        exclude_processed_in_query=_as_bool(data, "exclude_processed_in_query", where),
        credentials_file=Path(_as_str(data, "credentials_file", where)).expanduser(),
        token_file=Path(_as_str(data, "token_file", where)).expanduser(),
    )


def _build_security(data: Mapping[str, Any]) -> SecuritySettings:
    where = "security"
    domains = tuple(
        d.strip().lower().lstrip(".") for d in _as_str_tuple(data, "allowed_domains", where)
    )
    if not domains or any(not d for d in domains):
        raise ConfigError(
            "'security.allowed_domains' must list at least one domain; "
            "an empty allowlist would mean 'click anything'"
        )
    for domain in domains:
        if "/" in domain or ":" in domain:
            raise ConfigError(
                f"'security.allowed_domains' entry '{domain}' must be a bare hostname"
            )
    return SecuritySettings(
        allowed_domains=domains,
        allow_subdomains=_as_bool(data, "allow_subdomains", where),
        require_https=_as_bool(data, "require_https", where),
        max_redirects=_as_int(data, "max_redirects", where, minimum=0),
    )


def _build_parser(data: Mapping[str, Any]) -> ParserSettings:
    where = "parser"
    case_sensitive = _as_bool(data, "case_sensitive", where)
    return ParserSettings(
        min_confidence=_as_float(data, "min_confidence", where, minimum=0.0, maximum=1.0),
        ambiguity_margin=_as_float(data, "ambiguity_margin", where, minimum=0.0, maximum=1.0),
        max_links=_as_int(data, "max_links", where, minimum=1),
        context_chars=_as_int(data, "context_chars", where, minimum=0),
        case_sensitive=case_sensitive,
        signals=_parse_signals(_get(data, "signals", where), case_sensitive=case_sensitive),
    )


def _build_http(data: Mapping[str, Any]) -> HttpSettings:
    where = "redeem.http"
    return HttpSettings(
        timeout_seconds=_as_float(data, "timeout_seconds", where, minimum=0.1),
        verify_tls=_as_bool(data, "verify_tls", where),
        user_agent=_as_str(data, "user_agent", where),
        accept_language=_as_str(data, "accept_language", where),
        success_patterns=_compile(
            _as_str_tuple(data, "success_patterns", where), f"{where}.success_patterns"
        ),
        failure_patterns=_compile(
            _as_str_tuple(data, "failure_patterns", where), f"{where}.failure_patterns"
        ),
    )


def _build_playwright(data: Mapping[str, Any]) -> PlaywrightSettings:
    where = "redeem.playwright"
    browser = _as_str(data, "browser", where)
    if browser not in ("chromium", "firefox", "webkit"):
        raise ConfigError(f"'{where}.browser' must be chromium, firefox or webkit")
    wait_until = _as_str(data, "wait_until", where)
    if wait_until not in ("commit", "domcontentloaded", "load", "networkidle"):
        raise ConfigError(
            f"'{where}.wait_until' must be commit, domcontentloaded, load or networkidle"
        )
    screenshot_dir = _as_opt_str(data, "screenshot_dir", where)
    return PlaywrightSettings(
        browser=browser,
        headless=_as_bool(data, "headless", where),
        user_data_dir=Path(_as_str(data, "user_data_dir", where)).expanduser(),
        timeout_seconds=_as_float(data, "timeout_seconds", where, minimum=1.0),
        wait_until=wait_until,
        settle_seconds=_as_float(data, "settle_seconds", where, minimum=0.0),
        success_patterns=_compile(
            _as_str_tuple(data, "success_patterns", where), f"{where}.success_patterns"
        ),
        failure_patterns=_compile(
            _as_str_tuple(data, "failure_patterns", where), f"{where}.failure_patterns"
        ),
        screenshot_dir=Path(screenshot_dir).expanduser() if screenshot_dir else None,
    )


def _build_manual(data: Mapping[str, Any]) -> ManualSettings:
    where = "redeem.manual"
    on_confirm = _as_str(data, "on_confirm", where)
    if on_confirm not in _ON_CONFIRM:
        raise ConfigError(f"'{where}.on_confirm' must be one of {', '.join(_ON_CONFIRM)}")
    return ManualSettings(on_confirm=on_confirm)


def _build_redeem(data: Mapping[str, Any]) -> RedeemSettings:
    where = "redeem"
    strategy = _as_str(data, "strategy", where)
    if strategy not in _STRATEGIES:
        raise ConfigError(f"'{where}.strategy' must be one of {', '.join(_STRATEGIES)}")
    return RedeemSettings(
        strategy=strategy,
        dry_run=_as_bool(data, "dry_run", where),
        delay_between_actions_seconds=_as_float(
            data, "delay_between_actions_seconds", where, minimum=0.0
        ),
        http=_build_http(_section(data, "http")),
        playwright=_build_playwright(_section(data, "playwright")),
        manual=_build_manual(_section(data, "manual")),
    )


def _build_telegram(data: Mapping[str, Any]) -> TelegramSettings:
    where = "telegram"
    return TelegramSettings(
        max_detail_lines=_as_int(data, "max_detail_lines", where, minimum=0),
        show_urls_in_dry_run=_as_bool(data, "show_urls_in_dry_run", where),
    )


def _build_logging(data: Mapping[str, Any]) -> LoggingSettings:
    where = "logging"
    level = _as_str(data, "level", where).upper()
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        raise ConfigError(f"'{where}.level' must be a standard logging level name")
    fmt = _as_str(data, "format", where).lower()
    if fmt not in ("text", "json"):
        raise ConfigError(f"'{where}.format' must be 'text' or 'json'")
    return LoggingSettings(level=level, format=fmt, redact=_as_bool(data, "redact", where))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def default_config_dict() -> dict[str, Any]:
    """The packaged defaults, as a plain dict."""
    text = resources.files("bidoo_bot").joinpath(DEFAULT_CONFIG_RESOURCE).read_text("utf-8")
    data = yaml.safe_load(text)
    if not isinstance(data, Mapping):
        raise ConfigError("packaged default_config.yaml is not a mapping")
    return dict(data)


def build_config(
    data: Mapping[str, Any],
    *,
    source_path: Path | None = None,
    base_dir: Path | None = None,
) -> AppConfig:
    """Validate an already merged config mapping into an :class:`AppConfig`."""
    known = {"gmail", "security", "parser", "redeem", "telegram", "logging"}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ConfigError(
            f"unknown config section(s): {', '.join(unknown)}. "
            f"Known sections: {', '.join(sorted(known))}"
        )
    return AppConfig(
        gmail=_build_gmail(_section(data, "gmail")),
        security=_build_security(_section(data, "security")),
        parser=_build_parser(_section(data, "parser")),
        redeem=_build_redeem(_section(data, "redeem")),
        telegram=_build_telegram(_section(data, "telegram")),
        logging=_build_logging(_section(data, "logging")),
        source_path=source_path,
        base_dir=base_dir or (source_path.parent if source_path else Path.cwd()),
    )


def find_config_file(explicit: Path | str | None = None) -> Path | None:
    """Locate the user config file, or ``None`` when only defaults apply."""
    if explicit is not None:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise ConfigError(f"config file not found: {path}")
        return path
    env_path = os.environ.get("BIDOO_CONFIG")
    if env_path:
        path = Path(env_path).expanduser()
        if not path.is_file():
            raise ConfigError(f"config file from BIDOO_CONFIG not found: {path}")
        return path
    for name in DEFAULT_CONFIG_FILENAMES:
        candidate = Path.cwd() / name
        if candidate.is_file():
            return candidate
    return None


def load_config(path: Path | str | None = None) -> AppConfig:
    """Load defaults, deep-merge the user config on top, validate the result.

    A missing user config is not an error: the packaged defaults are usable for
    everything except the parts that need your own Gmail query and domains.
    """
    config_path = find_config_file(path)
    data = default_config_dict()
    if config_path is not None:
        try:
            raw = yaml.safe_load(config_path.read_text("utf-8"))
        except yaml.YAMLError as exc:
            raise ConfigError(f"could not parse {config_path}: {exc}") from exc
        except OSError as exc:
            raise ConfigError(f"could not read {config_path}: {exc}") from exc
        if raw is None:
            raw = {}
        if not isinstance(raw, Mapping):
            raise ConfigError(f"{config_path} must contain a YAML mapping at the top level")
        data = _deep_merge(data, raw)
    return build_config(data, source_path=config_path)


def _parse_user_ids(raw: str | None) -> frozenset[int]:
    if not raw:
        return frozenset()
    ids: set[int] = set()
    for chunk in re.split(r"[,\s]+", raw.strip()):
        if not chunk:
            continue
        try:
            ids.add(int(chunk))
        except ValueError as exc:
            raise ConfigError(
                f"TELEGRAM_ALLOWED_USER_IDS contains a non numeric entry: {chunk!r}"
            ) from exc
    return frozenset(ids)


def load_secrets(*, env_file: Path | str | None = None, load_dotenv_file: bool = True) -> Secrets:
    """Read secrets from the environment, optionally seeded by a ``.env`` file.

    Real environment variables always win over the ``.env`` file.

    The path is always explicit -- ``./.env`` unless told otherwise. python-dotenv
    would otherwise walk *up* the directory tree, so a stray ``.env`` in a parent
    folder could silently hand this tool a token meant for something else.
    """
    if load_dotenv_file:
        try:
            from dotenv import load_dotenv
        except ImportError:  # pragma: no cover - dotenv is a hard dependency
            pass
        else:
            path = Path(env_file).expanduser() if env_file else Path.cwd() / ".env"
            load_dotenv(dotenv_path=path, override=False)

    token = os.environ.get("TELEGRAM_BOT_TOKEN") or None
    ids = _parse_user_ids(
        os.environ.get("TELEGRAM_ALLOWED_USER_IDS") or os.environ.get("TELEGRAM_ALLOWED_USER_ID")
    )
    return Secrets(telegram_bot_token=token, telegram_allowed_user_ids=ids)


#: Environment variables that used to do something and no longer do, mapped to
#: what replaced them.
#:
#: BIDOO_DRY_RUN was only ever honoured by the `redeem` CLI command: the
#: Telegram bot always fell back to the config file. Someone could therefore set
#: BIDOO_DRY_RUN=1, believe they were protected, and have the bot execute
#: anyway. Rather than paper over that with a second code path, dry-run now has
#: exactly one source of truth -- redeem.dry_run in config.yaml -- and a stale
#: variable is reported instead of being silently ignored.
OBSOLETE_ENV_VARS: dict[str, str] = {
    "BIDOO_DRY_RUN": "redeem.dry_run in config.yaml",
}


def obsolete_env_vars_in_use() -> list[tuple[str, str]]:
    """Return ``(variable, replacement)`` for every stale variable that is set."""
    return [
        (name, replacement)
        for name, replacement in OBSOLETE_ENV_VARS.items()
        if os.environ.get(name, "").strip()
    ]
