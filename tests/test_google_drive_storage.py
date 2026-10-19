from services import google_drive_storage as drive


class FakeResponse:
    def __init__(self, status_code=200, payload=None, content=b"", text="", headers=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.content = content
        self.text = text
        self.headers = headers or {}
        self.ok = 200 <= status_code < 300

    def json(self):
        return self._payload


def test_drive_upload_creates_private_file_in_storage_folder(monkeypatch):
    monkeypatch.setattr(drive, "_access_token", lambda: "drive-access-token")
    monkeypatch.setattr(drive, "_ensure_folder", lambda: "private-folder-id")
    seen = {}

    def fake_post(url, headers, params, data, timeout):
        seen.update(url=url, headers=headers, params=params, data=data, timeout=timeout)
        return FakeResponse(200, {"id": "drive-file-123", "name": "files/demo.txt"})

    monkeypatch.setattr(drive.requests, "post", fake_post)
    result = drive.upload_file_bytes("files/demo.txt", b"private content", "a" * 32)

    assert result["id"] == "drive-file-123"
    assert seen["url"].endswith("/upload/drive/v3/files")
    assert seen["headers"]["Authorization"] == "Bearer drive-access-token"
    assert seen["headers"]["Content-Type"].startswith("multipart/related; boundary=")
    assert seen["params"]["uploadType"] == "multipart"
    assert b"private-folder-id" in seen["data"]
    assert b"private content" in seen["data"]
    assert b'"quickshareShareId":"' + b"a" * 32 + b'"' in seen["data"]


def test_drive_download_returns_file_bytes(monkeypatch):
    monkeypatch.setattr(drive, "_access_token", lambda: "drive-access-token")
    seen = {}

    def fake_get(url, headers, params, timeout):
        seen.update(url=url, headers=headers, params=params, timeout=timeout)
        return FakeResponse(200, content=b"downloaded private bytes")

    monkeypatch.setattr(drive.requests, "get", fake_get)
    content = drive.download_private_file("private-id")
    assert content == b"downloaded private bytes"
    assert seen["url"].endswith("/files/private-id")
    assert seen["params"] == {"alt": "media"}


def test_drive_delete_handles_missing_file(monkeypatch):
    monkeypatch.setattr(drive, "_access_token", lambda: "drive-access-token")
    monkeypatch.setattr(drive.requests, "delete", lambda *args, **kwargs: FakeResponse(404))
    assert drive.delete_private_file("missing-id") == "already_missing"


def test_drive_provider_requires_refresh_token(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("GOOGLE_DRIVE_REFRESH_TOKEN", raising=False)
    assert drive.is_configured() is False



def test_initiate_resumable_upload_returns_google_session_uri(monkeypatch):
    monkeypatch.setattr(drive, "_access_token", lambda: "drive-access-token")
    monkeypatch.setattr(drive, "_ensure_folder", lambda: "private-folder-id")
    seen = {}

    def fake_post(url, headers, params, json, timeout):
        seen.update(url=url, headers=headers, params=params, json=json, timeout=timeout)
        return FakeResponse(
            200,
            headers={"Location": "https://www.googleapis.com/upload/drive/v3/files?upload_id=session-123"},
        )

    monkeypatch.setattr(drive.requests, "post", fake_post)
    session_url = drive.initiate_resumable_upload(
        "files/share.mp4", "a" * 32, "nonce-123", 7 * 1024**3, "video/mp4"
    )

    assert session_url.endswith("upload_id=session-123")
    assert seen["params"] == {"uploadType": "resumable"}
    assert seen["headers"]["X-Upload-Content-Length"] == str(7 * 1024**3)
    assert seen["json"]["parents"] == ["private-folder-id"]
    assert seen["json"]["appProperties"]["quickshareShareId"] == "a" * 32
    assert seen["json"]["appProperties"]["quickshareUploadNonce"] == "nonce-123"


def test_verify_resumable_upload_checks_size_and_upload_nonce(monkeypatch):
    monkeypatch.setattr(drive, "_access_token", lambda: "drive-access-token")
    monkeypatch.setattr(drive, "_ensure_folder", lambda: "private-folder-id")

    def fake_get(url, headers, params, timeout):
        assert url.endswith("/files/drive-file-123")
        assert params["fields"].find("appProperties") >= 0
        return FakeResponse(200, {
            "id": "drive-file-123",
            "size": "1024",
            "parents": ["private-folder-id"],
            "appProperties": {
                "quickshareShareId": "b" * 32,
                "quickshareUploadNonce": "nonce-xyz",
            },
        })

    monkeypatch.setattr(drive.requests, "get", fake_get)
    result = drive.verify_resumable_upload(
        "drive-file-123",
        share_id="b" * 32,
        upload_nonce="nonce-xyz",
        expected_size=1024,
    )
    assert result["id"] == "drive-file-123"


def test_verify_resumable_upload_rejects_wrong_size(monkeypatch):
    monkeypatch.setattr(drive, "_access_token", lambda: "drive-access-token")
    monkeypatch.setattr(drive, "_ensure_folder", lambda: "private-folder-id")
    monkeypatch.setattr(drive.requests, "get", lambda *args, **kwargs: FakeResponse(
        200,
        {
            "id": "drive-file-123",
            "size": "999",
            "parents": ["private-folder-id"],
            "appProperties": {
                "quickshareShareId": "c" * 32,
                "quickshareUploadNonce": "nonce-xyz",
            },
        },
    ))
    from services.storage_errors import StorageError
    import pytest

    with pytest.raises(StorageError):
        drive.verify_resumable_upload(
            "drive-file-123",
            share_id="c" * 32,
            upload_nonce="nonce-xyz",
            expected_size=1024,
        )



def test_resumable_chunk_forwards_small_range_and_returns_next_offset(monkeypatch):
    seen = {}

    def fake_put(url, headers, data, timeout):
        seen.update(url=url, headers=headers, data=data, timeout=timeout)
        return FakeResponse(308, headers={"Range": "bytes=0-3"})

    monkeypatch.setattr(drive.requests, "put", fake_put)
    result = drive.upload_resumable_chunk(
        "https://www.googleapis.com/upload/drive/v3/files?upload_id=session",
        start=0,
        end=3,
        total_size=100,
        content=b"abcd",
    )
    assert result == {"complete": False, "next_offset": 4}
    assert seen["url"].endswith("upload_id=session")
    assert seen["headers"]["Content-Range"] == "bytes 0-3/100"
    assert seen["headers"]["Content-Length"] == "4"
    assert seen["data"] == b"abcd"


def test_resumable_chunk_returns_completed_file(monkeypatch):
    monkeypatch.setattr(
        drive.requests,
        "put",
        lambda *_args, **_kwargs: FakeResponse(201, {"id": "completed-drive-file"}),
    )
    result = drive.upload_resumable_chunk(
        "https://www.googleapis.com/upload/drive/v3/files?upload_id=session",
        start=4,
        end=7,
        total_size=8,
        content=b"efgh",
    )
    assert result["complete"] is True
    assert result["next_offset"] == 8
    assert result["file"]["id"] == "completed-drive-file"


def test_resumable_chunk_recovers_when_previous_chunk_was_already_committed(monkeypatch):
    calls = []

    def fake_put(url, headers, data, timeout):
        calls.append(headers.copy())
        if len(calls) == 1:
            return FakeResponse(400, {"error": "invalidRange"}, text="invalidRange")
        return FakeResponse(308, headers={"Range": "bytes=0-7"})

    monkeypatch.setattr(drive.requests, "put", fake_put)
    result = drive.upload_resumable_chunk(
        "https://www.googleapis.com/upload/drive/v3/files?upload_id=session",
        start=0,
        end=3,
        total_size=100,
        content=b"abcd",
    )
    assert result == {"complete": False, "next_offset": 8}
    assert len(calls) == 2
    assert calls[1]["Content-Range"] == "bytes */100"



def test_private_large_upload_allowlist_defaults_to_empty(monkeypatch):
    monkeypatch.setattr(drive, "_ensure_folder", lambda: "private-folder-id")
    monkeypatch.setattr(drive, "_list_files", lambda *args, **kwargs: [])
    assert drive.get_large_upload_allowlist() == []


def test_private_large_upload_allowlist_reads_and_normalizes_drive_config(monkeypatch):
    import json

    monkeypatch.setattr(drive, "_ensure_folder", lambda: "private-folder-id")
    monkeypatch.setattr(
        drive,
        "_list_files",
        lambda *args, **kwargs: [{"id": "private-config-id", "name": drive.LARGE_UPLOAD_CONFIG_NAME}],
    )
    monkeypatch.setattr(drive, "_headers", lambda: {"Authorization": "Bearer test"})
    monkeypatch.setattr(
        drive.requests,
        "get",
        lambda *args, **kwargs: FakeResponse(
            200,
            content=json.dumps({
                "version": 1,
                "approved_emails": [" USER@Example.com ", "bad-entry", "user@example.com"],
            }).encode("utf-8"),
        ),
    )
    assert drive.get_large_upload_allowlist() == ["user@example.com"]


def test_private_large_upload_allowlist_creates_config_in_drive_folder(monkeypatch):
    import json

    monkeypatch.setattr(drive, "_ensure_folder", lambda: "private-folder-id")
    monkeypatch.setattr(drive, "_list_files", lambda *args, **kwargs: [])
    monkeypatch.setattr(drive, "_headers", lambda: {"Authorization": "Bearer test"})
    seen = {}

    def fake_post(url, headers, params, data, timeout):
        seen.update(url=url, headers=headers, params=params, data=data, timeout=timeout)
        return FakeResponse(200, {"id": "private-config-id"})

    monkeypatch.setattr(drive.requests, "post", fake_post)
    saved = drive.set_large_upload_allowlist([" User@Example.com ", "user@example.com", "Other@example.com"])
    assert saved == ["other@example.com", "user@example.com"]
    assert seen["params"]["uploadType"] == "multipart"
    assert "private-folder-id" in seen["data"].decode("utf-8")
    assert "quickshareConfigKey" in seen["data"].decode("utf-8")
    assert "user@example.com" in seen["data"].decode("utf-8")
    assert json.loads(seen["data"].decode("utf-8").split("Content-Type: application/json; charset=UTF-8\r\n\r\n")[-1].split("\r\n--")[0])["approved_emails"] == ["other@example.com", "user@example.com"]


def test_private_large_upload_allowlist_updates_existing_config(monkeypatch):
    import json

    monkeypatch.setattr(
        drive,
        "_large_upload_config_file",
        lambda: {"id": "private-config-id", "name": drive.LARGE_UPLOAD_CONFIG_NAME},
    )
    monkeypatch.setattr(drive, "_headers", lambda: {"Authorization": "Bearer test"})
    seen = {}

    def fake_patch(url, headers, params, data, timeout):
        seen.update(url=url, headers=headers, params=params, data=data, timeout=timeout)
        return FakeResponse(200, {"id": "private-config-id"})

    monkeypatch.setattr(drive.requests, "patch", fake_patch)
    saved = drive.set_large_upload_allowlist(["UPLOADED@example.com"])
    assert saved == ["uploaded@example.com"]
    assert seen["url"].endswith("/files/private-config-id")
    assert seen["params"] == {"uploadType": "media"}
    assert json.loads(seen["data"].decode("utf-8"))["approved_emails"] == ["uploaded@example.com"]


def test_private_large_upload_allowlist_rejects_invalid_email(monkeypatch):
    import pytest
    from services.storage_errors import StorageError

    monkeypatch.setattr(drive, "_large_upload_config_file", lambda: None)
    with pytest.raises(StorageError):
        drive.set_large_upload_allowlist(["not-an-email"])
