import base64
import json
import os
from datetime import datetime, timezone
from urllib.parse import quote

import requests


class GitHubStorageError(Exception):
    """Safe, user-facing error raised for GitHub storage problems."""


class GitHubConflictError(GitHubStorageError):
    """The file changed during a metadata update; callers may retry."""


def _settings():
    token = os.getenv("GITHUB_TOKEN", "").strip()
    owner = os.getenv("GITHUB_OWNER", "").strip()
    repo = os.getenv("GITHUB_REPO", "").strip()
    branch = os.getenv("GITHUB_BRANCH", "main").strip() or "main"
    if not token:
        raise GitHubStorageError("GITHUB_TOKEN is missing from the deployment environment variables.")
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


def _raise_for_response(response, operation):
    if response.status_code in (409, 422):
        raise GitHubConflictError("GitHub reported a file update conflict. Please retry.")
    if response.status_code in (401, 403):
        raise GitHubStorageError("GitHub rejected the token or permissions. Verify repository access and Contents: Read and write.")
    if response.status_code == 404:
        raise GitHubStorageError("GitHub could not find the configured repository, branch, or file.")
    if not response.ok:
        raise GitHubStorageError(f"GitHub returned HTTP {response.status_code} during {operation}.")


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
    _raise_for_response(response, "uploading")
    try:
        return response.json()
    except ValueError as exc:
        raise GitHubStorageError("GitHub returned an unreadable upload response.") from exc


def upload_file_bytes(storage_path: str, content: bytes, share_id: str) -> dict:
    """Commit a new demonstration file into the configured public storage repository."""
    response = _put_contents(
        storage_path,
        content,
        f"Add QuickShare demo file {share_id}",
    )
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


def create_share_metadata(metadata: dict) -> None:
    share_id = metadata["share_id"]
    path = f"metadata/{share_id}.json"
    serialized = json.dumps(metadata, ensure_ascii=False, indent=2).encode("utf-8")
    _put_contents(path, serialized, f"Add QuickShare metadata {share_id}")


def get_share_metadata(share_id: str) -> dict | None:
    found = _get_json_file(f"metadata/{share_id}.json")
    return found[0] if found else None


def _status(metadata: dict, now: datetime) -> str:
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


def record_download(share_id: str) -> str:
    """Atomically-ish update metadata using GitHub's blob SHA, retrying stale updates.

    GitHub's Contents API is not a transactional database. SHA conflict retries
    make this suitable for a low-traffic college prototype, but not production use.
    """
    path = f"metadata/{share_id}.json"
    for _attempt in range(3):
        found = _get_json_file(path)
        if not found:
            return "not_found"
        metadata, sha = found
        now = datetime.now(timezone.utc)
        status = _status(metadata, now)
        if status != "active":
            return status

        metadata["download_count"] = int(metadata.get("download_count", 0)) + 1
        events = metadata.get("downloads", [])
        if not isinstance(events, list):
            events = []
        events.append({"time": now.isoformat(timespec="seconds"), "status": "success"})
        metadata["downloads"] = events[-50:]
        body = json.dumps(metadata, ensure_ascii=False, indent=2).encode("utf-8")
        try:
            _put_contents(path, body, f"Record QuickShare download {share_id}", sha=sha)
            return "success"
        except GitHubConflictError:
            continue
    return "conflict"


def record_rejected_attempt(share_id: str, status: str) -> None:
    """Best-effort audit logging for a denied download; limited to recent events."""
    path = f"metadata/{share_id}.json"
    for _attempt in range(2):
        found = _get_json_file(path)
        if not found:
            return
        metadata, sha = found
        events = metadata.get("downloads", [])
        if not isinstance(events, list):
            events = []
        events.append({
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "status": f"rejected_{status}",
        })
        metadata["downloads"] = events[-50:]
        body = json.dumps(metadata, ensure_ascii=False, indent=2).encode("utf-8")
        try:
            _put_contents(path, body, f"Record denied QuickShare access {share_id}", sha=sha)
            return
        except GitHubConflictError:
            continue


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
        raise GitHubStorageError(f"GitHub storage returned HTTP {response.status_code} while downloading.")
    return response.content
