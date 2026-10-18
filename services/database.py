import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def _connect(db_path):
    connection = sqlite3.connect(str(db_path), timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db(db_path):
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with _connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS files (
                share_id TEXT PRIMARY KEY,
                original_filename TEXT NOT NULL,
                storage_path TEXT NOT NULL UNIQUE,
                file_size INTEGER NOT NULL,
                upload_time TEXT NOT NULL,
                expiry_time TEXT NOT NULL,
                max_downloads INTEGER NOT NULL,
                download_count INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS download_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                share_id TEXT NOT NULL,
                download_time TEXT NOT NULL,
                status TEXT NOT NULL,
                FOREIGN KEY (share_id) REFERENCES files(share_id)
            );

            CREATE INDEX IF NOT EXISTS idx_files_expiry ON files(expiry_time);
            CREATE INDEX IF NOT EXISTS idx_download_logs_share_id ON download_logs(share_id);
            """
        )


def add_file(db_path, *, share_id, original_filename, storage_path, file_size,
             upload_time, expiry_time, max_downloads):
    with _connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO files (
                share_id, original_filename, storage_path, file_size,
                upload_time, expiry_time, max_downloads, download_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (share_id, original_filename, storage_path, file_size, upload_time,
             expiry_time, max_downloads),
        )


def get_file(db_path, share_id):
    with _connect(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM files WHERE share_id = ?", (share_id,)
        ).fetchone()
        return dict(row) if row else None


def record_download(db_path, share_id):
    """Atomically verify access and count one successful download."""
    now = datetime.now(timezone.utc)
    now_text = now.isoformat(timespec="seconds")
    with _connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT expiry_time, max_downloads, download_count FROM files WHERE share_id = ?",
            (share_id,),
        ).fetchone()
        if row is None:
            connection.execute("ROLLBACK")
            return "not_found"

        expires = datetime.fromisoformat(row["expiry_time"])
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires <= now:
            connection.execute(
                "INSERT INTO download_logs (share_id, download_time, status) VALUES (?, ?, ?)",
                (share_id, now_text, "expired"),
            )
            connection.commit()
            return "expired"

        if row["download_count"] >= row["max_downloads"]:
            connection.execute(
                "INSERT INTO download_logs (share_id, download_time, status) VALUES (?, ?, ?)",
                (share_id, now_text, "limit"),
            )
            connection.commit()
            return "limit"

        connection.execute(
            "UPDATE files SET download_count = download_count + 1 WHERE share_id = ?",
            (share_id,),
        )
        connection.execute(
            "INSERT INTO download_logs (share_id, download_time, status) VALUES (?, ?, ?)",
            (share_id, now_text, "success"),
        )
        connection.commit()
        return "success"


def record_rejected_attempt(db_path, share_id, status):
    now_text = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _connect(db_path) as connection:
        connection.execute(
            "INSERT INTO download_logs (share_id, download_time, status) VALUES (?, ?, ?)",
            (share_id, now_text, status),
        )
