from __future__ import annotations

import logging
from typing import Any, Callable

from app.tools.base import ACCESS_READ, ACCESS_WRITE, Tool, ToolError, ToolOperation, ToolResult
from app.tools.mail.client import MailApiError, MailClientProtocol
from app.tools.calendar.auth import CalendarAuthRequired, CalendarUnavailable

logger = logging.getLogger(__name__)

class MailTool(Tool):
    def __init__(self, *, client_factory: Callable[[], MailClientProtocol]) -> None:
        super().__init__(
            name="mail",
            description="Read and draft emails using Gmail.",
            operations=[
                ToolOperation(
                    name="list_emails",
                    description="Search for emails in the user's inbox.",
                    access=ACCESS_READ,
                    parameters={
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "Gmail search query (e.g. 'is:unread', 'from:boss@company.com')."},
                            "max_results": {"type": "integer", "description": "Number of results to return (max 20)."}
                        }
                    }
                ),
                ToolOperation(
                    name="draft_email",
                    description="Creates an email draft. The user MUST review and send it manually. Used to satisfy human-in-the-loop requirement.",
                    access=ACCESS_WRITE,
                    parameters={
                        "type": "object",
                        "properties": {
                            "to": {"type": "string", "description": "Recipient email address."},
                            "subject": {"type": "string", "description": "Email subject."},
                            "body": {"type": "string", "description": "Email body content."}
                        },
                        "required": ["to", "subject", "body"]
                    }
                )
            ]
        )
        self._client_factory = client_factory

    def execute(self, operation: str, arguments: dict[str, Any]) -> ToolResult:
        try:
            client = self._client_factory()
        except CalendarAuthRequired as exc:
            raise ToolError("auth_required", str(exc)) from exc
        except CalendarUnavailable as exc:
            raise ToolError("unavailable", str(exc)) from exc

        try:
            if operation == "list_emails":
                return self._list_emails(client, arguments)
            elif operation == "draft_email":
                return self._draft_email(client, arguments)
            else:
                raise ToolError("unknown_operation", f"Unknown operation: {operation}")
        except MailApiError as exc:
            raise ToolError(exc.error_type, exc.message) from exc

    def _list_emails(self, client: MailClientProtocol, arguments: dict[str, Any]) -> ToolResult:
        query = arguments.get("query", "")
        max_results = min(int(arguments.get("max_results", 10)), 20)
        emails = client.list_emails(query=query, max_results=max_results)
        return ToolResult(success=True, operation="list_emails", data={"emails": emails, "count": len(emails)})

    def _draft_email(self, client: MailClientProtocol, arguments: dict[str, Any]) -> ToolResult:
        to = arguments.get("to")
        subject = arguments.get("subject")
        body = arguments.get("body")
        
        if not to or not subject or not body:
            raise ToolError("invalid_arguments", "Missing 'to', 'subject', or 'body'")
            
        draft = client.create_draft(to=to, subject=subject, body=body)
        
        return ToolResult(
            success=True,
            operation="draft_email",
            data={"draft_id": draft.get("id")},
            message="Draft created successfully! Tell the user they can review and send it from their Gmail Drafts folder."
        )

    def get_client(self) -> MailClientProtocol:
        """The live Gmail client, for callers (the dashboard's triage) that need more than tool operations."""
        try:
            return self._client_factory()
        except CalendarAuthRequired as exc:
            raise ToolError("auth_required", str(exc)) from exc
        except CalendarUnavailable as exc:
            raise ToolError("unavailable", str(exc)) from exc

    def is_available(self) -> bool:
        try:
            self._client_factory()
            return True
        except Exception:
            return False

    def status(self) -> dict[str, Any]:
        try:
            self._client_factory()
            return {"connected": True}
        except CalendarAuthRequired as exc:
            return {"connected": False, "reason": str(exc)}
        except Exception as exc:
            return {"connected": False, "reason": f"Error: {exc}"}
