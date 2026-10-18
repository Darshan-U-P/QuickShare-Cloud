import io
from datetime import datetime, timedelta, timezone

import app as app_module


def make_metadata(share_id, *, revoked=False, downloads=0, max_downloads=3):
    return {
        "share_id": share_id,
        "original_filename": "demo.txt",
        "storage_path": f"files/{share_id}.txt",
        "file_size": 4,
        "upload_time": datetime.now(timezone.utc).isoformat(),
        "expiry_time": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "max_downloads": max_downloads,
        "download_count": downloads,
        "revoked": revoked,
        "downloads": [],
    }


def test_health_endpoint_works():
    client = app_module.app.test_client()
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json == {"status": "ok", "service": "QuickShare Cloud"}


def test_my_shares_page_renders():
    client = app_module.app.test_client()
    response = client.get("/my-shares")
    assert response.status_code == 200
    assert b"My" in response.data
    assert b"FILES TRACKED" in response.data


def test_executable_upload_is_rejected_before_storage(monkeypatch):
    client = app_module.app.test_client()

    def unexpected_upload(**_kwargs):
        raise AssertionError("GitHub upload should not be called for executable extensions")

    monkeypatch.setattr(app_module, "upload_file_bytes", unexpected_upload)
    response = client.post(
        "/upload",
        data={"file": (io.BytesIO(b"demo"), "suspicious.exe")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"file type is not accepted" in response.data


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


def test_cleanup_endpoint_requires_bearer_secret(monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "test-cron-secret-value")
    client = app_module.app.test_client()
    response = client.get("/api/cron/cleanup")
    assert response.status_code == 401



def test_vercel_uploads_are_disabled_without_creator_password(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.delenv("UPLOAD_PASSWORD", raising=False)
    client = app_module.app.test_client()
    response = client.post(
        "/upload",
        data={"file": (io.BytesIO(b"demo"), "demo.txt")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 503
    assert b"UPLOAD_PASSWORD" in response.data


def test_wrong_creator_password_is_rejected(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("UPLOAD_PASSWORD", "correct-passphrase")
    client = app_module.app.test_client()

    def unexpected_upload(**_kwargs):
        raise AssertionError("GitHub upload must not run for an incorrect creator passcode")

    monkeypatch.setattr(app_module, "upload_file_bytes", unexpected_upload)
    response = client.post(
        "/upload",
        data={
            "upload_password": "wrong-passphrase",
            "file": (io.BytesIO(b"demo"), "demo.txt"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"creator passcode is incorrect" in response.data
