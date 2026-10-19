import hashlib
import hmac
import io
import os
from datetime import datetime, timedelta, timezone

import app as app_module


def make_metadata(share_id, *, revoked=False, downloads=0, max_downloads=3, owner_id_hash=""):
    return {
        "share_id": share_id,
        "original_filename": "demo.txt",
        "storage_path": f"files/{share_id}.txt",
        "file_size": 4,
        "upload_time": datetime.now(timezone.utc).isoformat(),
        "expiry_time": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "max_downloads": max_downloads,
        "download_count": downloads,
        "owner_id_hash": owner_id_hash,
        "revoked": revoked,
        "downloads": [],
    }


def sign_in(client, sub="test-google-subject", email="student@example.com"):
    with client.session_transaction() as saved:
        saved["user"] = {"sub": sub, "email": email, "name": "Test Student"}
        saved["_csrf_token"] = "csrf-test-token-which-is-long-enough"



def expected_owner_hash(sub="test-google-subject"):
    key = str(os.getenv("OWNER_HASH_SECRET") or app_module.app.config["SECRET_KEY"]).encode("utf-8")
    return hmac.new(key, sub.encode("utf-8"), hashlib.sha256).hexdigest()


def test_health_endpoint_works():
    client = app_module.app.test_client()
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json == {"status": "ok", "service": "QuickShare Cloud"}


def test_anonymous_home_shows_google_sign_in():
    client = app_module.app.test_client()
    response = client.get("/")
    assert response.status_code == 200
    assert b"Continue with Google" in response.data


def test_my_shares_requires_google_sign_in():
    client = app_module.app.test_client()
    response = client.get("/my-shares")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_my_shares_page_renders_for_signed_in_account():
    client = app_module.app.test_client()
    sign_in(client)
    response = client.get("/my-shares")
    assert response.status_code == 200
    assert b"FILES TRACKED" in response.data
    assert b"Google account" in response.data


def test_my_shares_api_uses_current_account_hash(monkeypatch):
    client = app_module.app.test_client()
    sign_in(client)
    calls = []
    monkeypatch.setattr(app_module, "list_user_shares", lambda owner_hash: calls.append(owner_hash) or [])
    response = client.get("/api/my-shares")
    assert response.status_code == 200
    assert response.json == {"shares": []}
    assert len(calls) == 1
    assert calls[0] == expected_owner_hash()


def test_executable_upload_is_rejected_before_storage(monkeypatch):
    client = app_module.app.test_client()
    sign_in(client)

    def unexpected_upload(**_kwargs):
        raise AssertionError("GitHub upload should not be called for executable extensions")

    monkeypatch.setattr(app_module, "upload_file_bytes", unexpected_upload)
    response = client.post(
        "/upload",
        data={
            "csrf_token": "csrf-test-token-which-is-long-enough",
            "file": (io.BytesIO(b"demo"), "suspicious.exe"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"file type is not accepted" in response.data


def test_upload_requires_google_sign_in():
    client = app_module.app.test_client()
    response = client.post(
        "/upload",
        data={"file": (io.BytesIO(b"demo"), "demo.txt")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_csrf_required_for_authenticated_upload():
    client = app_module.app.test_client()
    sign_in(client)
    response = client.post(
        "/upload",
        data={"file": (io.BytesIO(b"demo"), "demo.txt")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 400


def test_revoked_share_does_not_download(monkeypatch):
    share_id = "e" * 32
    client = app_module.app.test_client()
    monkeypatch.setattr(app_module, "get_share_metadata", lambda _sid: make_metadata(share_id, revoked=True))
    rejected = []
    monkeypatch.setattr(app_module, "record_rejected_attempt", lambda _sid, status: rejected.append(status))
    monkeypatch.setattr(app_module, "download_public_file", lambda _path: (_ for _ in ()).throw(AssertionError("must not fetch revoked file")))

    response = client.get(f"/download/{share_id}")
    assert response.status_code == 302
    assert "error=revoked" in response.headers["Location"]
    assert rejected == ["revoked"]


def test_revoke_endpoint_scopes_action_to_authenticated_account(monkeypatch):
    client = app_module.app.test_client()
    sign_in(client)
    captured = []

    def fake_revoke(share_id, owner_hash):
        captured.append((share_id, owner_hash))
        return "success"

    monkeypatch.setattr(app_module, "revoke_share", fake_revoke)
    response = client.post(
        f"/api/shares/{'f' * 32}/revoke",
        headers={"X-CSRF-Token": "csrf-test-token-which-is-long-enough"},
        json={},
    )
    assert response.status_code == 200
    assert response.json["status"] == "revoked"
    assert captured[0][0] == "f" * 32
    assert captured[0][1] == expected_owner_hash()


def test_cleanup_endpoint_requires_bearer_secret(monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "test-cron-secret-value")
    client = app_module.app.test_client()
    response = client.get("/api/cron/cleanup")
    assert response.status_code == 401



def test_storage_settings_page_is_admin_only(monkeypatch):
    monkeypatch.setenv("STORAGE_ADMIN_EMAIL", "student@example.com")
    monkeypatch.setattr(app_module.google_drive_storage, "is_configured", lambda: False)
    client = app_module.app.test_client()
    sign_in(client)
    response = client.get("/admin/storage")
    assert response.status_code == 200
    assert b"providers." in response.data
    assert b"Google Drive" in response.data

    other_client = app_module.app.test_client()
    sign_in(other_client, sub="other-subject", email="other@example.com")
    assert other_client.get("/admin/storage").status_code == 403


def test_upload_can_select_google_drive_provider(monkeypatch):
    monkeypatch.setattr(app_module, "_drive_storage_ready", lambda: True)
    client = app_module.app.test_client()
    sign_in(client)
    uploaded = []
    saved_metadata = []

    def fake_upload(**kwargs):
        uploaded.append(kwargs)
        return {"id": "drive-file-123"}

    monkeypatch.setattr(app_module, "upload_file_bytes", fake_upload)
    monkeypatch.setattr(app_module, "create_share_metadata", lambda metadata: saved_metadata.append(metadata))

    response = client.post(
        "/upload",
        data={
            "csrf_token": "csrf-test-token-which-is-long-enough",
            "storage_provider": "google_drive",
            "expiry": "24h",
            "max_downloads": "3",
            "file": (io.BytesIO(b"private bytes"), "report.txt"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 303
    assert len(uploaded) == 1
    assert uploaded[0]["storage_provider"] == "google_drive"
    assert saved_metadata[0]["storage_provider"] == "google_drive"
    assert saved_metadata[0]["drive_file_id"] == "drive-file-123"


def test_upload_rejects_google_drive_when_not_configured(monkeypatch):
    monkeypatch.setattr(app_module, "_drive_storage_ready", lambda: False)
    client = app_module.app.test_client()
    sign_in(client)
    response = client.post(
        "/upload",
        data={
            "csrf_token": "csrf-test-token-which-is-long-enough",
            "storage_provider": "google_drive",
            "file": (io.BytesIO(b"private bytes"), "report.txt"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Google Drive storage is not configured" in response.data


def test_download_dispatches_private_drive_share(monkeypatch):
    share_id = "a" * 32
    metadata = make_metadata(share_id)
    metadata["storage_provider"] = "google_drive"
    metadata["drive_file_id"] = "private-drive-file-id"
    client = app_module.app.test_client()
    monkeypatch.setattr(app_module, "get_share_metadata", lambda _sid: metadata)
    downloaded_ids = []
    monkeypatch.setattr(
        app_module.google_drive_storage,
        "download_private_file",
        lambda file_id: downloaded_ids.append(file_id) or b"private file bytes",
    )
    monkeypatch.setattr(app_module, "record_download", lambda _sid: "success")
    response = client.get(f"/download/{share_id}")
    assert response.status_code == 200
    assert response.data == b"private file bytes"
    assert downloaded_ids == ["private-drive-file-id"]



def test_connected_drive_is_default_but_github_remains_available(monkeypatch):
    client = app_module.app.test_client()
    sign_in(client)
    monkeypatch.setattr(app_module.google_drive_storage, "is_configured", lambda: True)
    response = client.get("/")
    assert response.status_code == 200
    page = response.data.decode()
    assert '<option value="google_drive" selected>Google Drive · Private shared storage</option>' in page
    assert '<option value="github">GitHub · Public file storage</option>' in page


def test_unconnected_drive_keeps_github_as_default(monkeypatch):
    client = app_module.app.test_client()
    sign_in(client)
    monkeypatch.setattr(app_module.google_drive_storage, "is_configured", lambda: False)
    response = client.get("/")
    assert response.status_code == 200
    page = response.data.decode()
    assert '<option value="github" selected>GitHub · Public file storage</option>' in page
    assert '<option value="google_drive" disabled>Google Drive · Setup required</option>' in page



def test_standard_account_cannot_initialize_upload_over_5_gb(monkeypatch):
    client = app_module.app.test_client()
    sign_in(client)
    monkeypatch.delenv("LARGE_UPLOAD_ALLOWED_EMAILS", raising=False)
    monkeypatch.setattr(app_module, "_drive_storage_ready", lambda: True)
    monkeypatch.setattr(
        app_module.google_drive_storage,
        "initiate_resumable_upload",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("Oversized upload must be rejected before Drive session creation")),
    )
    response = client.post(
        "/api/uploads/initiate",
        headers={"X-CSRF-Token": "csrf-test-token-which-is-long-enough"},
        json={
            "filename": "movie.mp4",
            "file_size": 5 * 1024**3 + 1,
            "mime_type": "video/mp4",
            "storage_provider": "google_drive",
            "expiry": "24h",
            "max_downloads": 3,
        },
    )
    assert response.status_code == 413
    assert "5 GB" in response.json["error"]


def test_approved_account_can_initialize_upload_up_to_50_gb(monkeypatch):
    client = app_module.app.test_client()
    sign_in(client)
    monkeypatch.setenv("LARGE_UPLOAD_ALLOWED_EMAILS", "other@example.com, STUDENT@example.com")
    monkeypatch.setenv("TRANSFER_SERVICE_URL", "https://transfer.example.com")
    monkeypatch.setenv("TRANSFER_TOKEN_SECRET", "unit-test-transfer-secret-which-is-long")
    monkeypatch.setattr(app_module, "_drive_storage_ready", lambda: True)
    seen = []
    monkeypatch.setattr(
        app_module.google_drive_storage,
        "initiate_resumable_upload",
        lambda **kwargs: seen.append(kwargs) or "https://www.googleapis.com/upload/drive/v3/files?upload_id=test",
    )
    response = client.post(
        "/api/uploads/initiate",
        headers={"X-CSRF-Token": "csrf-test-token-which-is-long-enough"},
        json={
            "filename": "archive.zip",
            "file_size": 40 * 1024**3,
            "mime_type": "application/zip",
            "storage_provider": "google_drive",
            "expiry": "24h",
            "max_downloads": 3,
        },
    )
    assert response.status_code == 200
    assert response.json["chunk_size"] == 32 * 1024 * 1024
    assert response.json["upload_session_url"].endswith("upload_id=test")
    assert seen[0]["file_size"] == 40 * 1024**3


def test_finalize_resumable_upload_verifies_owner_and_saves_metadata(monkeypatch):
    client = app_module.app.test_client()
    sign_in(client)
    monkeypatch.setenv("TRANSFER_SERVICE_URL", "https://transfer.example.com")
    monkeypatch.setenv("TRANSFER_TOKEN_SECRET", "unit-test-transfer-secret-which-is-long")
    monkeypatch.setattr(app_module, "_drive_storage_ready", lambda: True)
    monkeypatch.setattr(
        app_module.google_drive_storage,
        "initiate_resumable_upload",
        lambda **_kwargs: "https://www.googleapis.com/upload/drive/v3/files?upload_id=test",
    )
    saved = []
    monkeypatch.setattr(
        app_module.google_drive_storage,
        "verify_resumable_upload",
        lambda file_id, **_kwargs: {"id": file_id, "size": str(1024 * 1024)},
    )
    monkeypatch.setattr(app_module, "get_share_metadata", lambda _share_id: None)
    monkeypatch.setattr(app_module, "create_share_metadata", lambda metadata: saved.append(metadata))

    initiated = client.post(
        "/api/uploads/initiate",
        headers={"X-CSRF-Token": "csrf-test-token-which-is-long-enough"},
        json={
            "filename": "sample.bin",
            "file_size": 1024 * 1024,
            "mime_type": "application/octet-stream",
            "storage_provider": "google_drive",
            "expiry": "24h",
            "max_downloads": 3,
        },
    )
    assert initiated.status_code == 200
    response = client.post(
        "/api/uploads/finalize",
        headers={"X-CSRF-Token": "csrf-test-token-which-is-long-enough"},
        json={
            "upload_ticket": initiated.json["upload_ticket"],
            "drive_file_id": "drive-file-test",
        },
    )
    assert response.status_code == 200
    assert response.json["status"] == "success"
    assert saved[0]["drive_file_id"] == "drive-file-test"
    assert saved[0]["file_size"] == 1024 * 1024
    assert saved[0]["owner_id_hash"] == expected_owner_hash()
