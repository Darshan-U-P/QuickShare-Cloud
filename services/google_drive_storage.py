import json
import os
import secrets
import time
from threading import Lock
from urllib.parse import quote

import requests

from services.storage_errors import StorageError


class GoogleDriveStorageError(StorageError):
    """Google Drive configuration or API error."""


DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
FOLDER_NAME = "QuickShare Cloud Storage"
_TOKEN_CACHE = {"value": "", "expires_at": 0.0}
_TOKEN_LOCK = Lock()
_FOLDER_CACHE = {"id": ""}
_FOLDER_LOCK = Lock()


def is_configured() -> bool:
    return all(os.getenv(name, "").strip() for name in (
        "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_DRIVE_REFRESH_TOKEN"
    ))


def _access_token() -> str:
    if not is_configured():
        raise GoogleDriveStorageError(
            "Google Drive storage is not connected. An administrator must connect Drive and set GOOGLE_DRIVE_REFRESH_TOKEN in Vercel."
        )
    with _TOKEN_LOCK:
        if _TOKEN_CACHE["value"] and time.time() < _TOKEN_CACHE["expires_at"] - 60:
            return _TOKEN_CACHE["value"]
        try:
            response = requests.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": os.environ["GOOGLE_CLIENT_ID"].strip(),
                    "client_secret": os.environ["GOOGLE_CLIENT_SECRET"].strip(),
                    "refresh_token": os.environ["GOOGLE_DRIVE_REFRESH_TOKEN"].strip(),
                    "grant_type": "refresh_token",
                },
                timeout=20,
            )
        except requests.RequestException as exc:
            raise GoogleDriveStorageError("Could not connect to Google to refresh Drive access.") from exc
        if not response.ok:
            body = (response.text or "").lower()
            if response.status_code in (400, 401) or "invalid_grant" in body:
                raise GoogleDriveStorageError(
                    "Google Drive authorization expired or was revoked. The storage administrator must reconnect Google Drive."
                )
            raise GoogleDriveStorageError(f"Google could not refresh Drive access (HTTP {response.status_code}).")
        try:
            payload = response.json()
            token = payload["access_token"]
            expires = int(payload.get("expires_in", 3600))
        except (ValueError, KeyError, TypeError) as exc:
            raise GoogleDriveStorageError("Google returned an invalid Drive access token response.") from exc
        _TOKEN_CACHE["value"] = token
        _TOKEN_CACHE["expires_at"] = time.time() + max(60, expires)
        return token


def _headers():
    return {"Authorization": f"Bearer {_access_token()}"}


def _raise_for_response(response, operation: str):
    if response.ok:
        return
    if response.status_code in (401, 403):
        body = (getattr(response, "text", "") or "").lower()
        if "insufficient" in body or "scope" in body:
            raise GoogleDriveStorageError(
                "Google Drive denied access. Reconnect Drive and grant the requested drive.file permission."
            )
        raise GoogleDriveStorageError(
            "Google Drive denied this operation. Check the connected account and Drive API configuration."
        )
    if response.status_code == 404:
        raise GoogleDriveStorageError("The Google Drive file or storage folder was not found.")
    if response.status_code == 429:
        raise GoogleDriveStorageError("Google Drive rate limit reached. Please retry shortly.")
    raise GoogleDriveStorageError(f"Google Drive returned HTTP {response.status_code} while {operation}.")


def _list_files(query: str, fields: str = "files(id,name,mimeType,size,appProperties),nextPageToken") -> list[dict]:
    results = []
    page_token = None
    while True:
        params = {
            "q": query,
            "pageSize": 1000,
            "orderBy": "name",
            "fields": fields,
            "spaces": "drive",
        }
        if page_token:
            params["pageToken"] = page_token
        try:
            response = requests.get(
                f"{DRIVE_API}/files",
                headers=_headers(),
                params=params,
                timeout=20,
            )
        except requests.RequestException as exc:
            raise GoogleDriveStorageError("Could not connect to Google Drive to list storage files.") from exc
        _raise_for_response(response, "listing files")
        try:
            payload = response.json()
            results.extend(payload.get("files", []))
            page_token = payload.get("nextPageToken")
        except (ValueError, AttributeError, TypeError) as exc:
            raise GoogleDriveStorageError("Google Drive returned an invalid file listing.") from exc
        if not page_token:
            break
    return results


def _ensure_folder() -> str:
    configured_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "").strip()
    if configured_id:
        return configured_id
    with _FOLDER_LOCK:
        if _FOLDER_CACHE["id"]:
            return _FOLDER_CACHE["id"]
        escaped_name = FOLDER_NAME.replace("'", "\\'")
        query = (
            f"name = '{escaped_name}' and "
            "mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        )
        folders = _list_files(query, fields="files(id,name,mimeType)")
        if folders:
            _FOLDER_CACHE["id"] = folders[0]["id"]
            return _FOLDER_CACHE["id"]
        try:
            response = requests.post(
                f"{DRIVE_API}/files",
                headers={**_headers(), "Content-Type": "application/json"},
                json={
                    "name": FOLDER_NAME,
                    "mimeType": "application/vnd.google-apps.folder",
                },
                params={"fields": "id,name,mimeType"},
                timeout=20,
            )
        except requests.RequestException as exc:
            raise GoogleDriveStorageError("Could not create the QuickShare folder in Google Drive.") from exc
        _raise_for_response(response, "creating the QuickShare folder")
        try:
            _FOLDER_CACHE["id"] = response.json()["id"]
            return _FOLDER_CACHE["id"]
        except (ValueError, KeyError, TypeError) as exc:
            raise GoogleDriveStorageError("Google Drive did not return the new folder ID.") from exc


def upload_file_bytes(storage_path: str, content: bytes, share_id: str) -> dict:
    """Upload a private file into the app-owned QuickShare Drive folder."""
    folder_id = _ensure_folder()
    boundary = "quickshare_" + secrets.token_hex(16)
    metadata = {
        "name": f"QuickShare {storage_path.rsplit('/', 1)[-1]}",
        "mimeType": "application/octet-stream",
        "parents": [folder_id],
        "appProperties": {
            "quickshareShareId": share_id,
            "quicksharePath": storage_path,
        },
    }
    body = (
        f"--{boundary}\r\n"
        "Content-Type: application/json; charset=UTF-8\r\n\r\n"
        + json.dumps(metadata, separators=(",", ":"))
        + f"\r\n--{boundary}\r\n"
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8") + content + f"\r\n--{boundary}--\r\n".encode("utf-8")
    try:
        response = requests.post(
            f"{DRIVE_UPLOAD_API}/files",
            headers={
                **_headers(),
                "Content-Type": f"multipart/related; boundary={boundary}",
            },
            params={"uploadType": "multipart", "fields": "id,name,mimeType,size"},
            data=body,
            timeout=40,
        )
    except requests.RequestException as exc:
        raise GoogleDriveStorageError("Could not upload the file to Google Drive.") from exc
    _raise_for_response(response, "uploading the file")
    try:
        result = response.json()
        if not result.get("id"):
            raise KeyError("id")
        return result
    except (ValueError, KeyError, TypeError) as exc:
        raise GoogleDriveStorageError("Google Drive returned an invalid upload response.") from exc


def download_private_file(file_id: str) -> bytes:
    if not file_id or not isinstance(file_id, str):
        raise GoogleDriveStorageError("This share has no Google Drive file ID.")
    try:
        response = requests.get(
            f"{DRIVE_API}/files/{quote(file_id, safe='')}",
            headers=_headers(),
            params={"alt": "media"},
            timeout=40,
        )
    except requests.RequestException as exc:
        raise GoogleDriveStorageError("Could not connect to Google Drive to download the file.") from exc
    _raise_for_response(response, "downloading the file")
    return response.content


def delete_private_file(file_id: str) -> str:
    if not file_id or not isinstance(file_id, str):
        raise GoogleDriveStorageError("This share has no Google Drive file ID for cleanup.")
    try:
        response = requests.delete(
            f"{DRIVE_API}/files/{quote(file_id, safe='')}",
            headers=_headers(),
            timeout=30,
        )
    except requests.RequestException as exc:
        raise GoogleDriveStorageError("Could not connect to Google Drive to delete the file.") from exc
    if response.status_code == 404:
        return "already_missing"
    _raise_for_response(response, "deleting the file")
    return "deleted"
