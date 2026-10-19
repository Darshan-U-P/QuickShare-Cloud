from pathlib import Path

from services.database_cleanup import retry_delay_seconds


def test_cleanup_retry_backoff_is_exponential_and_bounded():
    assert retry_delay_seconds(1) == 60
    assert retry_delay_seconds(2) == 120
    assert retry_delay_seconds(3) == 240
    assert retry_delay_seconds(20) == 3600


def test_expiry_lifecycle_and_shared_reference_protection_are_explicit():
    root = Path(__file__).resolve().parent.parent
    schema = (root / "migrations" / "001_initial_schema.sql").read_text(encoding="utf-8")
    worker = (root / "services" / "database_cleanup.py").read_text(encoding="utf-8")
    assert "expires_at <= now()" in worker
    assert "active_share" in worker
    assert "lease_expires_at" in schema
    assert "cleanup_status" in schema
    assert "delete_failed" in schema
    assert "FOR UPDATE SKIP LOCKED" in worker
