"""Dry-run or import existing GitHub JSON share records into PostgreSQL.

This is an idempotent copy operation. It does not switch the running web app
away from GitHub metadata. Use --apply only after configuring DATABASE_URL and
reviewing a successful dry run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from uuid import uuid4

from services import database, google_drive_storage
from services.github_storage import _list_metadata_names, get_share_metadata
from services.storage_errors import StorageError


SHARE_ID_RE = re.compile(r"^[a-f0-9]{32}$")
OWNER_HASH_RE = re.compile(r"^[a-f0-9]{64}$")


def _parse_time(value, default=None):
    if not value:
        return default or datetime.now(timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return default or datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _load_records(limit: int | None = None) -> tuple[list[dict], list[dict]]:
    names = _list_metadata_names()
    if limit is not None:
        names = names[:max(0, limit)]
    records, errors = [], []
    for share_id in names:
        try:
            metadata = get_share_metadata(share_id)
            if not metadata:
                errors.append({"share_id": share_id, "error": "metadata_missing"})
                continue
            if not SHARE_ID_RE.fullmatch(str(metadata.get("share_id", share_id))):
                errors.append({"share_id": share_id, "error": "invalid_share_id"})
                continue
            records.append(metadata)
        except StorageError as exc:
            errors.append({"share_id": share_id, "error": str(exc)[:250]})
    return records, errors


def _get_or_create_legacy_user(conn, owner_hash: str):
    row = conn.execute("SELECT user_id FROM users WHERE owner_hash = %s", (owner_hash,)).fetchone()
    if row:
        return row[0]
    row = conn.execute(
        """
        INSERT INTO users (owner_hash)
        VALUES (%s)
        ON CONFLICT (owner_hash) DO UPDATE SET owner_hash = EXCLUDED.owner_hash
        RETURNING user_id
        """,
        (owner_hash,),
    ).fetchone()
    return row[0]


def _get_or_create_file(conn, metadata: dict):
    provider = str(metadata.get("storage_provider") or "github").strip().lower()
    if provider not in {"github", "google_drive"}:
        raise ValueError("unsupported_storage_provider")
    path = str(metadata.get("storage_path") or "").strip()
    if provider == "google_drive":
        object_id = str(metadata.get("drive_file_id") or "").strip()
        if not object_id:
            raise ValueError("drive_file_id_missing")
    else:
        object_id = path
        if not object_id:
            raise ValueError("github_storage_path_missing")
    try:
        byte_size = int(metadata.get("file_size", 0))
    except (TypeError, ValueError):
        byte_size = -1
    if byte_size < 0:
        raise ValueError("invalid_file_size")

    row = conn.execute(
        """
        INSERT INTO files (
            file_id, storage_provider, storage_object_id, storage_path,
            byte_size, mime_type, lifecycle_state, created_at, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, 'active', %s, now())
        ON CONFLICT (storage_provider, storage_object_id) DO NOTHING
        RETURNING file_id, byte_size
        """,
        (
            uuid4(), provider, object_id, path or None, byte_size,
            str(metadata.get("mime_type") or "application/octet-stream")[:120],
            _parse_time(metadata.get("upload_time")),
        ),
    ).fetchone()
    if row:
        return row[0]

    existing = conn.execute(
        """
        SELECT file_id, byte_size
        FROM files
        WHERE storage_provider = %s AND storage_object_id = %s
        FOR UPDATE
        """,
        (provider, object_id),
    ).fetchone()
    if not existing:
        raise RuntimeError("file_upsert_failed")
    if int(existing[1]) != byte_size:
        raise ValueError("same_storage_object_has_conflicting_file_size")
    return existing[0]


def _import_one(conn, metadata: dict) -> tuple[bool, int]:
    share_id = str(metadata.get("share_id", "")).strip().lower()
    if not SHARE_ID_RE.fullmatch(share_id):
        raise ValueError("invalid_share_id")
    try:
        max_downloads = int(metadata["max_downloads"])
        download_count = int(metadata.get("download_count", 0))
        if max_downloads <= 0 or download_count < 0:
            raise ValueError("invalid_download_limits")
        expires_at = _parse_time(metadata["expiry_time"])
    except KeyError as exc:
        raise ValueError("required_share_field_missing") from exc
    original_filename = str(metadata.get("original_filename") or "Unnamed file")[:1024]
    created_at = _parse_time(metadata.get("upload_time"))

    owner_hash = str(metadata.get("owner_id_hash") or "").strip().lower()
    owner_user_id = None
    if owner_hash and OWNER_HASH_RE.fullmatch(owner_hash):
        owner_user_id = _get_or_create_legacy_user(conn, owner_hash)
    else:
        owner_hash = None

    file_id = _get_or_create_file(conn, metadata)
    revoked_at = _parse_time(metadata.get("revoked_at")) if metadata.get("revoked") else None
    now = datetime.now(timezone.utc)
    if metadata.get("cleanup_status") in {"deleted", "already_missing"}:
        lifecycle_state = "deleted"
        cleanup_status = str(metadata.get("cleanup_status"))
    elif revoked_at:
        lifecycle_state = "revoked"
        cleanup_status = str(metadata.get("cleanup_status") or "pending")
    elif expires_at <= now:
        lifecycle_state = "expired"
        cleanup_status = str(metadata.get("cleanup_status") or "pending")
    elif download_count >= max_downloads:
        lifecycle_state = "limit_reached"
        cleanup_status = str(metadata.get("cleanup_status") or "pending")
    else:
        lifecycle_state = "active"
        cleanup_status = str(metadata.get("cleanup_status") or "pending")
    if cleanup_status not in {"pending", "queued", "deleted", "already_missing", "failed", "retained_shared"}:
        cleanup_status = "pending"

    inserted = conn.execute(
        """
        INSERT INTO shares (
            share_pk, public_share_id, file_id, owner_user_id, owner_hash,
            original_filename, created_at, expires_at, max_downloads,
            download_count, revoked_at, lifecycle_state, cleanup_status,
            cleanup_at, legacy_metadata, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, now())
        ON CONFLICT (public_share_id) DO NOTHING
        RETURNING share_pk
        """,
        (
            uuid4(), share_id, file_id, owner_user_id, owner_hash, original_filename,
            created_at, expires_at, max_downloads, download_count, revoked_at,
            lifecycle_state, cleanup_status,
            _parse_time(metadata.get("cleanup_at")) if metadata.get("cleanup_at") else None,
            json.dumps(metadata, ensure_ascii=False),
        ),
    ).fetchone()
    if inserted:
        share_pk, was_inserted = inserted[0], True
    else:
        existing = conn.execute(
            "SELECT share_pk, file_id, owner_hash FROM shares WHERE public_share_id = %s",
            (share_id,),
        ).fetchone()
        if not existing or existing[1] != file_id or (existing[2] or None) != owner_hash:
            raise ValueError("existing_share_conflicts_with_legacy_record")
        share_pk, was_inserted = existing[0], False

    events = metadata.get("downloads", [])
    if not isinstance(events, list):
        events = []
    events_imported = 0
    for index, event in enumerate(events[-100:]):
        if not isinstance(event, dict):
            continue
        canonical = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        idempotency_key = hashlib.sha256(f"{share_id}:{index}:{canonical}".encode("utf-8")).hexdigest()
        event_type = str(event.get("status") or "unknown")[:80]
        if event_type == "success":
            outcome = "success"
        elif event_type.startswith("rejected"):
            outcome = "rejected"
        else:
            outcome = "system"
        result = conn.execute(
            """
            INSERT INTO download_events (share_pk, occurred_at, outcome, event_type, idempotency_key, details)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING event_id
            """,
            (
                share_pk,
                _parse_time(event.get("time"), default=created_at),
                outcome,
                event_type,
                idempotency_key,
                json.dumps(event, ensure_ascii=False),
            ),
        ).fetchone()
        if result:
            events_imported += 1
    return was_inserted, events_imported


def _import_transfer_permissions() -> dict:
    """Synchronize initial DB approvals to the private Drive source of truth."""
    emails = sorted({
        str(email).strip().lower()
        for email in google_drive_storage.get_large_upload_allowlist()
        if isinstance(email, str) and email.strip()
    })
    if any(len(email) > 254 or "@" not in email for email in emails):
        raise ValueError("invalid_transfer_permission_email")

    imported = 0
    revoked = 0
    with database.connection() as conn:
        with conn.transaction():
            active_rows = conn.execute(
                "SELECT permission_id, email_normalized FROM transfer_permissions WHERE revoked_at IS NULL"
            ).fetchall()
            approved = set(emails)
            for permission_id, email in active_rows:
                if email not in approved:
                    conn.execute(
                        "UPDATE transfer_permissions SET revoked_at = now(), updated_at = now() WHERE permission_id = %s",
                        (permission_id,),
                    )
                    revoked += 1
            for email in emails:
                row = conn.execute(
                    """
                    INSERT INTO transfer_permissions (email_normalized)
                    VALUES (%s)
                    ON CONFLICT DO NOTHING
                    RETURNING permission_id
                    """,
                    (email,),
                ).fetchone()
                if row:
                    imported += 1
    return {"approved_in_drive": len(emails), "imported": imported, "revoked": revoked}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Apply schema migrations and import records. Default is dry-run.")
    parser.add_argument("--limit", type=int, default=None, help="Limit the number of legacy share records scanned.")
    args = parser.parse_args()

    try:
        records, errors = _load_records(args.limit)
    except StorageError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2), file=sys.stderr)
        return 1

    report = {
        "mode": "apply" if args.apply else "dry-run",
        "records_found": len(records),
        "records_with_read_errors": len(errors),
        "providers": {},
        "expired_or_revoked_candidates": 0,
        "imported": 0,
        "already_present": 0,
        "events_imported": 0,
        "errors": errors[:50],
    }
    for metadata in records:
        provider = str(metadata.get("storage_provider") or "github")
        report["providers"][provider] = report["providers"].get(provider, 0) + 1
        try:
            max_downloads = int(metadata.get("max_downloads", 1))
            download_count = int(metadata.get("download_count", 0))
        except (TypeError, ValueError):
            max_downloads, download_count = 1, 0
        if (
            metadata.get("revoked")
            or _parse_time(metadata.get("expiry_time")) <= datetime.now(timezone.utc)
            or download_count >= max_downloads
        ):
            report["expired_or_revoked_candidates"] += 1

    if not args.apply:
        report["message"] = "Dry run only. No PostgreSQL rows or storage files were changed."
        print(json.dumps(report, indent=2))
        return 0 if not errors else 1

    if not database.is_configured():
        print("DATABASE_URL is required when --apply is used.", file=sys.stderr)
        return 2
    try:
        report["applied_migrations"] = database.run_migrations()
        with database.connection() as conn:
            for metadata in records:
                share_id = str(metadata.get("share_id") or "")
                try:
                    with conn.transaction():
                        inserted, event_count = _import_one(conn, metadata)
                    report["imported" if inserted else "already_present"] += 1
                    report["events_imported"] += event_count
                except Exception as exc:
                    report["errors"].append({"share_id": share_id, "error": str(exc)[:250] or type(exc).__name__})
                    report["records_with_read_errors"] += 1
        try:
            report["transfer_permissions"] = _import_transfer_permissions()
        except Exception as exc:
            report["transfer_permissions_error"] = str(exc)[:250] or type(exc).__name__
            report["records_with_read_errors"] += 1
    except database.DatabaseError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    report["ok"] = report["records_with_read_errors"] == 0
    report["message"] = (
        "Import complete. The Flask application still uses GitHub metadata; "
        "do not enable PostgreSQL cleanup or switch production reads until the application cutover is implemented."
    )
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
