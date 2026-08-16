"""Gmail OAuth 2.0 handling.

Least privilege: the bot asks for ``gmail.modify`` and nothing else. That is
the narrowest scope that still allows *applying a label to a message*, which is
how idempotency is implemented. It grants no access to Drive, Calendar or
contacts, and it cannot permanently delete anything.

The refresh token is cached in a local file (``gmail.token_file``) created with
owner-only permissions and excluded from git.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

from bidoo_bot.errors import AuthError
from bidoo_bot.logging_config import get_logger

logger = get_logger(__name__)

#: Read messages + add/remove labels. No delete, no send, nothing else.
SCOPES: tuple[str, ...] = ("https://www.googleapis.com/auth/gmail.modify",)

_SETUP_HINT = (
    "Gmail is not authorised yet. Run `bidoo-bot gmail-auth` once on a machine "
    "with a browser. See the 'Google Cloud / Gmail OAuth' section of the README "
    "for how to create the OAuth client."
)


def _write_private(path: Path, payload: str) -> None:
    """Write a secret to disk with 0600 permissions, creating parents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Create with restrictive permissions from the start, not after the fact.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(payload)


def save_credentials(credentials: Any, token_file: Path) -> None:
    """Persist the OAuth credentials (refresh token included) safely."""
    _write_private(token_file, credentials.to_json())
    logger.debug("OAuth token cached at %s", token_file)


def load_credentials(
    *,
    credentials_file: Path,
    token_file: Path,
    allow_interactive: bool = False,
) -> Any:
    """Return usable Google credentials.

    Order: cached token -> silent refresh -> interactive consent (only when
    ``allow_interactive`` is set, i.e. from the ``gmail-auth`` command).
    """
    from google.auth.exceptions import GoogleAuthError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    credentials: Any = None
    if token_file.is_file():
        try:
            credentials = Credentials.from_authorized_user_file(str(token_file), list(SCOPES))
        except (ValueError, json.JSONDecodeError) as exc:
            raise AuthError(
                f"the cached OAuth token at {token_file} is unreadable ({exc}). "
                "Delete it and run `bidoo-bot gmail-auth` again."
            ) from exc

    if credentials and credentials.valid:
        return credentials

    if credentials and credentials.expired and credentials.refresh_token:
        logger.info("Refreshing the Gmail access token")
        try:
            credentials.refresh(Request())
        except GoogleAuthError as exc:
            if not allow_interactive:
                raise AuthError(
                    f"could not refresh the Gmail token ({exc}). "
                    "Run `bidoo-bot gmail-auth` to authorise again."
                ) from exc
            logger.warning("Token refresh failed, falling back to interactive consent")
            credentials = None
        else:
            save_credentials(credentials, token_file)
            return credentials

    if not allow_interactive:
        raise AuthError(_SETUP_HINT)

    return _run_interactive_flow(credentials_file=credentials_file, token_file=token_file)


def _run_interactive_flow(*, credentials_file: Path, token_file: Path) -> Any:
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not credentials_file.is_file():
        raise AuthError(
            f"OAuth client file not found at {credentials_file}.\n"
            "Create an OAuth client of type 'Desktop app' in Google Cloud Console, "
            "download the JSON and save it there (the path is gmail.credentials_file "
            "in config.yaml). Never commit that file."
        )

    logger.info("Opening a browser for Google consent; sign in with your own account")
    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file), list(SCOPES))
    credentials = flow.run_local_server(port=0, prompt="consent")
    save_credentials(credentials, token_file)
    logger.info("Gmail authorised; token cached at %s", token_file)
    return credentials
