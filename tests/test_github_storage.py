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
    owner_id_hash = "google-subject-hash-for-this-test"
    share_id = "b" * 32
    state = {
        "metadata": {
            "share_id": share_id,
            "owner_id_hash": owner_id_hash,
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

    assert storage.revoke_share(share_id, "other-account-hash") == "unauthorized"
    assert state["metadata"]["revoked"] is False
    assert storage.revoke_share(share_id, owner_id_hash) == "success"
    assert state["metadata"]["revoked"] is True
    assert storage.share_status(state["metadata"]) == "revoked"


def test_concurrent_downloads_never_exceed_limit(monkeypatch):
    share_id = "c" * 32
    limit = 3
    state = {
        "metadata": {
            "share_id": share_id,
            "owner_id_hash": "unused",
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



def test_list_user_shares_only_returns_matching_account(monkeypatch):
    target_hash = "account-one-hash"
    records = {
        "1" * 32: {
            "share_id": "1" * 32,
            "owner_id_hash": target_hash,
            "original_filename": "mine.pdf",
            "storage_path": "files/mine.pdf",
            "upload_time": "2026-10-18T10:00:00+00:00",
            "expiry_time": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "file_size": 1234,
            "max_downloads": 3,
            "download_count": 1,
            "downloads": [{"time": "2026-10-18T10:01:00+00:00", "status": "success"}],
            "revoked": False,
        },
        "2" * 32: {
            "share_id": "2" * 32,
            "owner_id_hash": "account-two-hash",
            "original_filename": "someone-elses.pdf",
            "storage_path": "files/other.pdf",
            "upload_time": "2026-10-18T11:00:00+00:00",
            "expiry_time": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "file_size": 987,
            "max_downloads": 3,
            "download_count": 0,
            "downloads": [],
            "revoked": False,
        },
    }
    monkeypatch.setattr(storage, "_list_metadata_names", lambda: list(records))
    monkeypatch.setattr(storage, "get_share_metadata", lambda share_id: records.get(share_id))
    shares = storage.list_user_shares(target_hash)
    assert len(shares) == 1
    assert shares[0]["original_filename"] == "mine.pdf"
    assert "owner_id_hash" not in shares[0]



def test_cleanup_dispatches_private_drive_files_to_drive_deletion(monkeypatch):
    share_id = "e" * 32
    current = {
        "share_id": share_id,
        "storage_provider": "google_drive",
        "drive_file_id": "drive-private-id",
        "storage_path": f"files/{share_id}.txt",
        "revoked": False,
        "expiry_time": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
        "download_count": 0,
        "max_downloads": 3,
        "downloads": [],
        "cleanup_status": "pending",
    }
    monkeypatch.setattr(storage, "_list_metadata_names", lambda: [share_id])
    monkeypatch.setattr(storage, "_get_cleanup_cursor", lambda: "")
    monkeypatch.setattr(storage, "_set_cleanup_cursor", lambda _sid: None)
    monkeypatch.setattr(storage, "get_share_metadata", lambda _sid: copy.deepcopy(current))
    monkeypatch.setattr(
        storage,
        "delete_storage_file",
        lambda _path: (_ for _ in ()).throw(AssertionError("GitHub file deletion must not be used for Drive shares")),
    )
    drive_deleted = []
    from services import google_drive_storage
    monkeypatch.setattr(google_drive_storage, "delete_private_file", lambda file_id: drive_deleted.append(file_id) or "deleted")

    def fake_write(_sid, mutate, _message, attempts=3):
        outcome = mutate(current)
        return ("success", current) if outcome == "write" else (outcome, current)

    monkeypatch.setattr(storage, "_write_metadata", fake_write)
    summary = storage.cleanup_expired_files(max_items=20)

    assert drive_deleted == ["drive-private-id"]
    assert summary["deleted"] == 1
    assert current["cleanup_status"] == "deleted"
