import json
import os
import re
import secrets
import time
from threading import Lock
from datetime import datetime, timezone
from urllib.parse import quote

import requests

from services.storage_errors import StorageError


class GoogleDriveStorageError(StorageError):
    """Google Drive configuration or API error."""


DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
FOLDER_NAME = "QuickShare Cloud Storage"
LARGE_UPLOAD_CONFIG_KEY = "large_upload_allowlist_v1"
LARGE_UPLOAD_CONFIG_NAME = "QuickShare Cloud - Large Transfer Permissions.json"
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



def _large_upload_config_file() -> dict | None:
    folder_id = _ensure_folder()
    query = (
        f"'{folder_id}' in parents and "
        f"appProperties has {{ key='quickshareConfigKey' and value='{LARGE_UPLOAD_CONFIG_KEY}' }} and "
        "trashed = false"
    )
    files = _list_files(query, fields="files(id,name,mimeType,size,appProperties)")
    return files[0] if files else None


def get_large_upload_allowlist() -> list[str]:
    """Read approved 50 GB account emails from a private app-owned Drive config file."""
    config_file = _large_upload_config_file()
    if not config_file:
        return []
    file_id = str(config_file.get("id", "")).strip()
    if not file_id:
        raise GoogleDriveStorageError("The private large-transfer permissions file is missing its ID.")
    try:
        response = requests.get(
            f"{DRIVE_API}/files/{quote(file_id, safe='')}",
            headers=_headers(),
            params={"alt": "media"},
            timeout=20,
        )
    except requests.RequestException as exc:
        raise GoogleDriveStorageError("Could not read private large-transfer permissions from Drive.") from exc
    _raise_for_response(response, "reading large-transfer permissions")
    try:
        raw = getattr(response, "content", b"")
        if not raw:
            raw = (getattr(response, "text", "") or "").encode("utf-8")
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, TypeError) as exc:
        raise GoogleDriveStorageError("The private large-transfer permissions file is invalid.") from exc
    emails = payload.get("approved_emails") if isinstance(payload, dict) else None
    if not isinstance(emails, list):
        raise GoogleDriveStorageError("The private large-transfer permissions list is invalid.")
    normalized = set()
    for value in emails:
        if not isinstance(value, str):
            continue
        email = value.strip().lower()
        if email and len(email) <= 254 and re.fullmatch(r"[^@\\s]+@[^@\\s]+\\.[^@\\s]+", email):
            normalized.add(email)
    return sorted(normalized)


def set_large_upload_allowlist(emails: list[str]) -> list[str]:
    """Store the full approved-email allowlist privately in the app's Drive folder."""
    normalized = set()
    for value in emails:
        if not isinstance(value, str):
            continue
        email = value.strip().lower()
        if not email or len(email) > 254 or not re.fullmatch(r"[^@\\s]+@[^@\\s]+\\.[^@\\s]+", email):
            raise GoogleDriveStorageError("One of the approved email addresses is invalid.")
        normalized.add(email)
    approved = sorted(normalized)
    payload = json.dumps(
        {
            "version": 1,
            "approved_emails": approved,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")
    config_file = _large_upload_config_file()
    if config_file:
        file_id = str(config_file.get("id", "")).strip()
        if not file_id:
            raise GoogleDriveStorageError("The private large-transfer permissions file is missing its ID.")
        try:
            response = requests.patch(
                f"{DRIVE_UPLOAD_API}/files/{quote(file_id, safe='')}",
                headers={**_headers(), "Content-Type": "application/json; charset=UTF-8"},
                params={"uploadType": "media"},
                data=payload,
                timeout=20,
            )
        except requests.RequestException as exc:
            raise GoogleDriveStorageError("Could not update private large-transfer permissions in Drive.") from exc
        _raise_for_response(response, "updating large-transfer permissions")
    else:
        folder_id = _ensure_folder()
        boundary = "quickshare_config_" + secrets.token_hex(12)
        metadata = {
            "name": LARGE_UPLOAD_CONFIG_NAME,
            "mimeType": "application/json",
            "parents": [folder_id],
            "appProperties": {"quickshareConfigKey": LARGE_UPLOAD_CONFIG_KEY},
        }
        body = (
            f"--{boundary}\\r\\n"
            "Content-Type: application/json; charset=UTF-8\\r\\n\\r\\n"
            + json.dumps(metadata, separators=(",", ":"))
            + f"\\r\\n--{boundary}\\r\\n"
            "Content-Type: application/json; charset=UTF-8\\r\\n\\r\\n"
        ).encode("utf-8") + payload + f"\\r\\n--{boundary}--\\r\\n".encode("utf-8")
        try:
            response = requests.post(
                f"{DRIVE_UPLOAD_API}/files",
                headers={**_headers(), "Content-Type": f"multipart/related; boundary={boundary}"},
                params={"uploadType": "multipart", "fields": "id,name,mimeType,appProperties"},
                data=body,
                timeout=20,
            )
        except requests.RequestException as exc:
            raise GoogleDriveStorageError("Could not create private large-transfer permissions in Drive.") from exc
        _raise_for_response(response, "creating large-transfer permissions")
        try:
            if not response.json().get("id"):
                raise KeyError("id")
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise GoogleDriveStorageError("Google Drive did not return the private permissions file ID.") from exc
    return approved



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



def initiate_resumable_upload(
    storage_path: str,
    share_id: str,
    upload_nonce: str,
    file_size: int,
    mime_type: str,
) -> str:
    """Create a Drive resumable-upload session without relaying file bytes through Vercel."""
    folder_id = _ensure_folder()
    metadata = {
        "name": f"QuickShare {storage_path.rsplit('/', 1)[-1]}",
        "mimeType": mime_type or "application/octet-stream",
        "parents": [folder_id],
        "appProperties": {
            "quickshareShareId": share_id,
            "quickshareUploadNonce": upload_nonce,
        },
    }
    try:
        response = requests.post(
            f"{DRIVE_UPLOAD_API}/files",
            headers={
                **_headers(),
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Type": mime_type or "application/octet-stream",
                "X-Upload-Content-Length": str(int(file_size)),
            },
            params={"uploadType": "resumable"},
            json=metadata,
            timeout=30,
        )
    except requests.RequestException as exc:
        raise GoogleDriveStorageError("Could not start a resumable Google Drive upload.") from exc
    _raise_for_response(response, "starting a resumable upload")
    session_uri = (getattr(response, "headers", {}) or {}).get("Location", "")
    if not isinstance(session_uri, str) or not session_uri.startswith("https://www.googleapis.com/upload/drive/v3/files?"):
        raise GoogleDriveStorageError("Google Drive did not return a valid resumable-upload session.")
    return session_uri



def upload_resumable_chunk(
    session_uri: str,
    *,
    start: int,
    end: int,
    total_size: int,
    content: bytes,
) -> dict:
    """Forward one small browser-uploaded chunk from Vercel to Google's resumable session."""
    if (
        not isinstance(session_uri, str)
        or not session_uri.startswith("https://www.googleapis.com/upload/drive/v3/files?")
    ):
        raise GoogleDriveStorageError("This resumable Google Drive upload session is invalid.")
    if not isinstance(content, bytes) or not content:
        raise GoogleDriveStorageError("The upload chunk is empty.")
    if start < 0 or end < start or total_size <= end or len(content) != end - start + 1:
        raise GoogleDriveStorageError("The upload chunk's byte range does not match its content.")
    if len(content) > 4 * 1024 * 1024:
        raise GoogleDriveStorageError("Upload chunks must be 4 MiB or smaller.")

    try:
        response = requests.put(
            session_uri,
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(content)),
                "Content-Range": f"bytes {start}-{end}/{total_size}",
            },
            data=content,
            timeout=(15, 120),
        )
    except requests.RequestException as exc:
        raise GoogleDriveStorageError(
            "The connection to Google Drive was interrupted while forwarding a chunk. Retry this chunk."
        ) from exc

    if response.status_code == 308:
        acknowledged_range = (getattr(response, "headers", {}) or {}).get("Range", "")
        match = re.fullmatch(r"bytes=0-(\d+)", acknowledged_range.strip())
        next_offset = int(match.group(1)) + 1 if match else 0
        if next_offset < 0 or next_offset > total_size:
            raise GoogleDriveStorageError("Google Drive returned an invalid resumable-upload offset.")
        return {"complete": False, "next_offset": next_offset}

    if response.status_code in (200, 201):
        try:
            payload = response.json()
        except ValueError as exc:
            raise GoogleDriveStorageError("Google Drive completed the upload but returned invalid metadata.") from exc
        if not isinstance(payload, dict) or not payload.get("id"):
            raise GoogleDriveStorageError("Google Drive completed the upload without returning a file ID.")
        return {"complete": True, "next_offset": total_size, "file": payload}

    # A previous chunk may have reached Drive even if Vercel's response was lost.
    # If retrying that chunk returns 400, ask Drive for the committed offset before
    # deciding whether this is a real upload error.
    if response.status_code in (400, 404, 409):
        try:
            status_response = requests.put(
                session_uri,
                headers={
                    "Content-Length": "0",
                    "Content-Range": f"bytes */{total_size}",
                },
                data=b"",
                timeout=(15, 30),
            )
        except requests.RequestException:
            status_response = None
        if status_response is not None:
            if status_response.status_code == 308:
                status_range = (getattr(status_response, "headers", {}) or {}).get("Range", "")
                status_match = re.fullmatch(r"bytes=0-(\d+)", status_range.strip())
                committed_offset = int(status_match.group(1)) + 1 if status_match else 0
                if 0 <= committed_offset <= total_size:
                    return {"complete": False, "next_offset": committed_offset}
            elif status_response.status_code in (200, 201):
                try:
                    payload = status_response.json()
                except ValueError as exc:
                    raise GoogleDriveStorageError("Google Drive completed the upload but returned invalid metadata.") from exc
                if isinstance(payload, dict) and payload.get("id"):
                    return {"complete": True, "next_offset": total_size, "file": payload}

    _raise_for_response(response, "uploading a resumable chunk")
    raise GoogleDriveStorageError(f"Google Drive returned unexpected HTTP {response.status_code} for a chunk.")



def verify_resumable_upload(
    file_id: str,
    *,
    share_id: str,
    upload_nonce: str,
    expected_size: int,
) -> dict:
    """Verify that the browser completed the exact private file initialized by QuickShare."""
    if not isinstance(file_id, str) or not file_id.strip():
        raise GoogleDriveStorageError("Google Drive did not return a completed file ID.")
    try:
        response = requests.get(
            f"{DRIVE_API}/files/{quote(file_id.strip(), safe='')}",
            headers=_headers(),
            params={"fields": "id,name,mimeType,size,parents,appProperties,trashed"},
            timeout=20,
        )
    except requests.RequestException as exc:
        raise GoogleDriveStorageError("Could not verify the uploaded file in Google Drive.") from exc
    _raise_for_response(response, "verifying a completed upload")
    try:
        result = response.json()
    except ValueError as exc:
        raise GoogleDriveStorageError("Google Drive returned invalid file metadata.") from exc
    properties = result.get("appProperties") or {}
    try:
        actual_size = int(result.get("size", -1))
    except (TypeError, ValueError):
        actual_size = -1
    if (
        result.get("id") != file_id.strip()
        or result.get("trashed") is True
        or actual_size != int(expected_size)
        or properties.get("quickshareShareId") != share_id
        or properties.get("quickshareUploadNonce") != upload_nonce
        or _ensure_folder() not in (result.get("parents") or [])
    ):
        raise GoogleDriveStorageError(
            "The completed Drive upload did not match its QuickShare upload session."
        )
    return result


def open_private_file_stream(file_id: str, range_header: str = ""):
    """Open an authenticated, streaming download from Drive; the caller must close it."""
    if not file_id or not isinstance(file_id, str):
        raise GoogleDriveStorageError("This share has no Google Drive file ID.")
    headers = _headers()
    if range_header:
        headers["Range"] = range_header
    try:
        response = requests.get(
            f"{DRIVE_API}/files/{quote(file_id, safe='')}",
            headers=headers,
            params={"alt": "media"},
            timeout=(20, 120),
            stream=True,
        )
    except requests.RequestException as exc:
        raise GoogleDriveStorageError("Could not connect to Google Drive to stream the file.") from exc
    if response.status_code not in (200, 206):
        _raise_for_response(response, "opening a streamed download")
    return response


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
