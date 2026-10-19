import pytest
from itsdangerous import URLSafeTimedSerializer

import transfer_service


class FakeStreamResponse:
    def __init__(self, body, *, status=200, headers=None):
        self.body = body
        self.status_code = status
        self.headers = headers or {"Content-Type": "application/octet-stream", "Content-Length": str(len(body))}
        self.closed = False

    def iter_content(self, chunk_size=1024 * 1024):
        for start in range(0, len(self.body), chunk_size):
            yield self.body[start:start + chunk_size]

    def close(self):
        self.closed = True


@pytest.fixture
def transfer_test_setup(monkeypatch, tmp_path):
    monkeypatch.setattr(transfer_service, "serializer", URLSafeTimedSerializer(
        "a-unit-test-transfer-secret-long-enough", salt="quickshare-transfer-v1"
    ))
    monkeypatch.setattr(transfer_service, "TOKEN_MAX_AGE", 3600)
    monkeypatch.setattr(transfer_service, "DB_PATH", tmp_path / "transfer.sqlite3")
    monkeypatch.setattr(transfer_service.google_drive_storage, "is_configured", lambda: True)
    return transfer_service.app.test_client()


def ticket(serializer, ticket_id="a" * 32):
    return serializer.dumps({
        "jti": ticket_id,
        "share_id": "b" * 32,
        "file_id": "private-drive-id",
        "filename": "demo.bin",
        "size": 9,
    })


def test_parse_byte_ranges():
    assert transfer_service._parse_range("bytes=2-5", 10) == (2, 5, True)
    assert transfer_service._parse_range("bytes=-3", 10) == (7, 9, True)
    assert transfer_service._parse_range("", 10) == (0, 9, False)
    with pytest.raises(ValueError):
        transfer_service._parse_range("bytes=100-120", 10)


def test_large_download_streams_and_ticket_cannot_be_replayed(transfer_test_setup, monkeypatch):
    client = transfer_test_setup
    serializer = transfer_service.serializer
    upstream = FakeStreamResponse(b"test-file")
    monkeypatch.setattr(
        transfer_service.google_drive_storage,
        "open_private_file_stream",
        lambda file_id, range_header="": upstream,
    )

    response = client.get("/download/" + ticket(serializer))
    assert response.status_code == 200
    assert response.data == b"test-file"
    assert response.headers["Accept-Ranges"] == "bytes"
    assert "attachment" in response.headers["Content-Disposition"]
    assert upstream.closed is True

    # A fresh ticket is allowed; replaying the completed ticket is not.
    new_download = client.get("/download/" + ticket(serializer, "c" * 32))
    replay_same = client.get(response.request.path)
    assert new_download.status_code == 200
    assert new_download.data == b"test-file"
    assert replay_same.status_code == 410


def test_range_download_returns_partial_content(transfer_test_setup, monkeypatch):
    client = transfer_test_setup
    serializer = transfer_service.serializer
    upstream = FakeStreamResponse(
        b"t-f",
        status=206,
        headers={
            "Content-Type": "application/octet-stream",
            "Content-Length": "3",
            "Content-Range": "bytes 3-5/9",
        },
    )
    seen = []
    monkeypatch.setattr(
        transfer_service.google_drive_storage,
        "open_private_file_stream",
        lambda file_id, range_header="": seen.append(range_header) or upstream,
    )
    response = client.get(
        "/download/" + ticket(serializer),
        headers={"Range": "bytes=3-5"},
    )
    assert response.status_code == 206
    assert response.data == b"t-f"
    assert response.headers["Content-Range"] == "bytes 3-5/9"
    assert seen == ["bytes=3-5"]
