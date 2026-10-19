"""Durable PostgreSQL cleanup queue for expired or revoked file objects.

This worker is staged for the database cutover. Do not run it against a stale
copy of GitHub metadata; enable it for production only after the application
has switched all share reads/writes to PostgreSQL.
"""
from __future__ import annotations

import json

from services import database
from services.storage_errors import StorageError


def retry_delay_seconds(attempt: int) -> int:
    """Bounded exponential backoff: 60, 120, 240 ... up to one hour."""
    attempt = max(1, int(attempt))
    return min(3600, 60 * (2 ** min(attempt - 1, 6)))


def enqueue_expired_file_cleanup(limit: int = 100) -> int:
    """Queue a file only when no active share still needs that physical object."""
    limit = max(1, min(1000, int(limit)))
    with database.connection() as conn:
        with conn.transaction():
            queued = conn.execute(
                """
                WITH eligible_files AS (
                    SELECT f.file_id
                    FROM files AS f
                    WHERE f.lifecycle_state IN ('active', 'pending_delete')
                      AND EXISTS (
                          SELECT 1
                          FROM shares AS s
                          WHERE s.file_id = f.file_id
                            AND (
                                s.revoked_at IS NOT NULL
                                OR s.expires_at <= now()
                                OR s.download_count >= s.max_downloads
                                OR s.lifecycle_state IN ('revoked', 'expired', 'limit_reached', 'deleted')
                            )
                      )
                      AND NOT EXISTS (
                          SELECT 1
                          FROM shares AS active_share
                          WHERE active_share.file_id = f.file_id
                            AND active_share.revoked_at IS NULL
                            AND active_share.expires_at > now()
                            AND active_share.download_count < active_share.max_downloads
                            AND active_share.lifecycle_state = 'active'
                      )
                    ORDER BY f.created_at ASC
                    LIMIT %s
                )
                INSERT INTO cleanup_jobs (file_id, job_type, status, available_at)
                SELECT file_id, 'delete_storage_object', 'queued', now()
                FROM eligible_files
                ON CONFLICT DO NOTHING
                RETURNING file_id
                """,
                (limit,),
            ).fetchall()
            conn.execute(
                """
                UPDATE files AS f
                SET lifecycle_state = 'pending_delete', updated_at = now()
                WHERE f.lifecycle_state = 'active'
                  AND EXISTS (
                      SELECT 1
                      FROM cleanup_jobs AS j
                      WHERE j.file_id = f.file_id
                        AND j.job_type = 'delete_storage_object'
                        AND j.status IN ('queued', 'retry')
                  )
                """
            )
            return len(queued)


def _claim_jobs(limit: int, lease_seconds: int) -> list[tuple]:
    with database.connection() as conn:
        with conn.transaction():
            rows = conn.execute(
                """
                WITH due_jobs AS (
                    SELECT job_id
                    FROM cleanup_jobs
                    WHERE (
                        status IN ('queued', 'retry')
                        AND available_at <= now()
                    ) OR (
                        status = 'running'
                        AND lease_expires_at <= now()
                    )
                    ORDER BY available_at ASC, created_at ASC
                    FOR UPDATE SKIP LOCKED
                    LIMIT %s
                )
                UPDATE cleanup_jobs AS j
                SET status = 'running',
                    attempts = j.attempts + 1,
                    lease_expires_at = now() + (%s * interval '1 second'),
                    updated_at = now()
                FROM due_jobs
                WHERE j.job_id = due_jobs.job_id
                RETURNING j.job_id, j.file_id, j.attempts, j.max_attempts
                """,
                (limit, lease_seconds),
            ).fetchall()
            return rows


def _prepare_file_for_deletion(file_id):
    """Lock and recheck references before touching the external storage provider."""
    with database.connection() as conn:
        with conn.transaction():
            file_row = conn.execute(
                """
                SELECT file_id, storage_provider, storage_object_id, storage_path, lifecycle_state
                FROM files
                WHERE file_id = %s
                FOR UPDATE
                """,
                (file_id,),
            ).fetchone()
            if not file_row:
                return "missing", None

            _file_id, provider, object_id, path, state = file_row
            active_share = conn.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM shares
                    WHERE file_id = %s
                      AND revoked_at IS NULL
                      AND expires_at > now()
                      AND download_count < max_downloads
                      AND lifecycle_state = 'active'
                )
                """,
                (file_id,),
            ).fetchone()[0]

            if active_share:
                conn.execute(
                    """
                    UPDATE files SET lifecycle_state = 'active', updated_at = now()
                    WHERE file_id = %s
                    """,
                    (file_id,),
                )
                conn.execute(
                    """
                    UPDATE cleanup_jobs
                    SET status = 'cancelled', lease_expires_at = NULL,
                        last_error_code = 'active_reference',
                        last_error_summary = 'An active share still references this file.',
                        updated_at = now(), completed_at = now()
                    WHERE file_id = %s AND status = 'running'
                    """,
                    (file_id,),
                )
                return "retained_shared", None

            if state == "deleted":
                conn.execute(
                    """
                    UPDATE cleanup_jobs
                    SET status = 'completed', lease_expires_at = NULL,
                        updated_at = now(), completed_at = now()
                    WHERE file_id = %s AND status = 'running'
                    """,
                    (file_id,),
                )
                return "already_deleted", None

            conn.execute(
                "UPDATE files SET lifecycle_state = 'deleting', updated_at = now() WHERE file_id = %s",
                (file_id,),
            )
            return "delete", {
                "file_id": file_id,
                "provider": provider,
                "object_id": object_id,
                "storage_path": path,
            }


def _delete_storage_object(file_record: dict) -> str:
    provider = file_record["provider"]
    if provider == "google_drive":
        from services.google_drive_storage import delete_private_file
        return delete_private_file(file_record["object_id"])
    if provider == "github":
        from services.github_storage import delete_storage_file
        return delete_storage_file(file_record["storage_path"] or file_record["object_id"])
    raise StorageError("The file has an unsupported storage provider.")


def _record_success(job_id, file_id, result: str) -> None:
    status = "deleted" if result == "deleted" else "already_missing"
    with database.connection() as conn:
        with conn.transaction():
            conn.execute(
                """
                UPDATE files
                SET lifecycle_state = 'deleted', deleted_at = COALESCE(deleted_at, now()),
                    updated_at = now(), delete_attempts = delete_attempts + 1
                WHERE file_id = %s
                """,
                (file_id,),
            )
            conn.execute(
                """
                UPDATE shares
                SET cleanup_status = %s, cleanup_at = now(), updated_at = now()
                WHERE file_id = %s
                """,
                (status, file_id),
            )
            conn.execute(
                """
                UPDATE cleanup_jobs
                SET status = 'completed', lease_expires_at = NULL,
                    last_error_code = NULL, last_error_summary = NULL,
                    updated_at = now(), completed_at = now()
                WHERE job_id = %s
                """,
                (job_id,),
            )
            conn.execute(
                """
                INSERT INTO audit_logs (action, target_type, target_id, details)
                VALUES (%s, 'file', %s, %s::jsonb)
                """,
                (
                    "storage_file." + status,
                    str(file_id),
                    json.dumps({"provider_deletion_result": result}),
                ),
            )


def _record_failure(job_id, file_id, attempt: int, max_attempts: int, error: Exception) -> str:
    terminal = attempt >= max_attempts
    status = "failed" if terminal else "retry"
    file_state = "delete_failed" if terminal else "pending_delete"
    error_summary = str(error).replace("\r", " ").replace("\n", " ")[:400] or "Unknown storage deletion error."
    next_delay = retry_delay_seconds(attempt)
    with database.connection() as conn:
        with conn.transaction():
            conn.execute(
                """
                UPDATE files
                SET lifecycle_state = %s, updated_at = now(), delete_attempts = delete_attempts + 1
                WHERE file_id = %s
                """,
                (file_state, file_id),
            )
            conn.execute(
                """
                UPDATE shares
                SET cleanup_status = %s, updated_at = now()
                WHERE file_id = %s AND cleanup_status NOT IN ('deleted', 'already_missing')
                """,
                ("failed" if terminal else "queued", file_id),
            )
            conn.execute(
                """
                UPDATE cleanup_jobs
                SET status = %s, available_at = now() + (%s * interval '1 second'),
                    lease_expires_at = NULL, last_error_code = %s,
                    last_error_summary = %s, updated_at = now(),
                    completed_at = CASE WHEN %s THEN now() ELSE NULL END
                WHERE job_id = %s
                """,
                (
                    status,
                    next_delay,
                    type(error).__name__[:80],
                    error_summary,
                    terminal,
                    job_id,
                ),
            )
            conn.execute(
                """
                INSERT INTO audit_logs (action, target_type, target_id, details)
                VALUES (%s, 'file', %s, %s::jsonb)
                """,
                (
                    "storage_file.delete_failed" if terminal else "storage_file.delete_retry",
                    str(file_id),
                    json.dumps({"attempt": attempt, "terminal": terminal, "error_type": type(error).__name__}),
                ),
            )
    return status


def process_cleanup_jobs(max_jobs: int = 20, *, lease_seconds: int = 120) -> dict:
    """Queue and process a bounded number of deletion jobs idempotently."""
    max_jobs = max(1, min(100, int(max_jobs)))
    lease_seconds = max(30, min(900, int(lease_seconds)))
    queued = enqueue_expired_file_cleanup(limit=max_jobs * 5)
    claimed = _claim_jobs(max_jobs, lease_seconds)
    summary = {
        "queued": queued,
        "claimed": len(claimed),
        "deleted": 0,
        "already_missing": 0,
        "retained_shared": 0,
        "retrying": 0,
        "failed": 0,
        "skipped": 0,
    }

    for job_id, file_id, attempts, max_attempts in claimed:
        decision, file_record = _prepare_file_for_deletion(file_id)
        if decision == "retained_shared":
            summary["retained_shared"] += 1
            continue
        if decision == "already_deleted":
            summary["already_missing"] += 1
            continue
        if decision != "delete" or not file_record:
            summary["skipped"] += 1
            continue

        try:
            deletion_result = _delete_storage_object(file_record)
            _record_success(job_id, file_id, deletion_result)
            summary["deleted" if deletion_result == "deleted" else "already_missing"] += 1
        except Exception as exc:
            state = _record_failure(job_id, file_id, int(attempts), int(max_attempts), exc)
            summary["failed" if state == "failed" else "retrying"] += 1

    return summary
