# bidoo-bot

A small, manually triggered tool that looks for Bidoo free-bid emails in your
Gmail and redeems them — from Telegram, or from the command line.

It is **not** a daemon. Nothing watches your mailbox and nothing runs on a
schedule: the bot acts only when you send `/bidoo` (or run `bidoo-bot redeem`).

```
🎁 Bidoo check completed

📧 Emails found: 5
✅ Redeemed: 3
⏭️ Already processed: 1
⚠️ Unrecognized: 1

Details:
✅ Redeemed — La tua puntata gratis ti aspetta
✅ Redeemed — Un regalo per te
✅ Redeemed — Puntata omaggio
⏭️ Already processed — Bonus del weekend
⚠️ Unrecognized — Le aste della settimana
```

> **Status: alpha.** The parser and the whole pipeline are tested and working,
> but the project ships **no knowledge of real Bidoo emails**. The redeem
> patterns are educated guesses. Read
> [Configuring for your actual emails](#configuring-for-your-actual-emails)
> before turning off dry-run — that section is the point of the project.

---

## Contents

- [What it does](#what-it-does)
- [Architecture](#architecture)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Creating the Telegram bot](#creating-the-telegram-bot)
- [Google Cloud / Gmail OAuth](#google-cloud--gmail-oauth)
- [Configuring for your actual emails](#configuring-for-your-actual-emails)
- [Running it](#running-it)
- [Dry run](#dry-run)
- [Bidoo redeem strategies](#bidoo-redeem-strategies)
- [Idempotency](#idempotency)
- [Security](#security)
- [Deployment](#deployment)
- [Development](#development)
- [Troubleshooting](#troubleshooting)
- [Contributing](#contributing)
- [Known limitations](#known-limitations)
- [License](#license)

---

## What it does

1. You send `/bidoo` to your Telegram bot (or run `bidoo-bot redeem`).
2. It queries Gmail with **your** configured search query — never the whole
   mailbox.
3. For each matching email it parses the HTML and ranks every link.
4. It picks the most likely "redeem your free bid" link, with a confidence
   score and an explanation.
5. It checks that link against a **domain allowlist**. Anything else is
   refused.
6. It executes the action (or not, in dry-run) and labels the message so it is
   never processed twice.
7. It replies with a summary.

If the bot cannot work out which link is the right one, it does nothing and
tells you. **Not redeeming a free bid is much cheaper than following the wrong
link.**

## Architecture

The rule: **imports only ever point downwards.** The application core knows
nothing about Gmail, Telegram or HTTP — only about two small protocols
(`MailboxPort`, `RedeemerPort`). Swapping Telegram for a web UI, or the HTTP
redeemer for a browser, touches one file and no business logic.

```
                 ┌──────────────┐   ┌──────────┐   ┌─────────────────┐
  interfaces     │ Telegram bot │   │   CLI    │   │ serverless fn   │
                 └──────┬───────┘   └────┬─────┘   └────────┬────────┘
                        └────────────────┼──────────────────┘
                                         ▼
                            container.py  (composition root)
                                         ▼
                 ┌───────────────────────────────────────────────┐
  core           │  application/redeem.py   RedeemService.run()   │
                 │  application/ports.py    MailboxPort           │
                 │                          RedeemerPort          │
                 └───────────────┬───────────────┬───────────────┘
                                 │               │
                 ┌───────────────▼───┐   ┌───────▼─────────────────┐
  adapters       │ adapters/gmail    │   │ adapters/bidoo          │
                 │  Gmail API, OAuth │   │  http_redeemer          │
                 └───────────────────┘   │  playwright_redeemer    │
                                         └─────────────────────────┘
                 ┌───────────────────────────────────────────────┐
  pure           │ parsing/  models/  security.py  config.py     │
                 │ no I/O, no SDK, no network                    │
                 └───────────────────────────────────────────────┘
```

```
src/bidoo_bot/
├── __main__.py            python -m bidoo_bot
├── cli.py                 argparse front-end
├── container.py           composition root: wires adapters into the core
├── config.py              YAML config + env secrets, strictly validated
├── default_config.yaml    packaged defaults (config.example.yaml is its copy)
├── logging_config.py      logging + secret redaction
├── security.py            UrlPolicy: the domain allowlist
├── reporting.py           rendering shared by CLI and Telegram
├── errors.py
├── models/                EmailMessage, ActionCandidate, results
├── parsing/               HTML → ranked candidates (pure functions)
│   ├── html.py            link extraction
│   ├── action_parser.py   scoring and selection
│   └── eml.py             load a saved .eml/.html
├── application/
│   ├── ports.py           MailboxPort, RedeemerPort
│   └── redeem.py          RedeemService — the only use case
└── adapters/
    ├── gmail/             auth.py, client.py
    ├── bidoo/             http_redeemer.py, playwright_redeemer.py, factory.py
    └── telegram/          authorization.py, bot.py
```

`tests/test_architecture.py` enforces the layering rule: the core is not
allowed to import an adapter or a vendor SDK, and the build fails if it does.

## Requirements

- Python 3.12 or newer
- A Google account with Gmail
- A Telegram account (only if you want the bot interface)
- Optionally Playwright, if the redeem action turns out to need a real browser

## Installation

```bash
git clone https://github.com/OWNER/bidoo-bot.git
cd bidoo-bot
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

Check it works — this needs no credentials and touches no network:

```bash
python -m bidoo_bot --help
python -m bidoo_bot analyze-email tests/fixtures/free_bid_it.html
```

## Configuration

Two separate things, on purpose:

| | file | contains | committed? |
|---|---|---|---|
| Settings | `config.yaml` | query, labels, domains, patterns, timeouts | ❌ (git-ignored) |
| Secrets | `.env` | Telegram token, allowed user ids | ❌ (git-ignored) |

```bash
cp config.example.yaml config.yaml
cp .env.example .env
```

`.env` is read from the **current working directory** only — deliberately not
from parent folders, so an unrelated project's `.env` can never hand this tool
a token. Point somewhere else with `BIDOO_CONFIG` for the config file.

`config.example.yaml` is a full copy of the built-in defaults, so **delete
every key you are not actually changing**. Whatever you leave out keeps its
default value *and keeps following future improvements to it*. A typical
`config.yaml` is a handful of lines:

```yaml
gmail:
  query: "label:Bidoo newer_than:15d"
security:
  allowed_domains: ["bidoo.com", "bidoo.it"]
```

This matters most for `parser.signals`: **lists are replaced, not merged**, so
a wholesale copy freezes the scoring rules and new default rules will silently
never reach you. `check-config` tells you when that has happened.

Validate at any time:

```bash
python -m bidoo_bot check-config
```

The most important keys:

```yaml
gmail:
  query: "label:Bidoo newer_than:30d"     # your own filter, never the whole mailbox
  processed_label: "Bidoo/Processed"      # the idempotency store
security:
  allowed_domains: ["bidoo.com", "bidoo.it"]   # nothing else is ever requested
parser:
  min_confidence: 0.65                    # below this: refuse
  ambiguity_margin: 0.10                  # two close candidates: refuse
redeem:
  strategy: "http"                        # or "playwright"
  dry_run: true                           # safe default
```

## Creating the Telegram bot

1. Open Telegram and talk to [@BotFather](https://t.me/BotFather).
2. `/newbot`, pick a name and a username. BotFather replies with a token.
3. Put the token in `.env` as `TELEGRAM_BOT_TOKEN`.
4. Open Telegram and send any message to your new bot — `/start` is fine. It
   will not answer yet; that is expected.
5. Ask which ids have written to it:

   ```bash
   python -m bidoo_bot telegram-whoami
   ```

   ```
   Telegram accounts that recently messaged your bot:

     123456789 — Your Name

   Put *your own* id in .env (leave out anyone you do not recognise):

     TELEGRAM_ALLOWED_USER_IDS=123456789
   ```

   This reads your bot's pending updates with your own token: no third-party
   "what is my id" bot is involved, the token never leaves your machine, and
   the messages stay queued.

6. Paste that line into `.env`, then start the bot:

   ```bash
   python -m bidoo_bot bot
   ```

Only ids on that list can use the bot; everyone else gets `Access denied.` and
nothing more. **The bot refuses to start with an empty allowlist** — that is
why you fill it in before the first start, not after.

## Google Cloud / Gmail OAuth

You need your own OAuth client — this project ships none, and you should never
put one in a repository.

1. Go to the [Google Cloud Console](https://console.cloud.google.com/) and
   create a project (any name).
2. **APIs & Services → Library → Gmail API → Enable.**
3. **APIs & Services → OAuth consent screen:**
   - User type: **External** is fine for a personal project.
   - Fill in the app name and your email.
   - Under **Audience**, add your own Google account as a **Test user**.
     While the app is in "Testing" you do not need Google's verification, but
     the refresh token expires after 7 days — see
     [Troubleshooting](#troubleshooting).
4. **APIs & Services → Credentials → Create credentials → OAuth client ID:**
   - Application type: **Desktop app** (required — the login flow runs a
     local server).
   - Download the JSON.
5. Save it where `gmail.credentials_file` points, by default:

   ```
   secrets/google_client_secret.json
   ```

   `secrets/` is git-ignored.
6. Authorise once:

   ```bash
   python -m bidoo_bot gmail-auth
   ```

   A browser opens, you consent, and the refresh token is cached at
   `gmail.token_file` (default `secrets/gmail_token.json`) with `0600`
   permissions.

**Scope:** the bot requests `gmail.modify` and nothing else. That is the
narrowest scope that still allows applying a label to a message, which is how
idempotency works. It cannot permanently delete anything, cannot send mail, and
has no access to Drive, Calendar or contacts.

Then create the Gmail filter/label that `gmail.query` refers to — e.g. a filter
that applies the label `Bidoo` to mail from the Bidoo sender.

## Configuring for your actual emails

**This is the step that makes the project actually work for you.** The default
patterns were written without access to a real Bidoo email; they are a starting
point, not the truth.

1. Open a real Bidoo email in Gmail, **Show original → Download original**.
   Save it as `mail.eml` (outside the repo — it is your personal mail).
2. Ask the parser what it sees:

   ```bash
   python -m bidoo_bot analyze-email ~/mail.eml --all
   ```

   ```
   Status:  OK
   Best candidate:
     text:       "RISCUOTI LA TUA PUNTATA GRATIS"
     url:        https://www.bidoo.com/promo/riscuoti?token=...
     confidence: 0.99
     reason:     matched redeem-verb-it(+0.70), free-bid-it(+0.75), ...
     policy:     ✅ allowed — host is a subdomain of allowed domain 'bidoo.com'
   ```

3. Fix whatever is wrong, in `config.yaml`:

   | Symptom | Fix |
   |---|---|
   | `⛔ REJECTED` on the right link | add the real host to `security.allowed_domains` (newsletters often use a tracking domain) |
   | `LOW_CONFIDENCE` | add a signal matching the real button wording, or lower `parser.min_confidence` |
   | `AMBIGUOUS` | add a signal that distinguishes them, or lower `parser.ambiguity_margin` |
   | the wrong link wins | add a `kind: negative` signal for it |
   | a new default rule seems to do nothing | your `config.yaml` pins `parser.signals`; `check-config` says so — delete the block |

4. Re-run `analyze-email` until it says what you expect, then do a real
   dry run against Gmail:

   ```bash
   python -m bidoo_bot redeem --dry-run -v
   ```

5. Only then turn dry-run off.

### How scoring works

Each rule that matches contributes its weight. Positives combine with a
noisy-OR — `confidence = 1 - Π(1 - wᵢ)` — so several weak hints reinforce each
other but a single one is never enough. Negatives then scale the result down by
`Π(1 - wⱼ)`. Every candidate carries the list of rules that fired, which is
what `analyze-email` prints.

`field` selects what a rule looks at: `text`, `url`, `attrs`, `context`, or
`any` (= text + url + attrs, i.e. the link itself — deliberately *not* the
surrounding text, otherwise an "unsubscribe" in the footer would penalise the
real button too).

## Running it

```bash
python -m bidoo_bot redeem                  # dry-run by default
python -m bidoo_bot redeem --no-dry-run     # actually execute
python -m bidoo_bot redeem --json           # machine readable
python -m bidoo_bot analyze-email mail.eml  # offline parser inspection
python -m bidoo_bot status                  # config + Gmail connectivity
python -m bidoo_bot check-config            # validate config.yaml
python -m bidoo_bot gmail-auth              # one-off OAuth
python -m bidoo_bot bot                     # start the Telegram bot
python -m bidoo_bot telegram-whoami         # find your Telegram id (first-time setup)
```

`-v` for info, `-vv` for debug, `-q` for quiet. These, along with `--config`
and `--log-format`, are accepted on either side of the subcommand — both
`bidoo-bot -v redeem` and `bidoo-bot redeem -v` work. After `pip install` the
same commands are available as `bidoo-bot ...`.

The Telegram bot answers `/start`, `/help`, `/bidoo` and `/status`. `/bidoo`
acknowledges immediately ("🔎 Checking your mailbox…"), does the work in a
worker thread, and replies with the summary. Two concurrent `/bidoo` are
serialised — the second one is told to wait.

**The CLI and the bot call the exact same `RedeemService`.** There is no second
implementation to keep in sync.

## Dry run

`dry_run: true` is the default and it is a real safety feature, not a debug
flag. In dry-run the bot searches, parses, scores and validates against the
allowlist, then stops:

```
🎁 Bidoo check completed
🧪 DRY RUN — no action was executed

📧 Emails found: 1
🧪 Dry run: 1

Details:
🧪 Dry run — La tua puntata gratis ti aspetta
   text: "RISCUOTI LA TUA PUNTATA GRATIS"
   url: https://www.bidoo.com/promo/riscuoti?token=...
   confidence: 0.99
```

Nothing is requested, nothing is labelled, and the browser is never even
started.

`redeem.dry_run` in `config.yaml` is the **single source of truth**: the
Telegram bot, the CLI and any future interface all read that one value. To go
live permanently:

```yaml
redeem:
  dry_run: false
```

The CLI can override it for one run with `--no-dry-run` (or force it back on
with `--dry-run`); the Telegram bot deliberately has no such override, so
`/status` always tells you the truth about what `/bidoo` will do.

There is no environment variable for this on purpose. An earlier
`BIDOO_DRY_RUN` was honoured only by the CLI, which meant you could set it to
`1`, believe you were protected, and have the bot execute anyway. If it is
still set anywhere, bidoo-bot warns that it is being ignored.

## Bidoo redeem strategies

**Start with `http`.** It performs one GET on the validated URL, following
redirects one hop at a time and re-checking the allowlist at *every* hop.
Success is decided by the status code plus the optional
`success_patterns`/`failure_patterns` regexes.

Switch to `playwright` only if the redeem genuinely needs a logged-in browser
session:

```bash
pip install -e ".[playwright]"
playwright install chromium
python -m bidoo_bot browser-login          # opens a window; you sign in by hand
```

Then set `redeem.strategy: playwright`. The session lives in a persistent
profile directory (`.local/playwright-profile`, git-ignored). The bot **never
types credentials, never handles passwords, and never touches CAPTCHAs, MFA or
bot-detection.** You authenticate manually; the bot only reuses the session. If
the site shows a login page, the run fails with a clear message — that is the
intended behaviour.

Set `redeem.playwright.headless: false` while debugging to watch what happens.

## Idempotency

The simplest thing that is also robust: **Gmail labels are the store.** No
database, no Firestore.

- After a successful redeem the message gets `Bidoo/Processed`.
- `-label:"Bidoo/Processed"` is appended to your query automatically, so
  processed mail is not even fetched.
- Independently, the service skips any message that already carries the label —
  so a mistyped query cannot cause a double redeem.
- Failures get `Bidoo/Failed` (configurable, or `null` to disable) and *are*
  retried on the next run.

If labelling fails after a successful redeem, the run reports it loudly as a
warning rather than silently risking a repeat.

## Security

This project has access to your mailbox and performs an authenticated action on
an external site. It is written accordingly.

- **Least-privilege OAuth** — `gmail.modify` only.
- **Telegram allowlist** — a fixed set of user ids; empty allowlist = the bot
  refuses to start; denied users get four words and no information.
- **Domain allowlist** — no URL outside `security.allowed_domains` is ever
  requested, including redirect targets. Look-alikes (`bidoo.com.evil.tld`) and
  userinfo spoofing (`https://bidoo.com@evil.tld/`) are refused.
- **HTTPS required** by default.
- **No arbitrary URL execution** — a link is followed only if the parser is
  confident *and* the policy allows it. Ambiguity means refusal.
- **Dry-run by default.**
- **Secrets never in the repo** — `.gitignore` is aggressive about `.env`,
  `*token*.json`, `client_secret*.json`, browser profiles and cookies. The
  OAuth token is written with `0600`.
- **No secrets in logs** — every log record passes a redaction filter that
  scrubs Telegram/Google tokens, `Authorization` and `Cookie` headers,
  `key=value` secrets and email addresses. URLs are logged without their query
  string (that is where redeem codes live), and Gmail message ids are logged as
  a short hash.
- **Email bodies are never sent to Telegram** — only subject, status and (in
  dry-run) the candidate URL.
- **Timeouts** on every outbound request; **no CAPTCHA/MFA/anti-bot bypass** of
  any kind.

Please read [SECURITY.md](SECURITY.md) before reporting a vulnerability.

## Deployment

Running `python -m bidoo_bot bot` on any always-on machine (a laptop, a
Raspberry Pi, a small VM) is enough for personal use, and is what this project
is designed for today.

**No cloud configuration ships with this repository, on purpose.** What *is*
prepared is the code: the core is transport agnostic, so a webhook-driven
function can call it directly. See
[`examples/serverless_entrypoint.py`](examples/serverless_entrypoint.py):

```
Telegram webhook  ->  HTTP function  ->  build_service(config).run()
```

If you do go serverless, note:

- **Runtime.** Cloud Run functions currently support Python 3.12, 3.13 and 3.14
  as GA runtimes (3.12 is supported until 2028-10-02), so this project's
  `requires-python = ">=3.12"` is satisfiable. Check
  [the current support matrix](https://docs.cloud.google.com/functions/docs/runtime-support)
  before you commit to a version — these dates move.
- **Secrets.** The Gmail refresh token and the Telegram token belong in a
  secret manager mounted at runtime, never in the deployment artifact.
- **Statelessness.** Idempotency lives in Gmail labels, so concurrent or
  retried invocations stay safe.
- **Webhook authenticity.** Verify Telegram's `secret_token` header before
  acting on a payload.

## Development

```bash
pip install -e ".[dev]"
ruff format .           # format
ruff check .            # lint
mypy                    # type check (strict on src/)
pytest                  # 281 tests, no network, no credentials
```

Every test runs against the packaged defaults and uses in-memory fakes for
Gmail, Bidoo and Telegram (`tests/fakes.py`). Nothing in the suite needs a
credential or a network connection. HTML fixtures in `tests/fixtures/` are
hand-written inventions — **no real Bidoo email is in this repository.**

## Troubleshooting

**`Gmail is not authorised yet`** — run `python -m bidoo_bot gmail-auth`.

**Login stops working after 7 days** — while the OAuth consent screen is in
"Testing", Google expires refresh tokens after a week. Either re-run
`gmail-auth`, or publish the app (Audience → Publish; for a personal app with
only your own account this is fine and needs no verification for these scopes).

**`Gmail refused the request (HTTP 403)`** — the Gmail API is not enabled on
the project, or the token predates a scope change. Enable it, delete
`secrets/gmail_token.json`, re-run `gmail-auth`.

**`Emails found: 0`** — your `gmail.query` matches nothing. Paste it into the
Gmail search box to check. Remember the bot appends
`-label:"Bidoo/Processed"`, so already-redeemed mail is excluded by design;
`python -m bidoo_bot check-config` prints the effective query.

**`⚠️ Unrecognized`** — run `analyze-email` on that mail and follow
[Configuring for your actual emails](#configuring-for-your-actual-emails). Some
senders never write "gratis" and express the offer as a quantity instead
("Sblocca 3 Puntate 🎁"); the `free-bid-count` and `gift-emoji` default rules
cover that shape.

**A new default scoring rule has no effect** — your `config.yaml` is pinning
the whole `parser.signals` list. Run `check-config`: it reports `pinned by your
config.yaml` and how many rules the defaults now ship.

**`⛔ Rejected`** — the winning link is not on your allowlist. This is the
system working. Check the host with `analyze-email` and add it *only* if you
recognise it as Bidoo's own tracking domain.

**`❓ Ambiguous`** — two links scored within `ambiguity_margin` of each other.
Refusing is intentional. Distinguish them with a signal, or lower the margin.

**Playwright: "the profile does not exist yet"** — run
`python -m bidoo_bot browser-login` first.

**`TELEGRAM_ALLOWED_USER_IDS is empty. Refusing to start...`** — the bot will
not start until you allowlist yourself. Send any message to the bot in
Telegram, then run `python -m bidoo_bot telegram-whoami` to get your id.

**`telegram-whoami` says "No pending messages"** — you have not written to the
bot yet, or something already collected the updates. Send it a message and run
the command again. If it reports that Telegram is "already delivering updates
elsewhere", stop the running `bidoo-bot bot` first.

**Telegram: nothing happens** — check the logs for
`Denied '/bidoo' from Telegram user id ...` and add that id to
`TELEGRAM_ALLOWED_USER_IDS`.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). In short: keep the layering (no adapter
imports in the core), add a test, run `ruff` and `mypy`, and never commit a
real email or a credential.

## Known limitations

- **The default redeem patterns are guesses.** No real Bidoo email was
  available while writing this. Expect to tune `parser.signals` and
  `security.allowed_domains` — `analyze-email` exists exactly for that.
- **Success detection is shallow by default.** The HTTP strategy treats any 2xx
  as success unless you configure `success_patterns`. Once you know what a
  redeemed page looks like, set them.
- **Single Gmail account**, single user.
- **Long polling only** for Telegram; no webhook server is included.
- **The Playwright strategy is written but unverified against the real site**,
  since that needs an authenticated session.
- The bot deliberately refuses more often than it acts. That is the design.

## License

[MIT](LICENSE). This is an independent personal project and is not affiliated
with, endorsed by, or connected to Bidoo in any way. Use it on your own account
and in accordance with the site's terms of service.
