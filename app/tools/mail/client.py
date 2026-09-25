from __future__ import annotations

import logging
from typing import Any, Protocol

logger = logging.getLogger(__name__)

class MailApiError(Exception):
    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.message = message

class MailClientProtocol(Protocol):
    def list_emails(self, *, query: str = "", max_results: int = 10) -> list[dict[str, Any]]:
        ...

    def get_email(self, message_id: str) -> dict[str, Any] | None:
        ...

    def create_draft(self, *, to: str, subject: str, body: str) -> dict[str, Any]:
        ...

class GoogleMailClient:
    def __init__(self, credentials: Any) -> None:
        from googleapiclient.discovery import build
        self._service = build("gmail", "v1", credentials=credentials, cache_discovery=False)

    def list_emails(self, *, query: str = "", max_results: int = 10) -> list[dict[str, Any]]:
        response = self._call(
            lambda: self._service.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
        )
        messages = (response or {}).get("messages", [])
        results = []
        for msg in messages:
            full = self.get_email(msg["id"])
            if full:
                results.append(full)
        return results

    def get_email(self, message_id: str) -> dict[str, Any] | None:
        response = self._call(
            lambda: self._service.users().messages().get(userId="me", id=message_id, format="full").execute(),
            not_found_returns_none=True
        )
        if not response:
            return None
            
        headers = response.get("payload", {}).get("headers", [])
        subject = next((h["value"] for h in headers if h["name"].lower() == "subject"), "(no subject)")
        sender = next((h["value"] for h in headers if h["name"].lower() == "from"), "(unknown)")
        snippet = response.get("snippet", "")
        
        return {
            "id": response["id"],
            "subject": subject,
            "sender": sender,
            "snippet": snippet
        }

    # ---- triage fast path -------------------------------------------------------------------------
    _TRIAGE_HEADERS = ["From", "Subject", "Date", "List-Unsubscribe", "Precedence", "Auto-Submitted"]

    def triage_candidates(self, *, query: str, max_results: int = 25) -> list[dict[str, Any]]:
        """Recent mail as light metadata (no bodies), fetched in ONE batched request.

        ``list_emails`` downloads every message in full, one call each. For triage all we need is who,
        what, when, the labels and the thread id (for a link that opens the exact conversation).
        """
        response = self._call(
            lambda: self._service.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
        )
        ids = [m["id"] for m in (response or {}).get("messages", [])]
        if not ids:
            return []

        fetched: dict[str, dict[str, Any]] = {}

        def collect(request_id: str, message: Any, error: Any) -> None:
            if error is None and message:
                fetched[request_id] = message

        batch = self._service.new_batch_http_request(callback=collect)
        for message_id in ids:
            batch.add(
                self._service.users().messages().get(
                    userId="me", id=message_id, format="metadata", metadataHeaders=self._TRIAGE_HEADERS
                ),
                request_id=message_id,
            )
        self._call(batch.execute)
        return [self._parse_metadata(fetched[i]) for i in ids if i in fetched]

    def profile_email(self) -> str:
        """The signed-in Gmail address (used to build links that open in the right account)."""
        if not getattr(self, "_profile_email", None):
            profile = self._call(lambda: self._service.users().getProfile(userId="me").execute())
            self._profile_email = (profile or {}).get("emailAddress", "")
        return self._profile_email

    @staticmethod
    def _parse_metadata(message: dict[str, Any]) -> dict[str, Any]:
        from datetime import datetime, timezone
        from email.utils import parseaddr

        headers = {h["name"].lower(): h["value"] for h in message.get("payload", {}).get("headers", [])}
        name, address = parseaddr(headers.get("from", ""))
        labels = message.get("labelIds", [])
        try:
            when = datetime.fromtimestamp(int(message.get("internalDate", 0)) / 1000, tz=timezone.utc).astimezone().isoformat(timespec="minutes")
        except (TypeError, ValueError, OSError):
            when = ""
        precedence = headers.get("precedence", "").lower()
        return {
            "id": message["id"],
            "thread_id": message.get("threadId", message["id"]),
            "subject": headers.get("subject", "(no subject)"),
            "sender": headers.get("from", "(unknown)"),
            "sender_name": name or address,
            "sender_email": address,
            "snippet": message.get("snippet", ""),
            "date": when,
            "labels": labels,
            "unread": "UNREAD" in labels,
            "starred": "STARRED" in labels,
            "important": "IMPORTANT" in labels,
            "bulk": bool(headers.get("list-unsubscribe")) or precedence in {"bulk", "list", "junk"}
            or headers.get("auto-submitted", "no").lower() != "no",
        }

    def create_draft(self, *, to: str, subject: str, body: str) -> dict[str, Any]:
        from email.message import EmailMessage
        import base64
        
        message = EmailMessage()
        message.set_content(body)
        message["To"] = to
        message["Subject"] = subject
        
        encoded_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
        create_message = {"message": {"raw": encoded_message}}
        
        return self._call(lambda: self._service.users().drafts().create(userId="me", body=create_message).execute())

    def _call(self, fn, *, not_found_returns_none: bool = False) -> Any:
        from googleapiclient.errors import HttpError
        try:
            return fn()
        except HttpError as exc:
            status = getattr(exc.resp, "status", None)
            if status == 404 and not_found_returns_none:
                return None
            raise MailApiError("api_error", f"Gmail API error (status {status}).") from exc
        except Exception as exc:
            raise MailApiError("network_error", "Could not reach Gmail.") from exc
