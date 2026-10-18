from datetime import datetime, timedelta, timezone

from services.database import add_file, get_file, init_db, record_download


def create_test_file(db_path, *, share_id="a" * 32, expiry_delta=timedelta(hours=1), max_downloads=1):
    now = datetime.now(timezone.utc)
    init_db(db_path)
    add_file(
        db_path,
        share_id=share_id,
        original_filename="sample.txt",
        storage_path=f"files/{share_id}.txt",
        file_size=12,
        upload_time=now.isoformat(timespec="seconds"),
        expiry_time=(now + expiry_delta).isoformat(timespec="seconds"),
        max_downloads=max_downloads,
    )


def test_successful_download_is_counted(tmp_path):
    db_path = tmp_path / "quickshare.db"
    create_test_file(db_path, max_downloads=2)

    assert record_download(db_path, "a" * 32) == "success"
    saved = get_file(db_path, "a" * 32)
    assert saved["download_count"] == 1


def test_download_limit_is_enforced(tmp_path):
    db_path = tmp_path / "quickshare.db"
    create_test_file(db_path, max_downloads=1)

    assert record_download(db_path, "a" * 32) == "success"
    assert record_download(db_path, "a" * 32) == "limit"
    assert get_file(db_path, "a" * 32)["download_count"] == 1


def test_expired_file_cannot_be_downloaded(tmp_path):
    db_path = tmp_path / "quickshare.db"
    create_test_file(db_path, expiry_delta=timedelta(seconds=-1), max_downloads=3)

    assert record_download(db_path, "a" * 32) == "expired"
    assert get_file(db_path, "a" * 32)["download_count"] == 0
