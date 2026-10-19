"""Supabase PostgreSQL connection pooling and versioned schema migrations."""
from __future__ import annotations

import hashlib
import os
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from dotenv import load_dotenv
from psycopg_pool import ConnectionPool


BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")
DEFAULT_MIGRATIONS_DIR = BASE_DIR / "migrations"
MIGRATION_LOCK_ID = 73490162804517
_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()


class DatabaseError(RuntimeError):
    """Safe, non-secret-bearing error for database setup and operations."""


class DatabaseNotConfiguredError(DatabaseError):
    """Raised when a database operation is attempted without DATABASE_URL."""


@dataclass(frozen=True)
class Migration:
    version: str
    path: Path
    sql: str
    checksum: str


def is_configured() -> bool:
    return bool(os.getenv("DATABASE_URL", "").strip())


def _database_url() -> str:
    """Normalize a Supabase Postgres URI and require TLS by default."""
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise DatabaseNotConfiguredError(
            "Supabase PostgreSQL is not configured. Set DATABASE_URL before running database commands."
        )
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if not url.startswith(("postgresql://", "postgresql+psycopg://")):
        raise DatabaseNotConfiguredError("DATABASE_URL must be a PostgreSQL connection URI.")
    url = url.replace("postgresql+psycopg://", "postgresql://", 1)
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.setdefault("sslmode", "require")
    return urlunsplit((
        parsed.scheme,
        parsed.netloc,
        parsed.path,
        urlencode(query),
        parsed.fragment,
    ))


def _pool_max_size() -> int:
    try:
        value = int(os.getenv("DATABASE_POOL_MAX_SIZE", "1"))
    except (TypeError, ValueError):
        value = 1
    return max(1, min(3, value))


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is not None:
            return _pool
        url = _database_url()
        try:
            pool = ConnectionPool(
                conninfo=url,
                min_size=0,
                max_size=_pool_max_size(),
                timeout=5,
                reconnect_timeout=10,
                kwargs={
                    "connect_timeout": 5,
                    "application_name": "quickshare-cloud",
                    # Supabase transaction pooling does not support named
                    # prepared statements; disable psycopg automatic prepare.
                    "prepare_threshold": None,
                },
                open=False,
            )
            pool.open(wait=True, timeout=5)
        except Exception as exc:
            raise DatabaseError(
                "Could not initialize PostgreSQL. Check DATABASE_URL and the provider's connection settings."
            ) from exc
        _pool = pool
        return pool


@contextmanager
def connection() -> Iterator:
    """Yield a pooled psycopg connection; the pool commits or rolls back on exit."""
    try:
        with get_pool().connection(timeout=5) as conn:
            yield conn
    except DatabaseError:
        raise
    except Exception as exc:
        raise DatabaseError(
            "A PostgreSQL operation failed. Check database connectivity and application logs."
        ) from exc


def close_pool() -> None:
    """Close the process-local pool (primarily useful for tests and CLI tools)."""
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.close()
            _pool = None


def load_migrations(migrations_dir: Path | None = None) -> list[Migration]:
    directory = migrations_dir or DEFAULT_MIGRATIONS_DIR
    if not directory.is_dir():
        raise DatabaseError(f"Migration directory does not exist: {directory}")
    migrations: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        if not re.match(r"^\d{3,}_[a-z0-9_]+\.sql$", path.name):
            raise DatabaseError(f"Migration filename {path.name!r} must use NNN_name.sql format.")
        raw = path.read_bytes()
        migrations.append(Migration(
            version=path.stem,
            path=path,
            sql=raw.decode("utf-8"),
            checksum=hashlib.sha256(raw).hexdigest(),
        ))
    if not migrations:
        raise DatabaseError(f"No SQL migrations were found in {directory}.")
    versions = [item.version for item in migrations]
    if len(versions) != len(set(versions)):
        raise DatabaseError("Migration versions must be unique.")
    return migrations


def _split_sql_statements(script: str) -> list[str]:
    """Split simple DDL migrations without stored procedures or dollar-quoted blocks."""
    # Remove full-line comments before splitting so a semicolon inside a
    # comment cannot leave a fragment of comment text as executable SQL.
    without_comments = re.sub(r"(?m)^\s*--.*$", "", script)
    return [part.strip() for part in without_comments.split(";") if part.strip()]


def run_migrations(migrations_dir: Path | None = None) -> list[str]:
    """Apply unapplied migrations transactionally and reject checksum drift."""
    migrations = load_migrations(migrations_dir)
    applied_now: list[str] = []
    with connection() as conn:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK_ID,))
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version text PRIMARY KEY,
                    checksum char(64) NOT NULL,
                    applied_at timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            existing_rows = conn.execute(
                "SELECT version, checksum FROM schema_migrations"
            ).fetchall()
            existing = {str(version): str(checksum).strip() for version, checksum in existing_rows}
            for migration in migrations:
                saved_checksum = existing.get(migration.version)
                if saved_checksum:
                    if saved_checksum != migration.checksum:
                        raise DatabaseError(
                            f"Applied migration {migration.version} was modified. "
                            "Create a new migration instead of editing an applied migration."
                        )
                    continue
                for statement in _split_sql_statements(migration.sql):
                    conn.execute(statement, prepare=False)
                conn.execute(
                    "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)",
                    (migration.version, migration.checksum),
                )
                applied_now.append(migration.version)
    return applied_now


def health_check() -> bool:
    with connection() as conn:
        row = conn.execute("SELECT 1").fetchone()
    return bool(row and row[0] == 1)
