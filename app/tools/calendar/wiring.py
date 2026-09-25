from __future__ import annotations

from pathlib import Path

from app.tools.calendar.auth import CalendarAuthRequired, CalendarUnavailable, load_credentials
from app.tools.calendar.client import CalendarClientProtocol, GoogleCalendarClient
from app.tools.calendar.tool import CalendarTool


def build_calendar_client_factory(*, credentials_path: Path, token_path: Path):
    """Returns a zero-arg callable that resolves a connected calendar client
    on demand -- resolved lazily so Miki can start before authentication and
    so a freshly connected/refreshed token is picked up without a restart."""

    def factory() -> CalendarClientProtocol:
        try:
            import googleapiclient  # noqa: F401
        except ImportError as exc:
            raise CalendarUnavailable(
                "Google Calendar packages are not installed. Run: "
                "pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib"
            ) from exc

        creds = load_credentials(credentials_path, token_path)
        if creds is None:
            raise CalendarAuthRequired("Google Calendar authentication is required.")
        return GoogleCalendarClient(creds)

    return factory


def build_calendar_tool(
    *,
    credentials_path: Path,
    token_path: Path,
    require_create_confirmation: bool = True,
    default_calendar_id: str = "primary",
) -> CalendarTool:
    factory = build_calendar_client_factory(credentials_path=credentials_path, token_path=token_path)
    return CalendarTool(
        client_factory=factory,
        require_create_confirmation=require_create_confirmation,
        default_calendar_id=default_calendar_id,
    )
