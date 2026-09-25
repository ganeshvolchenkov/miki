from __future__ import annotations

import logging
from typing import Any, Protocol

logger = logging.getLogger(__name__)

class DriveApiError(Exception):
    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.message = message

class DriveClientProtocol(Protocol):
    def search_files(self, *, query: str = "", max_results: int = 15) -> list[dict[str, Any]]:
        ...

    def get_file(self, file_id: str) -> dict[str, Any] | None:
        ...

class GoogleDriveClient:
    def __init__(self, credentials: Any, folder_name: str | None = None) -> None:
        from googleapiclient.discovery import build
        self._service = build("drive", "v3", credentials=credentials, cache_discovery=False)
        self._folder_name = folder_name
        self._folder_id = None
        self._folder_resolved = False

    def _resolve_folder_id(self) -> None:
        if self._folder_resolved: return
        self._folder_resolved = True
        if not self._folder_name: return
        
        # Search for the folder by name
        res = self._call(
            lambda: self._service.files().list(
                q=f"name='{self._folder_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false",
                fields="files(id)"
            ).execute()
        )
        files = (res or {}).get("files", [])
        if files:
            self._folder_id = files[0]["id"]

    def search_files(self, *, query: str = "", max_results: int = 15) -> list[dict[str, Any]]:
        self._resolve_folder_id()
        base_q = "trashed=false"
        if self._folder_id:
            base_q += f" and '{self._folder_id}' in parents"
            
        if not query:
            final_query = base_q
        else:
            final_query = f"({query}) and {base_q}"
            
        return self._call(
            lambda: self._service.files().list(
                q=final_query,
                pageSize=max_results,
                fields="files(id, name, mimeType, modifiedTime, webViewLink)",
                orderBy="modifiedTime desc"
            ).execute()
        ).get("files", [])

    def get_file(self, file_id: str) -> dict[str, Any] | None:
        return self._call(
            lambda: self._service.files().get(
                fileId=file_id,
                fields="id, name, mimeType, modifiedTime, webViewLink, owners"
            ).execute(),
            not_found_returns_none=True
        )

    def _call(self, fn, *, not_found_returns_none: bool = False) -> Any:
        from googleapiclient.errors import HttpError
        try:
            return fn()
        except HttpError as exc:
            status = getattr(exc.resp, "status", None)
            if status == 404 and not_found_returns_none:
                return None
            raise DriveApiError("api_error", f"Drive API error (status {status}).") from exc
        except Exception as exc:
            raise DriveApiError("network_error", "Could not reach Google Drive.") from exc
