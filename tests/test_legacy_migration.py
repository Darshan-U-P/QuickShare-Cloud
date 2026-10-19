import json
import sys

from scripts import migrate_github_metadata


def test_legacy_migration_defaults_to_dry_run(monkeypatch, capsys):
    records = [{
        "share_id": "a" * 32,
        "storage_provider": "google_drive",
        "expiry_time": "2099-01-01T00:00:00+00:00",
        "download_count": 0,
        "max_downloads": 3,
    }]
    monkeypatch.setattr(migrate_github_metadata, "_load_records", lambda _limit: (records, []))
    monkeypatch.setattr(migrate_github_metadata.database, "is_configured", lambda: False)
    monkeypatch.setattr(sys, "argv", ["migrate_github_metadata.py"])
    assert migrate_github_metadata.main() == 0
    output = json.loads(capsys.readouterr().out)
    assert output["mode"] == "dry-run"
    assert output["records_found"] == 1
    assert output["expired_or_revoked_candidates"] == 0
    assert "No PostgreSQL rows or storage files were changed" in output["message"]


def test_legacy_time_parser_adds_utc_to_naive_timestamps():
    parsed = migrate_github_metadata._parse_time("2026-01-01T12:00:00")
    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0
