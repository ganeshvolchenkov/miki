from __future__ import annotations

import logging
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class CalendarApiError(Exception):
    """Clean, normalized failure from the Calendar client -- never a raw
    googleapiclient exception. `tool.py` only ever needs to catch this one
    type, regardless of what actually went wrong underneath."""

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.message = message


class CalendarClientProtocol(Protocol):
    """What CalendarTool needs from a calendar backend. Satisfied by
    GoogleCalendarClient in production and by a fake in tests -- neither
    side imports the other."""

    def list_events(self, *, time_min: str, time_max: str, calendar_id: str = "primary", max_results: int = 50) -> list[dict[str, Any]]:
        ...

    def get_event(self, event_id: str, *, calendar_id: str = "primary") -> dict[str, Any] | None:
        ...

    def insert_event(self, event_body: dict[str, Any], *, calendar_id: str = "primary") -> dict[str, Any]:
        ...

    def update_event(self, event_id: str, event_body: dict[str, Any], *, calendar_id: str = "primary") -> dict[str, Any]:
        ...

    def delete_event(self, event_id: str, *, calendar_id: str = "primary") -> None:
        ...


class GoogleCalendarClient:
    """Thin wrapper around the real Google Calendar API. All googleapiclient
    specifics -- the service object, HttpError handling -- live here.
    Nothing above this layer imports Google's SDK directly (spec section 6)."""

    def __init__(self, credentials: Any) -> None:
        from googleapiclient.discovery import build

        self._service = build("calendar", "v3", credentials=credentials, cache_discovery=False)

    def list_events(self, *, time_min: str, time_max: str, calendar_id: str = "primary", max_results: int = 50) -> list[dict[str, Any]]:
        response = self._call(
            lambda: self._service.events()
            .list(calendarId=calendar_id, timeMin=time_min, timeMax=time_max, singleEvents=True, orderBy="startTime", maxResults=max_results)
            .execute()
        )
        return (response or {}).get("items", [])

    def get_event(self, event_id: str, *, calendar_id: str = "primary") -> dict[str, Any] | None:
        return self._call(
            lambda: self._service.events().get(calendarId=calendar_id, eventId=event_id).execute(),
            not_found_returns_none=True,
        )

    def insert_event(self, event_body: dict[str, Any], *, calendar_id: str = "primary") -> dict[str, Any]:
        return self._call(lambda: self._service.events().insert(calendarId=calendar_id, body=event_body).execute())

    def update_event(self, event_id: str, event_body: dict[str, Any], *, calendar_id: str = "primary") -> dict[str, Any]:
        return self._call(lambda: self._service.events().patch(calendarId=calendar_id, eventId=event_id, body=event_body).execute())

    def delete_event(self, event_id: str, *, calendar_id: str = "primary") -> None:
        self._call(lambda: self._service.events().delete(calendarId=calendar_id, eventId=event_id).execute())

    def _call(self, fn, *, not_found_returns_none: bool = False) -> Any:
        from googleapiclient.errors import HttpError

        try:
            return fn()
        except HttpError as exc:
            status = getattr(exc.resp, "status", None)
            if status == 404 and not_found_returns_none:
                return None
            if status == 401:
                raise CalendarApiError("authentication_required", "Google Calendar authentication has expired.") from exc
            if status == 403:
                raise CalendarApiError("permission_denied", "Permission was denied by Google Calendar.") from exc
            if status == 429:
                raise CalendarApiError("rate_limited", "Google Calendar rate limit reached; try again shortly.") from exc
            logger.warning("Google Calendar API error (status=%s)", status)
            raise CalendarApiError("api_error", f"Google Calendar API error (status {status}).") from exc
        except CalendarApiError:
            raise
        except Exception as exc:
            logger.warning("Could not reach Google Calendar: %s", exc)
            raise CalendarApiError("network_error", "Could not reach Google Calendar.") from exc
