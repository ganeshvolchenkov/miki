from __future__ import annotations
from pathlib import Path
from app.tools.calendar.auth import CalendarAuthRequired, CalendarUnavailable, load_credentials
from app.tools.drive.client import GoogleDriveClient, DriveClientProtocol
from app.tools.drive.tool import DriveTool

def build_drive_client_factory(*, credentials_path: Path, token_path: Path, folder_name: str | None = None):
    def factory() -> DriveClientProtocol:
        try:
            import googleapiclient  # noqa: F401
        except ImportError as exc:
            raise CalendarUnavailable("Google packages not installed.") from exc

        creds = load_credentials(credentials_path, token_path)
        if creds is None:
            raise CalendarAuthRequired("Google Drive authentication is required.")
        return GoogleDriveClient(creds, folder_name=folder_name)
    return factory

def build_drive_tool(*, credentials_path: Path, token_path: Path, folder_name: str | None = None) -> DriveTool:
    factory = build_drive_client_factory(credentials_path=credentials_path, token_path=token_path, folder_name=folder_name)
    return DriveTool(client_factory=factory)
