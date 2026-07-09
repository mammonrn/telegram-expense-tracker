"""OAuth2 authentication for Google Drive uploads.

Drive uploads use a personal Google account via OAuth2 rather than the
service account used for Sheets. A service account has no storage quota
of its own outside a Shared Drive, and Shared Drives require a paid
Google Workspace plan - which a personal Gmail account doesn't have. So
Drive files must be owned by (and count against the quota of) a real
Google account, which means OAuth2 user credentials.

This module only *loads and refreshes* an already-issued token; the
interactive, one-time authorization step lives in `authorize_drive.py`
(run manually once on the server) since it requires a human to visit a
consent URL and paste back a code.
"""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

logger = logging.getLogger("expense_bot.auth")

DRIVE_OAUTH_SCOPES = ["https://www.googleapis.com/auth/drive"]

# "Desktop app" OAuth clients require a loopback redirect URI, even though
# nothing needs to actually be listening on it for the manual copy/paste
# flow used here - see authorize_drive.py.
REDIRECT_URI = "http://localhost"


class DriveAuthError(RuntimeError):
    """Raised when Drive OAuth credentials are missing, invalid, or expired
    beyond automatic refresh - always with instructions on how to fix it."""


def load_drive_credentials(token_path: str) -> Credentials:
    """Load cached OAuth2 credentials for Drive, refreshing if needed.

    Does not run the interactive consent flow - run `authorize_drive.py`
    once first. Raises DriveAuthError with setup instructions otherwise.
    """
    token_file = Path(token_path)
    if not token_file.exists():
        raise DriveAuthError(
            f"No Drive OAuth token found at '{token_path}'. Run "
            f"'python authorize_drive.py' once to authorize this bot, then restart it."
        )

    creds = Credentials.from_authorized_user_file(str(token_file), DRIVE_OAUTH_SCOPES)

    if creds.valid:
        return creds

    if creds.expired and creds.refresh_token:
        logger.info("Drive OAuth token expired, refreshing")
        creds.refresh(Request())
        save_credentials(creds, token_file)
        return creds

    raise DriveAuthError(
        f"Drive OAuth token at '{token_path}' is invalid and has no refresh token. "
        f"Run 'python authorize_drive.py' again to re-authorize."
    )


def save_credentials(creds: Credentials, token_path: str | Path) -> None:
    Path(token_path).write_text(creds.to_json(), encoding="utf-8")
    logger.info("Saved Drive OAuth token to %s", token_path)


def build_authorization_url(client_secret_path: str) -> tuple[InstalledAppFlow, str]:
    """Start the manual OAuth flow.

    Returns the in-progress `flow` object (needed to complete the exchange)
    and a URL for the user to open in any browser, on any device.
    """
    flow = InstalledAppFlow.from_client_secrets_file(client_secret_path, DRIVE_OAUTH_SCOPES)
    flow.redirect_uri = REDIRECT_URI
    auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent")
    return flow, auth_url


def exchange_code(flow: InstalledAppFlow, pasted: str) -> Credentials:
    """Finish the manual flow: exchange a pasted code (or full redirect URL)
    for credentials. Accepts either just the `code=...` value or the whole
    (unreachable) redirect URL the browser lands on, for convenience."""
    code = _extract_code(pasted)
    flow.fetch_token(code=code)
    return flow.credentials


def _extract_code(pasted: str) -> str:
    value = pasted.strip()
    if value.startswith("http://") or value.startswith("https://"):
        query = parse_qs(urlparse(value).query)
        codes = query.get("code")
        if not codes:
            raise DriveAuthError("Couldn't find a 'code' parameter in that URL.")
        return codes[0]
    if not value:
        raise DriveAuthError("No code or URL was entered.")
    return value
