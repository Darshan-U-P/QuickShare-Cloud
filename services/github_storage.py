import base64
import hmac
import json
import os
import time
from datetime import datetime, timezone
from urllib.parse import quote

import requests

from services.storage_errors import StorageError


class GitHubStorageError(StorageError):
    """Safe, user-facing error raised for GitHub storage problems."""


class GitHubConflictError(GitHubStorageError):
    """The file changed during a metadata update; callers may retry."""


def _settings():
    token = os.getenv("GITHUB_TOKEN", "").strip()
    owner = os.getenv("GITHUB_OWNER", "").strip()
    repo = os.getenv("GITHUB_REPO", "").strip()
    branch = os.getenv("GITHUB_BRANCH", "main").strip() or "main"
    if not token:
        raise GitHubStorageError("GitHub storage is not configured: missing GITHUB_TOKEN.")
    if not owner or not repo:
        raise GitHubStorageError("Set GITHUB_OWNER and GITHUB_REPO in the environment variables.")
    return token, owner, repo, branch


def _headers(token):
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _endpoint(owner, repo, storage_path):
    encoded_path = quote(storage_path, safe="/")
    return f"https://api.github.com/repos/{owner}/{repo}/contents/{encoded_path}"


def _raise_for_response(response, operation, *, sha_update=False):
    status = response.status_code
    body_text = (getattr(response, "text", "") or "").lower()
    headers = getattr(response, "headers", {}) or {}
    remaining = headers.get("X-RateLimit-Remaining") or headers.get("x-ratelimit-remaining")
    if status == 429 or (status == 403 and (str(remaining) == "0" or "rate limit" in body_text)):
        reset = headers.get("X-RateLimit-Reset") or headers.get("x-ratelimit-reset")
        retry_text = ""
        if reset:
            try:
                seconds = max(1, int(reset) - int(time.time()))
                retry_text = f" Try again in about {seconds} seconds."
            except (TypeError, ValueError):
                pass
        raise GitHubStorageError("GitHub API rate limit reached." + retry_text)
    if status in (409, 422) and sha_update:
        raise GitHubConflictError("The share metadata changed during an update. Please retry.")
    if status in (401, 403):
        raise GitHubStorageError("GitHub rejected the token or permissions. Verify the token's repository access and Contents: Read and write permission.")
    if status == 404:
        raise GitHubStorageError("GitHub could not find the configured repository, branch, or file.")
    if not response.ok:
        raise GitHubStorageError(f"GitHub returned HTTP {status} during {operation}.")


def _put_contents(storage_path: str, content: bytes, message: str, sha: str | None = None) -> dict:
    token, owner, repo, branch = _settings()
    payload = {
        "message": message,
        "content": base64.b64encode(content).decode("ascii"),
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha
    try:
        response = requests.put(
            _endpoint(owner, repo, storage_path),
            headers=_headers(token),
            json=payload,
            timeout=30,
        )
    except requests.RequestException as exc:
        raise GitHubStorageError("Could not connect to GitHub. Check the network connection and try again.") from exc
    _raise_for_response(response, "uploading", sha_update=bool(sha))
    try:
        return response.json()
    except ValueError as exc:
        raise GitHubStorageError("GitHub returned an unreadable upload response.") from exc


def upload_file_bytes(storage_path: str, content: bytes, share_id: str) -> dict:
    """Commit a new demonstration file into the configured public storage repository."""
    response = _put_contents(storage_path, content, f"Add QuickShare demo file {share_id}")
    return response.get("content", {})


def _get_json_file(storage_path: str) -> tuple[dict, str] | None:
    token, owner, repo, branch = _settings()
    try:
        response = requests.get(
            _endpoint(owner, repo, storage_path),
            headers=_headers(token),
            params={"ref": branch},
            timeout=20,
        )
    except requests.RequestException as exc:
        raise GitHubStorageError("Could not connect to GitHub to read share metadata.") from exc

    if response.status_code == 404:
        return None
    _raise_for_response(response, "reading metadata")
    try:
        item = response.json()
        raw_content = base64.b64decode(item["content"]).decode("utf-8")
        metadata = json.loads(raw_content)
        sha = item["sha"]
        if not isinstance(metadata, dict):
            raise ValueError("metadata is not a JSON object")
        return metadata, sha
    except (ValueError, KeyError, TypeError) as exc:
        raise GitHubStorageError("The saved share metadata is invalid or unreadable.") from exc


def _write_metadata(share_id: str, mutate, message: str, attempts: int = 3):
    path = f"metadata/{share_id}.json"
    for _attempt in range(attempts):
        found = _get_json_file(path)
        if not found:
            return "not_found", None
        metadata, sha = found
        outcome = mutate(metadata)
        if outcome != "write":
            return outcome, metadata
        body = json.dumps(metadata, ensure_ascii=False, indent=2).encode("utf-8")
        try:
            _put_contents(path, body, message, sha=sha)
            return "success", metadata
        except GitHubConflictError:
            continue
    return "conflict", None


def create_share_metadata(metadata: dict) -> None:
    share_id = metadata["share_id"]
    path = f"metadata/{share_id}.json"
    serialized = json.dumps(metadata, ensure_ascii=False, indent=2).encode("utf-8")
    _put_contents(path, serialized, f"Add QuickShare metadata {share_id}")


def get_share_metadata(share_id: str) -> dict | None:
    found = _get_json_file(f"metadata/{share_id}.json")
    return found[0] if found else None


def share_status(metadata: dict, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    if metadata.get("revoked"):
        return "revoked"
    try:
        expires = datetime.fromisoformat(metadata["expiry_time"])
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires <= now:
            return "expired"
        if int(metadata["download_count"]) >= int(metadata["max_downloads"]):
            return "limit"
        return "active"
    except (KeyError, TypeError, ValueError):
        raise GitHubStorageError("The saved share metadata is missing required fields.")


def _valid_owner_id(metadata: dict, owner_id_hash: str) -> bool:
    if not isinstance(owner_id_hash, str) or not owner_id_hash:
        return False
    saved_hash = metadata.get("owner_id_hash", "")
    if not isinstance(saved_hash, str) or not saved_hash:
        return False
    return hmac.compare_digest(saved_hash, owner_id_hash)



def _append_event(metadata: dict, status: str, now: datetime | None = None):
    events = metadata.get("downloads", [])
    if not isinstance(events, list):
        events = []
    events.append({
        "time": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
        "status": status,
    })
    metadata["downloads"] = events[-100:]


def record_download(share_id: str) -> str:
    """Record a download with optimistic SHA updates; appropriate only for a low-traffic demo."""
    def mutate(metadata):
        status = share_status(metadata)
        if status != "active":
            _append_event(metadata, f"rejected_{status}")
            return "write"
        metadata["download_count"] = int(metadata.get("download_count", 0)) + 1
        _append_event(metadata, "success")
        return "write"

    path = f"metadata/{share_id}.json"
    for _attempt in range(3):
        found = _get_json_file(path)
        if not found:
            return "not_found"
        metadata, sha = found
        status = share_status(metadata)
        _append_event(metadata, "success" if status == "active" else f"rejected_{status}")
        if status == "active":
            metadata["download_count"] = int(metadata.get("download_count", 0)) + 1
        body = json.dumps(metadata, ensure_ascii=False, indent=2).encode("utf-8")
        try:
            _put_contents(path, body, f"Record QuickShare download {share_id}", sha=sha)
            return "success" if status == "active" else status
        except GitHubConflictError:
            continue
    return "conflict"


def record_rejected_attempt(share_id: str, status: str) -> None:
    def mutate(metadata):
        _append_event(metadata, f"rejected_{status}")
        return "write"
    _write_metadata(share_id, mutate, f"Record denied QuickShare access {share_id}", attempts=2)


def revoke_share(share_id: str, owner_id_hash: str) -> str:
    def mutate(metadata):
        if not _valid_owner_id(metadata, owner_id_hash):
            return "unauthorized"
        if metadata.get("revoked"):
            return "revoked"
        metadata["revoked"] = True
        metadata["revoked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _append_event(metadata, "revoked")
        return "write"

    result, _metadata = _write_metadata(
        share_id, mutate, f"Revoke QuickShare share {share_id}", attempts=3
    )
    return result


def get_share_analytics(share_id: str, owner_id_hash: str) -> tuple[str, dict | None]:
    metadata = get_share_metadata(share_id)
    if not metadata:
        return "not_found", None
    if not _valid_owner_id(metadata, owner_id_hash):
        return "unauthorized", None
    events = metadata.get("downloads", [])
    if not isinstance(events, list):
        events = []
    safe_events = []
    for event in events[-100:]:
        if isinstance(event, dict):
            safe_events.append({
                "time": str(event.get("time", "")),
                "status": str(event.get("status", "unknown")),
            })
    return "success", {
        "share_id": metadata.get("share_id", share_id),
        "original_filename": metadata.get("original_filename", "Unnamed file"),
        "file_size": int(metadata.get("file_size", 0)),
        "upload_time": metadata.get("upload_time", ""),
        "expiry_time": metadata.get("expiry_time", ""),
        "download_count": int(metadata.get("download_count", 0)),
        "max_downloads": int(metadata.get("max_downloads", 0)),
        "status": share_status(metadata),
        "revoked_at": metadata.get("revoked_at"),
        "cleanup_status": metadata.get("cleanup_status", "pending"),
        "storage_provider": metadata.get("storage_provider", "github"),
        "downloads": safe_events,
    }


def list_user_shares(owner_id_hash: str) -> list[dict]:
    """Return metadata for shares owned by one authenticated account."""
    if not isinstance(owner_id_hash, str) or not owner_id_hash:
        return []
    shares = []
    for share_id in _list_metadata_names():
        metadata = get_share_metadata(share_id)
        if not metadata or not _valid_owner_id(metadata, owner_id_hash):
            continue
        events = metadata.get("downloads", [])
        if not isinstance(events, list):
            events = []
        safe_events = [
            {"time": str(event.get("time", "")), "status": str(event.get("status", "unknown"))}
            for event in events[-100:]
            if isinstance(event, dict)
        ]
        shares.append({
            "share_id": metadata.get("share_id", share_id),
            "original_filename": metadata.get("original_filename", "Unnamed file"),
            "file_size": int(metadata.get("file_size", 0)),
            "upload_time": metadata.get("upload_time", ""),
            "expiry_time": metadata.get("expiry_time", ""),
            "max_downloads": int(metadata.get("max_downloads", 0)),
            "download_count": int(metadata.get("download_count", 0)),
            "status": share_status(metadata),
            "revoked_at": metadata.get("revoked_at"),
            "cleanup_status": metadata.get("cleanup_status", "pending"),
            "storage_provider": metadata.get("storage_provider", "github"),
            "downloads": safe_events,
        })
    shares.sort(key=lambda row: row.get("upload_time", ""), reverse=True)
    return shares[:200]

def _list_metadata_names() -> list[str]:
    token, owner, repo, branch = _settings()
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/metadata"
    try:
        response = requests.get(
            url,
            headers=_headers(token),
            params={"ref": branch},
            timeout=20,
        )
    except requests.RequestException as exc:
        raise GitHubStorageError("Could not connect to GitHub to list metadata for cleanup.") from exc
    if response.status_code == 404:
        return []
    _raise_for_response(response, "listing metadata")
    try:
        items = response.json()
        if not isinstance(items, list):
            return []
        return [
            item["name"][:-5]
            for item in items
            if item.get("type") == "file"
            and item.get("name", "").endswith(".json")
            and item.get("name", "")[:-5].isalnum()
        ]
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise GitHubStorageError("GitHub returned an invalid metadata directory listing.") from exc


def delete_storage_file(storage_path: str) -> str:
    """Remove a file from the current branch; old Git history is not erased."""
    token, owner, repo, branch = _settings()
    endpoint = _endpoint(owner, repo, storage_path)
    try:
        get_response = requests.get(
            endpoint,
            headers=_headers(token),
            params={"ref": branch},
            timeout=20,
        )
    except requests.RequestException as exc:
        raise GitHubStorageError("Could not connect to GitHub to inspect the expired file.") from exc
    if get_response.status_code == 404:
        return "already_missing"
    _raise_for_response(get_response, "checking a file before deletion")
    try:
        blob_sha = get_response.json()["sha"]
    except (ValueError, KeyError, TypeError) as exc:
        raise GitHubStorageError("GitHub did not return a file SHA for cleanup.") from exc

    payload = {
        "message": f"QuickShare cleanup: remove {storage_path}",
        "sha": blob_sha,
        "branch": branch,
    }
    try:
        delete_response = requests.delete(endpoint, headers=_headers(token), json=payload, timeout=30)
    except requests.RequestException as exc:
        raise GitHubStorageError("Could not connect to GitHub to delete the expired file.") from exc
    if delete_response.status_code == 404:
        return "already_missing"
    _raise_for_response(delete_response, "deleting an expired file", sha_update=True)
    return "deleted"


def _get_cleanup_cursor() -> str:
    found = _get_json_file("metadata/.cleanup_cursor.json")
    if not found:
        return ""
    value = found[0].get("last_share_id", "")
    return value if isinstance(value, str) else ""


def _set_cleanup_cursor(share_id: str) -> None:
    path = "metadata/.cleanup_cursor.json"
    value = {
        "last_share_id": share_id,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    body = json.dumps(value, indent=2).encode("utf-8")
    for _attempt in range(3):
        found = _get_json_file(path)
        try:
            _put_contents(
                path,
                body,
                "Update QuickShare cleanup cursor",
                sha=found[1] if found else None,
            )
            return
        except GitHubConflictError:
            continue
    raise GitHubStorageError("Could not update the cleanup cursor after repeated conflicts.")


def cleanup_expired_files(max_items: int = 20, max_scan: int = 100) -> dict:
    """Delete current-branch file blobs for expired/revoked shares and retain status metadata.

    A saved cursor keeps daily runs moving across the metadata directory without
    reading every share on every invocation. At most max_scan records are checked
    and at most max_items eligible records are cleaned per run.
    """
    names = _list_metadata_names()
    summary = {
        "scanned": 0, "deleted": 0, "already_missing": 0,
        "failed": 0, "skipped": 0, "items": [],
    }
    if not names:
        summary["remaining_metadata"] = 0
        return summary

    limit = max(1, min(100, int(max_items)))
    scan_limit = max(1, min(200, int(max_scan)))
    try:
        cursor = _get_cleanup_cursor()
    except GitHubStorageError as exc:
        summary["cursor_warning"] = str(exc)
        cursor = ""
    if cursor in names:
        cursor_index = names.index(cursor)
        ordered_names = names[cursor_index + 1:] + names[:cursor_index + 1]
    else:
        ordered_names = names
    scan_names = ordered_names[:scan_limit]
    cleanup_attempts = 0

    for share_id in scan_names:
        if cleanup_attempts >= limit:
            break
        summary["scanned"] += 1
        try:
            metadata = get_share_metadata(share_id)
            if not metadata:
                summary["skipped"] += 1
                continue
            status = share_status(metadata)
            if status not in {"expired", "revoked"}:
                summary["skipped"] += 1
                continue
            if metadata.get("cleanup_status") in {"deleted", "already_missing"}:
                summary["skipped"] += 1
                continue

            cleanup_attempts += 1
            if metadata.get("storage_provider", "github") == "google_drive":
                from services.google_drive_storage import delete_private_file
                deletion_result = delete_private_file(metadata.get("drive_file_id", ""))
            else:
                deletion_result = delete_storage_file(metadata.get("storage_path", ""))
            cleanup_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

            def mutate(current, result=deletion_result, stamp=cleanup_at):
                # Recheck state after file deletion, then persist the cleanup receipt.
                state = share_status(current)
                if state not in {"expired", "revoked"}:
                    return "skip"
                current["cleanup_status"] = "deleted" if result == "deleted" else "already_missing"
                current["cleanup_at"] = stamp
                _append_event(current, f"cleanup_{current['cleanup_status']}")
                return "write"

            result, _ = _write_metadata(
                share_id, mutate, f"Record QuickShare cleanup {share_id}", attempts=3
            )
            if result == "success":
                key = "deleted" if deletion_result == "deleted" else "already_missing"
                summary[key] += 1
                summary["items"].append({"share_id": share_id, "status": key})
            else:
                summary["failed"] += 1
                summary["items"].append({"share_id": share_id, "status": result})
        except StorageError as exc:
            summary["failed"] += 1
            summary["items"].append({"share_id": share_id, "status": "failed", "message": str(exc)})

    if summary["scanned"]:
        try:
            _set_cleanup_cursor(scan_names[summary["scanned"] - 1])
        except GitHubStorageError as exc:
            summary["cursor_warning"] = str(exc)
    summary["remaining_metadata"] = max(0, len(names) - summary["scanned"])
    return summary

def download_public_file(storage_path: str) -> bytes:
    """Download a known file from the configured public repository."""
    _token, owner, repo, branch = _settings()
    safe_branch = quote(branch, safe="")
    safe_path = quote(storage_path, safe="/")
    url = f"https://raw.githubusercontent.com/{owner}/{repo}/{safe_branch}/{safe_path}"
    try:
        response = requests.get(url, timeout=30)
    except requests.RequestException as exc:
        raise GitHubStorageError("Could not connect to GitHub to retrieve the file.") from exc
    if response.status_code == 404:
        raise GitHubStorageError("The file was not found in GitHub storage.")
    if not response.ok:
        body_text = (getattr(response, "text", "") or "").lower()
        if response.status_code == 429 or (response.status_code == 403 and "rate limit" in body_text):
            raise GitHubStorageError("GitHub raw download rate limit reached. Retry later.")
        raise GitHubStorageError(f"GitHub storage returned HTTP {response.status_code} while downloading.")
    return response.content
