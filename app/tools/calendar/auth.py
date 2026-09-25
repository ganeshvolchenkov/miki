from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# https://www.googleapis.com/auth/calendar grants read/write access to the
# user's calendars -- required since Miki both reads and creates/edits events.
# https://www.googleapis.com/auth/gmail.modify grants read/write access to Gmail.
SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/drive.readonly"
]


class CalendarAuthRequired(Exception):
    """The user has not completed Google authentication yet (no valid token)."""


class CalendarUnavailable(Exception):
    """The Calendar tool cannot be used for a reason other than missing
    auth -- e.g. the Google client packages are not installed."""


def load_credentials(credentials_path: Path, token_path: Path) -> Any | None:
    """Returns valid, non-expired Google credentials, or None if the user
    has not completed the OAuth flow yet. Refreshes an expired token
    automatically when a refresh token is available. Never launches the
    interactive browser flow itself -- see `run_oauth_flow`."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError:
        return None

    creds = None
    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        except Exception:
            logger.warning("Stored Google Calendar token could not be read; re-authentication will be required.")
            creds = None

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            token_path.write_text(creds.to_json(), encoding="utf-8")
        except Exception:
            logger.warning("Failed to refresh the Google Calendar token; re-authentication will be required.")
            return None

    if creds and creds.valid:
        return creds
    return None


def run_oauth_flow(credentials_path: Path, token_path: Path) -> Any:
    """Runs the interactive local-server OAuth flow (opens a browser tab)
    and stores the resulting token. Only called explicitly by a user-facing
    "connect" action (e.g. the /calendar connect CLI command) -- never
    triggered automatically mid-conversation."""
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as err:
        raise CalendarUnavailable(
            "Google Calendar packages are not installed. Run: "
            "pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib"
        ) from err

    if not credentials_path.exists():
        raise CalendarUnavailable(
            f"credentials.json was not found at {credentials_path}. See the README's Google Calendar setup section."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
    creds = flow.run_local_server(port=0)

    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds


