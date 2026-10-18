import base64
import copy
import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from services import github_storage as storage


class FakeResponse:
    def __init__(self, status_code, payload=None, headers=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}
        self.text = text
        self.ok = 200 <= status_code < 300

    def json(self):
        return self._payload


def configure_env(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("GITHUB_OWNER", "test-owner")
    monkeypatch.setenv("GITHUB_REPO", "test-storage")
    monkeypatch.setenv("GITHUB_BRANCH", "main")


def test_upload_file_bytes_sends_base64_to_contents_api(monkeypatch):
    configure_env(monkeypatch)
    seen = {}

    def fake_put(url, headers, json, timeout):
        seen.update(url=url, headers=headers, payload=json, timeout=timeout)
        return FakeResponse(201, {"content": {"path": "files/demo.txt"}})

    monkeypatch.setattr(storage.requests, "put", fake_put)
    result = storage.upload_file_bytes("files/demo.txt", b"hello quickshare", "a" * 32)

    assert result["path"] == "files/demo.txt"
    assert seen["url"].endswith("/contents/files/demo.txt")
    assert base64.b64decode(seen["payload"]["content"]) == b"hello quickshare"
    assert seen["payload"]["branch"] == "main"
    assert seen["headers"]["Authorization"] == "Bearer test-token"


def test_github_rate_limit_is_reported_clearly():
    response = FakeResponse(
        403,
        headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "2000000000"},
        text='{"message":"API rate limit exceeded"}',
    )
    with pytest.raises(storage.GitHubStorageError, match="rate limit"):
        storage._raise_for_response(response, "testing")


def test_only_owner_token_can_revoke_share(monkeypatch):
    owner_token = "owner-token-for-this-test"
    share_id = "b" * 32
    state = {
        "metadata": {
            "share_id": share_id,
            "owner_token_hash": hashlib.sha256(owner_token.encode()).hexdigest(),
            "revoked": False,
            "expiry_time": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "max_downloads": 3,
            "download_count": 0,
            "downloads": [],
        },
        "sha": "blob-1",
    }
    lock = threading.Lock()

    def fake_get(_path):
        with lock:
            return copy.deepcopy(state["metadata"]), state["sha"]

    def fake_put(_path, content, _message, sha=None):
        with lock:
            if sha != state["sha"]:
                raise storage.GitHubConflictError("stale blob")
            state["metadata"] = json.loads(content.decode("utf-8"))
            state["sha"] = "blob-" + str(int(state["sha"].split("-")[1]) + 1)
            return {}

    monkeypatch.setattr(storage, "_get_json_file", fake_get)
    monkeypatch.setattr(storage, "_put_contents", fake_put)

    assert storage.revoke_share(share_id, "wrong-token") == "unauthorized"
    assert state["metadata"]["revoked"] is False
    assert storage.revoke_share(share_id, owner_token) == "success"
    assert state["metadata"]["revoked"] is True
    assert storage.share_status(state["metadata"]) == "revoked"


def test_concurrent_downloads_never_exceed_limit(monkeypatch):
    share_id = "c" * 32
    limit = 3
    state = {
        "metadata": {
            "share_id": share_id,
            "owner_token_hash": "unused",
            "revoked": False,
            "expiry_time": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "max_downloads": limit,
            "download_count": 0,
            "downloads": [],
        },
        "sha": "blob-0",
    }
    lock = threading.Lock()

    def fake_get(_path):
        with lock:
            return copy.deepcopy(state["metadata"]), state["sha"]

    def fake_put(_path, content, _message, sha=None):
        with lock:
            if sha != state["sha"]:
                raise storage.GitHubConflictError("stale blob")
            state["metadata"] = json.loads(content.decode("utf-8"))
            version = int(state["sha"].split("-")[1]) + 1
            state["sha"] = f"blob-{version}"
            return {}

    monkeypatch.setattr(storage, "_get_json_file", fake_get)
    monkeypatch.setattr(storage, "_put_contents", fake_put)

    with ThreadPoolExecutor(max_workers=12) as executor:
        results = list(executor.map(lambda _i: storage.record_download(share_id), range(12)))

    assert results.count("success") == limit
    assert state["metadata"]["download_count"] == limit
    assert set(results) <= {"success", "limit", "conflict"}


def test_cleanup_deletes_expired_current_revision_and_marks_metadata(monkeypatch):
    share_id = "d" * 32
    metadata = {
        "share_id": share_id,
        "storage_path": f"files/{share_id}.txt",
        "revoked": False,
        "expiry_time": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
        "download_count": 0,
        "max_downloads": 3,
        "downloads": [],
        "cleanup_status": "pending",
    }
    current = copy.deepcopy(metadata)

    monkeypatch.setattr(storage, "_list_metadata_names", lambda: [share_id])
    monkeypatch.setattr(storage, "_get_cleanup_cursor", lambda: "")
    monkeypatch.setattr(storage, "_set_cleanup_cursor", lambda _sid: None)
    monkeypatch.setattr(storage, "get_share_metadata", lambda _sid: copy.deepcopy(current))
    monkeypatch.setattr(storage, "delete_storage_file", lambda _path: "deleted")

    def fake_write(_sid, mutate, _message, attempts=3):
        outcome = mutate(current)
        return ("success", current) if outcome == "write" else (outcome, current)

    monkeypatch.setattr(storage, "_write_metadata", fake_write)
    summary = storage.cleanup_expired_files(max_items=20)

    assert summary["deleted"] == 1
    assert current["cleanup_status"] == "deleted"
    assert current["cleanup_at"]
