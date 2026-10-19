from services import google_drive_storage as drive


class FakeResponse:
    def __init__(self, status_code=200, payload=None, content=b"", text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.content = content
        self.text = text
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
