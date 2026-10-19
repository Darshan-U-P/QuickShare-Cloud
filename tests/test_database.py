from pathlib import Path

import pytest

from services import database


def test_database_is_optional_until_migration_is_selected(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert database.is_configured() is False
    with pytest.raises(database.DatabaseNotConfiguredError, match="DATABASE_URL"):
        database.get_pool()


def test_database_url_normalizes_legacy_postgres_scheme(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://user:pass@example.invalid/db")
    assert database._database_url() == "postgresql://user:pass@example.invalid/db"


def test_database_rejects_non_postgres_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "mysql://user:pass@example.invalid/db")
    with pytest.raises(database.DatabaseNotConfiguredError, match="PostgreSQL"):
        database._database_url()


def test_migration_files_are_versioned_and_have_stable_checksums():
    migrations = database.load_migrations()
    assert [migration.version for migration in migrations] == ["001_initial_schema"]
    assert len(migrations[0].checksum) == 64
    assert migrations[0].checksum == database.load_migrations()[0].checksum


def test_schema_has_required_tables_constraints_and_deletion_lifecycle():
    migrations = database.load_migrations()
    sql = "\n".join(item.sql for item in migrations)
    for table in (
        "users", "files", "shares", "download_events", "upload_sessions",
        "transfer_permissions", "cleanup_jobs", "audit_logs",
    ):
        assert f"CREATE TABLE {table}" in sql
    assert "UNIQUE (storage_provider, storage_object_id)" in sql
    assert "files_provider_sha256_active_uq" in sql
    assert "ON DELETE RESTRICT" in sql
    assert "'pending_delete', 'deleting', 'deleted', 'delete_failed'" in sql
    assert "cleanup_jobs_one_open_job_per_file_uq" in sql


def test_sql_splitter_ignores_comment_only_fragments():
    statements = database._split_sql_statements("""
        -- table comment
        CREATE TABLE sample (id integer PRIMARY KEY);
        -- index comment
        CREATE INDEX sample_id_idx ON sample (id);
    """)
    assert len(statements) == 2
    assert statements[0].startswith("CREATE TABLE")
    assert statements[1].startswith("CREATE INDEX")
