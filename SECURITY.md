# Security Policy

`bidoo-bot` has access to a Gmail account and performs an authenticated action
on an external website. Security reports are very welcome.

## Reporting a vulnerability

**Please do not open a public issue for a security problem.**

Use GitHub's private reporting instead:

1. Go to the **Security** tab of this repository.
2. **Report a vulnerability** ([GitHub Private Vulnerability Reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)).
3. Describe the issue, the impact, and how to reproduce it.

If private reporting is not enabled on the fork you are looking at, contact the
repository owner through their GitHub profile and ask for a private channel
before disclosing details.

**Never include real credentials, tokens, cookies or a real email in a report.**
Redact them, or reproduce the issue with one of the synthetic fixtures in
`tests/fixtures/`.

### What to expect

This is a small personal project maintained in spare time. Best effort:

- acknowledgement within about a week,
- an assessment and a plan within about two weeks,
- credit in the release notes when a fix ships, unless you prefer otherwise.

Please allow a reasonable window for a fix before public disclosure.

## Scope

In scope — anything that could:

- cause the bot to request a URL outside `security.allowed_domains`,
- bypass the Telegram allowlist,
- leak a token, cookie, OAuth credential or email content into logs, Telegram
  messages, or the repository,
- cause the same email to be redeemed more than once, or an unrelated link to
  be followed,
- escalate the requested OAuth scope beyond `gmail.modify`,
- allow a crafted email to trigger an unintended action (parser injection,
  URL parsing confusion, redirect smuggling).

Out of scope:

- vulnerabilities in Bidoo's own website,
- issues that require an attacker to already control your machine, your Google
  account, or your `config.yaml`,
- a misconfigured allowlist that you widened yourself,
- the absence of features the project intentionally does not implement (see
  below).

## Design decisions relevant to security

- **Least privilege.** Only `gmail.modify` is requested — the narrowest scope
  that permits applying a label, which is how idempotency is implemented.
- **Fail closed.** Low confidence, ambiguity, a non-allowlisted host, a
  non-HTTPS URL or a redirect leaving the allowlist all mean *do nothing*.
- **No arbitrary URL execution.** Every URL, including each redirect hop, is
  re-checked against the allowlist before the request is made.
- **Dry-run by default.**
- **Secrets never in the repository.** `.env`, OAuth client files, tokens and
  browser profiles are git-ignored; the token file is created `0600`.
- **Redacted logging.** Every log record is filtered for token shapes,
  `Authorization`/`Cookie` headers, `key=value` secrets and email addresses.
  URL query strings are stripped and Gmail message ids are hashed.
- **Minimal disclosure.** Unauthorised Telegram users receive `Access denied.`
  and nothing else. Email bodies are never forwarded to Telegram.

## What this project will never do

By design, and not up for negotiation in a pull request:

- bypass, solve or work around CAPTCHAs, MFA or anti-bot systems,
- store, type or transmit your Bidoo password,
- create accounts, or automate anything beyond following a link you were sent,
- run unattended against your mailbox on a schedule (it is manually triggered),
- widen the OAuth scope for convenience.

If the redeem flow requires an authenticated session, you sign in **manually**
into a local browser profile and the bot reuses that session — nothing more.

## Hardening your own installation

- Keep `dry_run: true` until `analyze-email` shows exactly what you expect.
- Keep `security.allowed_domains` as narrow as possible; add a tracking domain
  only after verifying it is really Bidoo's.
- Keep `require_https: true`.
- Restrict `TELEGRAM_ALLOWED_USER_IDS` to your own id.
- `chmod 600 .env` and keep `secrets/` off any backup that leaves your machine.
- Revoke access any time at
  [Google Account → Third-party access](https://myaccount.google.com/connections).
