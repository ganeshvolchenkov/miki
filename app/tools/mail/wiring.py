from __future__ import annotations

from pathlib import Path

from app.tools.calendar.auth import CalendarAuthRequired, CalendarUnavailable, load_credentials
from app.tools.mail.client import GoogleMailClient, MailClientProtocol
from app.tools.mail.tool import MailTool


def build_mail_client_factory(*, credentials_path: Path, token_path: Path):
    def factory() -> MailClientProtocol:
        try:
            import googleapiclient  # noqa: F401
        except ImportError as exc:
            raise CalendarUnavailable(
                "Google Mail packages are not installed."
            ) from exc

        creds = load_credentials(credentials_path, token_path)
        if creds is None:
            raise CalendarAuthRequired("Google Mail authentication is required.")
        return GoogleMailClient(creds)

    return factory


def build_mail_tool(
    *,
    credentials_path: Path,
    token_path: Path,
) -> MailTool:
    factory = build_mail_client_factory(credentials_path=credentials_path, token_path=token_path)
    return MailTool(client_factory=factory)
