from datetime import datetime, timedelta, timezone

from app import share_status


def record(*, expiry_delta=timedelta(hours=1), downloads=0, maximum=3):
    now = datetime.now(timezone.utc)
    return {
        "expiry_time": (now + expiry_delta).isoformat(timespec="seconds"),
        "download_count": downloads,
        "max_downloads": maximum,
    }


def test_unexpired_share_with_remaining_downloads_is_active():
    assert share_status(record()) == "active"


def test_expired_share_is_rejected():
    assert share_status(record(expiry_delta=timedelta(seconds=-1))) == "expired"


def test_download_limit_is_enforced():
    assert share_status(record(downloads=3, maximum=3)) == "limit"
