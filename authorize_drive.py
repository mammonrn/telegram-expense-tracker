"""One-time interactive setup: authorize this bot to upload to your personal
Google Drive.

Run this manually on the server (e.g. over SSH) once, before starting the
bot for the first time, and again any time you see a DriveAuthError asking
you to re-authorize:

    python authorize_drive.py

It prints a URL. Open that URL in a browser on ANY device (your phone or
laptop works fine - it does not need to be the server itself), sign in
with the Google account you want the bot to upload slips to, and approve
access. The browser will then try to redirect to a localhost address and
fail to load ("This site can't be reached" or similar) - that's expected,
since nothing is running on your personal device. Copy the full URL from
the address bar (or just the `code=...` value in it) and paste it back
into this terminal when prompted.

The resulting token is saved to GOOGLE_OAUTH_TOKEN_PATH (default
token.json) and is refreshed automatically by the bot afterwards - you
only need to repeat this if the refresh token itself is revoked.
"""

from __future__ import annotations

from auth import DriveAuthError, build_authorization_url, exchange_code, save_credentials
from config import load_config


def main() -> None:
    config = load_config()
    flow, auth_url = build_authorization_url(config.google_oauth_client_secret_path)

    print("=" * 70)
    print("Google Drive authorization for the Expense Tracker Bot")
    print("=" * 70)
    print("\n1. Open this URL in any browser (it doesn't need to be on this server):\n")
    print(auth_url)
    print(
        "\n2. Sign in with the Google account you want this bot to upload slips to,"
        "\n   and click Allow.\n"
        "3. The browser will try to redirect to a localhost address and fail to"
        "\n   load - that's expected. Copy the full URL from the address bar"
        "\n   (or just the 'code=...' value in it) and paste it below.\n"
    )

    pasted = input("Paste the redirect URL or code here: ")

    try:
        creds = exchange_code(flow, pasted)
    except DriveAuthError as exc:
        print(f"\n❌ {exc}")
        raise SystemExit(1)
    except Exception as exc:  # noqa: BLE001
        print(f"\n❌ Authorization failed: {exc}")
        raise SystemExit(1)

    save_credentials(creds, config.google_oauth_token_path)
    print(f"\n✅ Saved Drive access token to '{config.google_oauth_token_path}'.")
    print("You can now start the bot normally: python main.py")


if __name__ == "__main__":
    main()
