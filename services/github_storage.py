import base64
import os
from urllib.parse import quote

import requests


class GitHubStorageError(Exception):
    """Safe, user-facing error raised for GitHub storage problems."""


def _settings():
    token = os.getenv("GITHUB_TOKEN", "").strip()
    owner = os.getenv("GITHUB_OWNER", "").strip()
    repo = os.getenv("GITHUB_REPO", "").strip()
    branch = os.getenv("GITHUB_BRANCH", "main").strip() or "main"
    if not token:
        raise GitHubStorageError("GITHUB_TOKEN is missing from your local .env file.")
    if not owner or not repo:
        raise GitHubStorageError("Set GITHUB_OWNER and GITHUB_REPO in your local .env file.")
    return token, owner, repo, branch


def _headers(token):
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def upload_file_bytes(storage_path: str, content: bytes, share_id: str) -> dict:
    """Create a new file in the configured GitHub storage repository."""
    token, owner, repo, branch = _settings()
    encoded_path = quote(storage_path, safe="/")
    endpoint = f"https://api.github.com/repos/{owner}/{repo}/contents/{encoded_path}"
    payload = {
        "message": f"Add QuickShare demo file {share_id}",
        "content": base64.b64encode(content).decode("ascii"),
        "branch": branch,
    }
    try:
        response = requests.put(endpoint, headers=_headers(token), json=payload, timeout=30)
    except requests.RequestException as exc:
        raise GitHubStorageError("Could not connect to GitHub. Check your internet connection and try again.") from exc

    if response.status_code in (401, 403):
        raise GitHubStorageError("GitHub rejected the token or write permission. Check the token's repository access and Contents: Read and write permission.")
    if response.status_code == 404:
        raise GitHubStorageError("The storage repository or configured branch was not found. Check GITHUB_OWNER, GITHUB_REPO, and GITHUB_BRANCH.")
    if response.status_code == 409:
        raise GitHubStorageError("A storage conflict occurred. Please retry the upload.")
    if response.status_code == 422:
        raise GitHubStorageError("GitHub could not create this file. Check repository settings and applicable API/file limits.")
    if not response.ok:
        raise GitHubStorageError(f"GitHub returned HTTP {response.status_code} while uploading the file.")

    try:
        return response.json()["content"]
    except (ValueError, KeyError, TypeError) as exc:
        raise GitHubStorageError("GitHub accepted a response that could not be interpreted.") from exc


def download_public_file(storage_path: str) -> bytes:
    """Download a known file from the public repository's raw-content endpoint."""
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
