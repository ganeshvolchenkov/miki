from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable

from app.tools.base import ACCESS_DESTRUCTIVE, ACCESS_READ, ACCESS_WRITE, Tool, ToolError, ToolOperation, ToolResult
from app.tools.calendar.auth import CalendarAuthRequired, CalendarUnavailable
from app.tools.calendar.client import CalendarApiError, CalendarClientProtocol
from app.tools.calendar.models import CalendarEvent

logger = logging.getLogger(__name__)

_ISO_DATETIME_DESCRIPTION = "ISO 8601 datetime with a UTC offset, e.g. 2026-08-18T18:00:00+02:00."

_GET_EVENTS_PARAMETERS = {
    "type": "object",
    "properties": {
        "start": {"type": "string", "description": f"Start of the range (inclusive). {_ISO_DATETIME_DESCRIPTION}"},
        "end": {"type": "string", "description": f"End of the range (exclusive). {_ISO_DATETIME_DESCRIPTION}"},
    },
    "required": ["start", "end"],
}

_FIND_FREE_TIME_PARAMETERS = {
    "type": "object",
    "properties": {
        "start": {"type": "string", "description": f"Start of the window to search. {_ISO_DATETIME_DESCRIPTION}"},
        "end": {"type": "string", "description": f"End of the window to search. {_ISO_DATETIME_DESCRIPTION}"},
        "min_duration_minutes": {"type": "integer", "description": "Minimum free block length, in minutes. Defaults to 30."},
    },
    "required": ["start", "end"],
}

_CREATE_EVENT_PARAMETERS = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Event title."},
        "start": {"type": "string", "description": _ISO_DATETIME_DESCRIPTION},
        "end": {"type": "string", "description": _ISO_DATETIME_DESCRIPTION},
        "description": {"type": "string", "description": "Optional event description."},
        "location": {"type": "string", "description": "Optional event location."},
        "confirm": {
            "type": "boolean",
            "description": "Set to true only after the user has explicitly confirmed creating this exact event.",
        },
    },
    "required": ["title", "start", "end"],
}

_UPDATE_EVENT_PARAMETERS = {
    "type": "object",
    "properties": {
        "event_id": {"type": "string", "description": "The Google Calendar event ID to update, obtained from a prior get_events call."},
        "title": {"type": "string", "description": "New title, if changing."},
        "start": {"type": "string", "description": f"New start, if changing. {_ISO_DATETIME_DESCRIPTION}"},
        "end": {"type": "string", "description": f"New end, if changing. {_ISO_DATETIME_DESCRIPTION}"},
        "description": {"type": "string", "description": "New description, if changing."},
        "location": {"type": "string", "description": "New location, if changing."},
        "confirm": {
            "type": "boolean",
            "description": "Set to true only after the user has explicitly confirmed this exact change.",
        },
    },
    "required": ["event_id"],
}

_DELETE_EVENT_PARAMETERS = {
    "type": "object",
    "properties": {
        "event_id": {"type": "string", "description": "The Google Calendar event ID to delete, obtained from a prior get_events call."},
        "confirm": {
            "type": "boolean",
            "description": "Set to true only after the user has explicitly confirmed this exact deletion.",
        },
    },
    "required": ["event_id"],
}


def _looks_like_iso_datetime(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        datetime.fromisoformat(value)
        return True
    except ValueError:
        return False


class CalendarTool(Tool):
    """Read and modify the user's Google Calendar. The first tool built on
    the general tool system -- see app/tools/base.py for the abstraction
    this implements."""

    name = "calendar"
    description = "Read and modify the user's Google Calendar: list events, find free time, create, update, and delete events."

    def __init__(
        self,
        *,
        client_factory: Callable[[], CalendarClientProtocol],
        require_create_confirmation: bool = True,
        default_calendar_id: str = "primary",
    ) -> None:
        """``client_factory`` is a zero-argument callable that returns a
        connected CalendarClientProtocol, or raises CalendarAuthRequired /
        CalendarUnavailable. It is resolved lazily on every call (not once
        at construction) so Miki can start up fine before the user has
        authenticated, and so a token refreshed later is picked up without
        restarting."""
        super().__init__(name=self.name, description=self.description)
        self._client_factory = client_factory
        self.require_create_confirmation = require_create_confirmation
        self.default_calendar_id = default_calendar_id
        # MIKI_GOOGLE_CALENDAR_ID may list several calendars ("primary,other@import.calendar.google.com") so
        # events can be READ from all of them. Google needs exactly one calendar id per write, and imported /
        # subscribed calendars are read-only, so writes go to the first (the user's own) calendar.
        self.read_calendar_ids = [c.strip() for c in default_calendar_id.split(",") if c.strip()] or ["primary"]
        self.write_calendar_id = self.read_calendar_ids[0]
        self.operations = [
            ToolOperation("get_events", "List calendar events within a datetime range.", _GET_EVENTS_PARAMETERS, ACCESS_READ),
            ToolOperation("find_free_time", "Find free/available time blocks within a datetime range.", _FIND_FREE_TIME_PARAMETERS, ACCESS_READ),
            ToolOperation("create_event", "Create a new calendar event.", _CREATE_EVENT_PARAMETERS, ACCESS_WRITE),
            ToolOperation("update_event", "Update an existing calendar event.", _UPDATE_EVENT_PARAMETERS, ACCESS_WRITE),
            ToolOperation("delete_event", "Delete a calendar event.", _DELETE_EVENT_PARAMETERS, ACCESS_DESTRUCTIVE),
        ]

    def is_available(self) -> bool:
        try:
            self._client_factory()
            return True
        except Exception:
            return False

    def status(self) -> dict[str, Any]:
        try:
            self._client_factory()
            return {"available": True, "connected": True}
        except CalendarAuthRequired:
            return {"available": False, "connected": False, "reason": "authentication_required"}
        except CalendarUnavailable as exc:
            return {"available": False, "connected": False, "reason": str(exc)}
        except Exception as exc:
            return {"available": False, "connected": False, "reason": str(exc)}

    def execute(self, operation: str, arguments: dict[str, Any]) -> ToolResult:
        try:
            client = self._client_factory()
        except CalendarAuthRequired:
            return ToolResult(
                success=False, operation=operation, error_type="authentication_required",
                message="I don't have access to your Google Calendar yet. You'll need to connect your Google account first.",
            )
        except CalendarUnavailable as exc:
            return ToolResult(success=False, operation=operation, error_type="unavailable", message=str(exc))

        handler = {
            "get_events": self._get_events,
            "find_free_time": self._find_free_time,
            "create_event": self._create_event,
            "update_event": self._update_event,
            "delete_event": self._delete_event,
        }.get(operation)
        if handler is None:
            return ToolResult(success=False, operation=operation, error_type="operation_not_found", message=f"Unknown calendar operation '{operation}'.")

        try:
            return handler(client, arguments)
        except CalendarApiError as exc:
            return ToolResult(success=False, operation=operation, error_type=exc.error_type, message=exc.message)
        except ToolError as exc:
            return ToolResult(success=False, operation=operation, error_type=exc.error_type, message=exc.message)

    # -- operations ---------------------------------------------------

    def _require_range(self, arguments: dict[str, Any]) -> tuple[str, str]:
        start, end = arguments.get("start"), arguments.get("end")
        if not start or not end:
            raise ToolError("invalid_arguments", "Both a start and end datetime are required.")
        if not _looks_like_iso_datetime(start) or not _looks_like_iso_datetime(end):
            raise ToolError("invalid_arguments", "start/end must be ISO 8601 datetimes, e.g. 2026-08-18T09:00:00+02:00.")
        return start, end

    def _get_events(self, client: CalendarClientProtocol, arguments: dict[str, Any]) -> ToolResult:
        start, end = self._require_range(arguments)
        calendar_id_input = arguments.get("calendar_id") or self.default_calendar_id
        
        all_events = []
        for cid in calendar_id_input.split(","):
            cid = cid.strip()
            if not cid:
                continue
            raw_events = client.list_events(time_min=start, time_max=end, calendar_id=cid)
            events = [CalendarEvent.from_google_event(item).to_dict() for item in raw_events]
            # Tag with source calendar
            for ev in events:
                ev["calendar_id"] = cid
            all_events.extend(events)
            
        # Sort merged events by start time
        all_events.sort(key=lambda e: (e.get("start", ""), e.get("title", "")))
        return ToolResult(success=True, operation="get_events", data={"events": all_events, "count": len(all_events)})

    def _find_free_time(self, client: CalendarClientProtocol, arguments: dict[str, Any]) -> ToolResult:
        start, end = self._require_range(arguments)
        min_duration = arguments.get("min_duration_minutes") or 30
        try:
            min_duration = int(min_duration)
        except (TypeError, ValueError):
            raise ToolError("invalid_arguments", "min_duration_minutes must be a number.")

        calendar_id_input = arguments.get("calendar_id") or self.default_calendar_id
        
        busy: list[tuple[datetime, datetime]] = []
        
        for cid in calendar_id_input.split(","):
            cid = cid.strip()
            if not cid:
                continue
                
            raw_events = client.list_events(time_min=start, time_max=end, calendar_id=cid)

            for item in raw_events:
                s = (item.get("start") or {}).get("dateTime")
                e = (item.get("end") or {}).get("dateTime")
                if s and e:
                    busy.append((datetime.fromisoformat(s), datetime.fromisoformat(e)))
        busy.sort(key=lambda pair: pair[0])

        range_start, range_end = datetime.fromisoformat(start), datetime.fromisoformat(end)
        free_blocks: list[tuple[datetime, datetime]] = []
        cursor = range_start
        for busy_start, busy_end in busy:
            if busy_start > cursor:
                gap_minutes = (busy_start - cursor).total_seconds() / 60
                if gap_minutes >= min_duration:
                    free_blocks.append((cursor, busy_start))
            if busy_end > cursor:
                cursor = busy_end
        if cursor < range_end:
            gap_minutes = (range_end - cursor).total_seconds() / 60
            if gap_minutes >= min_duration:
                free_blocks.append((cursor, range_end))

        data = {"free_blocks": [{"start": s.isoformat(), "end": e.isoformat()} for s, e in free_blocks]}
        return ToolResult(success=True, operation="find_free_time", data=data)

    def _write_calendar(self, arguments: dict[str, Any]) -> str:
        """The single calendar a write goes to (an explicit id if given, never a comma-separated list)."""
        requested = str(arguments.get("calendar_id") or "").split(",")[0].strip()
        return requested or self.write_calendar_id

    def _raise_event_not_found(self, client: CalendarClientProtocol, event_id: str, verb: str) -> None:
        """Explain *why* an event can't be changed: gone, or living on a read-only calendar."""
        for other in self.read_calendar_ids:
            if other != self.write_calendar_id and client.get_event(event_id, calendar_id=other) is not None:
                raise ToolError("read_only_calendar", f"That event is on a read-only calendar, so I can't {verb} it. I can only change events on your own calendar.")
        raise ToolError("event_not_found", f"I couldn't find that event -- it may have already been {'changed' if verb == 'change' else 'deleted'} or removed.")

    def _create_event(self, client: CalendarClientProtocol, arguments: dict[str, Any]) -> ToolResult:
        title, start, end = arguments.get("title"), arguments.get("start"), arguments.get("end")
        if not title or not start or not end:
            raise ToolError("invalid_arguments", "A title, start, and end datetime are required to create an event.")
        if not _looks_like_iso_datetime(start) or not _looks_like_iso_datetime(end):
            raise ToolError("invalid_arguments", "start/end must be ISO 8601 datetimes, e.g. 2026-08-18T18:00:00+02:00.")

        confirmed = bool(arguments.get("confirm"))
        if self.require_create_confirmation and not confirmed:
            preview = CalendarEvent(id="", title=title, start=start, end=end, location=arguments.get("location"), description=arguments.get("description"))
            return ToolResult(
                success=True, operation="create_event", requires_confirmation=True,
                data={"preview": preview.to_dict()},
                message=f"Ready to add '{title}' from {start} to {end}. Shall I add it?",
            )

        event_body: dict[str, Any] = {"summary": title, "start": {"dateTime": start}, "end": {"dateTime": end}}
        if arguments.get("description"):
            event_body["description"] = arguments["description"]
        if arguments.get("location"):
            event_body["location"] = arguments["location"]

        calendar_id = self._write_calendar(arguments)
        raw = client.insert_event(event_body, calendar_id=calendar_id)
        event = CalendarEvent.from_google_event(raw)
        return ToolResult(success=True, operation="create_event", data={"event": event.to_dict()}, message=f"Added '{event.title}'.")

    def _update_event(self, client: CalendarClientProtocol, arguments: dict[str, Any]) -> ToolResult:
        event_id = arguments.get("event_id")
        if not event_id:
            raise ToolError("invalid_arguments", "An event_id is required to update an event (get it via get_events first).")

        calendar_id = self._write_calendar(arguments)
        existing_raw = client.get_event(event_id, calendar_id=calendar_id)
        if existing_raw is None:
            self._raise_event_not_found(client, event_id, "change")
        existing = CalendarEvent.from_google_event(existing_raw)

        changes: dict[str, Any] = {}
        if arguments.get("title"):
            changes["summary"] = arguments["title"]
        if arguments.get("start"):
            if not _looks_like_iso_datetime(arguments["start"]):
                raise ToolError("invalid_arguments", "start must be an ISO 8601 datetime.")
            changes["start"] = {"dateTime": arguments["start"]}
        if arguments.get("end"):
            if not _looks_like_iso_datetime(arguments["end"]):
                raise ToolError("invalid_arguments", "end must be an ISO 8601 datetime.")
            changes["end"] = {"dateTime": arguments["end"]}
        if "description" in arguments:
            changes["description"] = arguments["description"]
        if "location" in arguments:
            changes["location"] = arguments["location"]

        if not changes:
            raise ToolError("invalid_arguments", "No changes were provided to update.")

        confirmed = bool(arguments.get("confirm"))
        if not confirmed:
            proposed = CalendarEvent(
                id=existing.id,
                title=arguments.get("title") or existing.title,
                start=arguments.get("start") or existing.start,
                end=arguments.get("end") or existing.end,
                location=arguments.get("location", existing.location),
                description=arguments.get("description", existing.description),
            )
            return ToolResult(
                success=True, operation="update_event", requires_confirmation=True,
                data={"current": existing.to_dict(), "proposed": proposed.to_dict()},
                message=f"Found '{existing.title}' ({existing.start} - {existing.end}). Change it to '{proposed.title}' ({proposed.start} - {proposed.end})?",
            )

        raw = client.update_event(event_id, changes, calendar_id=calendar_id)
        updated = CalendarEvent.from_google_event(raw)
        return ToolResult(success=True, operation="update_event", data={"event": updated.to_dict()}, message=f"Updated '{updated.title}'.")

    def _delete_event(self, client: CalendarClientProtocol, arguments: dict[str, Any]) -> ToolResult:
        event_id = arguments.get("event_id")
        if not event_id:
            raise ToolError("invalid_arguments", "An event_id is required to delete an event (get it via get_events first).")

        calendar_id = self._write_calendar(arguments)
        existing_raw = client.get_event(event_id, calendar_id=calendar_id)
        if existing_raw is None:
            self._raise_event_not_found(client, event_id, "delete")
        existing = CalendarEvent.from_google_event(existing_raw)

        confirmed = bool(arguments.get("confirm"))
        if not confirmed:
            return ToolResult(
                success=True, operation="delete_event", requires_confirmation=True,
                data={"event": existing.to_dict()},
                message=f"Found '{existing.title}' ({existing.start} - {existing.end}). Delete it?",
            )

        client.delete_event(event_id, calendar_id=calendar_id)
        return ToolResult(success=True, operation="delete_event", data={"deleted_event": existing.to_dict()}, message=f"Deleted '{existing.title}'.")
