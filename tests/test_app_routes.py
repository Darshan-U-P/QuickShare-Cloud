import io
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
    expected = app_module._current_owner_hash()
    assert calls[0] == expected


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
    assert captured[0][1] == app_module._current_owner_hash()


def test_cleanup_endpoint_requires_bearer_secret(monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "test-cron-secret-value")
    client = app_module.app.test_client()
    response = client.get("/api/cron/cleanup")
    assert response.status_code == 401
