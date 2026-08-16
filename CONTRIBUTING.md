# Contributing

Thanks for taking a look. This is a small personal project, so the bar is
simple: keep it small, keep it safe, keep it tested.

## Getting set up

```bash
git clone https://github.com/OWNER/bidoo-bot.git
cd bidoo-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

The whole suite runs offline with no credentials. If a change makes that
untrue, the change is wrong.

## Before opening a pull request

```bash
ruff format .
ruff check .
mypy
pytest
```

All four must be clean. CI runs exactly these on Python 3.12 and 3.13.

## The one architectural rule

**Imports point downwards only.**

```
adapters/  →  application/  →  parsing/, models/, security.py, config.py
```

The application core must not import an adapter, `telegram`, `googleapiclient`,
`httpx` or `playwright`. Concrete implementations are wired in exactly one
place: `src/bidoo_bot/container.py`.

`tests/test_architecture.py` enforces this automatically — if it fails, the fix
is to move the dependency behind a port in `application/ports.py`, not to
relax the test.

Practically:

- new behaviour that any interface should get → `application/redeem.py`
- new way to *reach* the behaviour (web UI, webhook) → a new adapter + a couple
  of lines in `container.py`
- new way to *execute* a redeem → a class implementing `RedeemerPort`

## Adding parser rules

Most tuning needs no code at all — `parser.signals` in `config.yaml` is data.
If you want to contribute a rule to the defaults:

1. Add it to `src/bidoo_bot/default_config.yaml`.
2. Regenerate/mirror it in `config.example.yaml` (the two must stay
   parse-identical; `tests/test_config.py` checks this).
3. Add a fixture and a test showing what it catches — **and** check it does not
   raise the score of the footer links in the existing fixtures.

Prefer several small weights over one big one: the noisy-OR is designed for
corroboration, and a single overconfident rule is how you end up clicking the
wrong link.

## Tests

- Use the fakes in `tests/fakes.py`; never reach the network.
- Fixtures must be **invented**. Do not commit a real Bidoo email, a real
  address, a real token or a real tracking URL — replace hosts with
  `example.invalid` unless the test is specifically about the allowlist.
- Cover the refusal path too. "It correctly did nothing" is the behaviour most
  worth protecting.

## Security-sensitive changes

Anything touching `security.py`, `logging_config.py`, the OAuth scope or the
Telegram allowlist needs an explicit note in the PR describing what could go
wrong. Changes that would make the bot act more eagerly (following more links,
guessing between candidates, widening scope) will be declined unless they come
with a matching safety mechanism.

Never propose CAPTCHA/MFA/anti-bot circumvention, credential storage, or
scheduled unattended runs — see [SECURITY.md](SECURITY.md).

## Commits and PRs

- One logical change per PR; a short description of *why* beats a long
  description of *what*.
- Conventional-ish commit subjects are appreciated (`fix:`, `feat:`, `docs:`)
  but not enforced.
- Update the README if you change behaviour a user can see.

## Reporting bugs

Open an issue with: what you ran, what you expected, what happened, and the
output of `python -m bidoo_bot check-config`. If it is a parsing problem, the
output of `analyze-email --all` on a **redacted or synthetic** email is the
single most useful thing you can attach.

For anything with a security impact, follow [SECURITY.md](SECURITY.md) instead
of opening a public issue.
