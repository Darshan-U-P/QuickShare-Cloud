-- QuickShare Cloud initial relational schema.
-- Keep this file immutable after application; create a new numbered migration.
-- PostgreSQL 13+ is required for gen_random_uuid().

CREATE TABLE users (
    user_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_hash char(64) NOT NULL UNIQUE,
    google_subject_hash char(64) UNIQUE,
    email text,
    email_verified boolean NOT NULL DEFAULT false,
    display_name text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    disabled_at timestamptz,
    CONSTRAINT users_owner_hash_format CHECK (owner_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT users_subject_hash_format CHECK (
        google_subject_hash IS NULL OR google_subject_hash ~ '^[0-9a-f]{64}$'
    )
);
CREATE INDEX users_email_lower_idx ON users (lower(email)) WHERE email IS NOT NULL;

CREATE TABLE files (
    file_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    storage_provider text NOT NULL CHECK (storage_provider IN ('google_drive', 'github')),
    storage_object_id text NOT NULL,
    storage_path text,
    content_sha256 char(64),
    byte_size bigint NOT NULL CHECK (byte_size >= 0),
    mime_type text NOT NULL DEFAULT 'application/octet-stream',
    lifecycle_state text NOT NULL DEFAULT 'active'
        CHECK (lifecycle_state IN ('active', 'pending_delete', 'deleting', 'deleted', 'delete_failed', 'orphaned')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    delete_attempts integer NOT NULL DEFAULT 0 CHECK (delete_attempts >= 0),
    UNIQUE (storage_provider, storage_object_id),
    CONSTRAINT files_sha256_format CHECK (
        content_sha256 IS NULL OR content_sha256 ~ '^[0-9a-f]{64}$'
    )
);
CREATE UNIQUE INDEX files_provider_sha256_active_uq
    ON files (storage_provider, content_sha256)
    WHERE content_sha256 IS NOT NULL AND lifecycle_state <> 'deleted';
CREATE INDEX files_lifecycle_created_idx ON files (lifecycle_state, created_at);
CREATE INDEX files_delete_retry_idx ON files (lifecycle_state, updated_at)
    WHERE lifecycle_state IN ('pending_delete', 'delete_failed');

CREATE TABLE shares (
    share_pk uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    public_share_id varchar(32) NOT NULL UNIQUE
        CHECK (public_share_id ~ '^[a-f0-9]{32}$'),
    file_id uuid NOT NULL REFERENCES files(file_id) ON DELETE RESTRICT,
    owner_user_id uuid REFERENCES users(user_id) ON DELETE RESTRICT,
    owner_hash char(64),
    original_filename text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    max_downloads integer NOT NULL CHECK (max_downloads > 0),
    download_count integer NOT NULL DEFAULT 0 CHECK (download_count >= 0),
    revoked_at timestamptz,
    lifecycle_state text NOT NULL DEFAULT 'active'
        CHECK (lifecycle_state IN ('active', 'revoked', 'expired', 'limit_reached', 'deleted')),
    cleanup_status text NOT NULL DEFAULT 'pending'
        CHECK (cleanup_status IN ('pending', 'queued', 'deleted', 'already_missing', 'failed', 'retained_shared')),
    cleanup_at timestamptz,
    legacy_metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT shares_owner_hash_format CHECK (
        owner_hash IS NULL OR owner_hash ~ '^[0-9a-f]{64}$'
    )
);
CREATE INDEX shares_owner_created_idx ON shares (owner_user_id, created_at DESC);
CREATE INDEX shares_file_idx ON shares (file_id);
CREATE INDEX shares_expiry_idx ON shares (expires_at)
    WHERE revoked_at IS NULL AND lifecycle_state <> 'deleted';
CREATE INDEX shares_owner_hash_idx ON shares (owner_hash) WHERE owner_hash IS NOT NULL;

CREATE TABLE download_events (
    event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    share_pk uuid NOT NULL REFERENCES shares(share_pk) ON DELETE RESTRICT,
    occurred_at timestamptz NOT NULL DEFAULT now(),
    outcome text NOT NULL CHECK (outcome IN ('success', 'rejected', 'system')),
    event_type text NOT NULL,
    idempotency_key char(64) UNIQUE,
    details jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX download_events_share_time_idx ON download_events (share_pk, occurred_at DESC);
CREATE INDEX download_events_time_idx ON download_events (occurred_at DESC);

CREATE TABLE upload_sessions (
    upload_session_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_user_id uuid NOT NULL REFERENCES users(user_id) ON DELETE RESTRICT,
    file_id uuid REFERENCES files(file_id) ON DELETE RESTRICT,
    proposed_share_id varchar(32) NOT NULL UNIQUE CHECK (proposed_share_id ~ '^[a-f0-9]{32}$'),
    idempotency_key varchar(128) NOT NULL,
    storage_provider text NOT NULL CHECK (storage_provider IN ('google_drive', 'github')),
    state text NOT NULL DEFAULT 'initiated'
        CHECK (state IN ('initiated', 'uploading', 'finalizing', 'complete', 'aborted', 'expired', 'failed')),
    expected_bytes bigint NOT NULL CHECK (expected_bytes > 0),
    received_bytes bigint NOT NULL DEFAULT 0 CHECK (received_bytes >= 0 AND received_bytes <= expected_bytes),
    provider_session_uri_ciphertext bytea,
    expires_at timestamptz NOT NULL,
    completed_at timestamptz,
    last_error_code text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (owner_user_id, idempotency_key)
);
CREATE INDEX upload_sessions_state_expiry_idx ON upload_sessions (state, expires_at)
    WHERE state IN ('initiated', 'uploading', 'finalizing');

CREATE TABLE transfer_permissions (
    permission_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email_normalized text NOT NULL CHECK (email_normalized = lower(btrim(email_normalized))),
    max_file_size_bytes bigint NOT NULL DEFAULT 53687091200
        CHECK (max_file_size_bytes > 5368709120 AND max_file_size_bytes <= 53687091200),
    granted_by_user_id uuid REFERENCES users(user_id) ON DELETE SET NULL,
    granted_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX transfer_permissions_active_email_uq
    ON transfer_permissions (email_normalized)
    WHERE revoked_at IS NULL;

CREATE TABLE cleanup_jobs (
    job_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    file_id uuid NOT NULL REFERENCES files(file_id) ON DELETE RESTRICT,
    job_type text NOT NULL DEFAULT 'delete_storage_object' CHECK (job_type IN ('delete_storage_object')),
    status text NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'retry', 'completed', 'cancelled', 'failed')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts integer NOT NULL DEFAULT 8 CHECK (max_attempts > 0),
    available_at timestamptz NOT NULL DEFAULT now(),
    lease_expires_at timestamptz,
    last_error_code text,
    last_error_summary text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz
);
CREATE UNIQUE INDEX cleanup_jobs_one_open_job_per_file_uq
    ON cleanup_jobs (file_id, job_type)
    WHERE status IN ('queued', 'running', 'retry');
CREATE INDEX cleanup_jobs_ready_idx ON cleanup_jobs (status, available_at, created_at)
    WHERE status IN ('queued', 'retry', 'running');

CREATE TABLE audit_logs (
    audit_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    actor_user_id uuid REFERENCES users(user_id) ON DELETE SET NULL,
    action text NOT NULL,
    target_type text NOT NULL,
    target_id text,
    occurred_at timestamptz NOT NULL DEFAULT now(),
    details jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX audit_logs_time_idx ON audit_logs (occurred_at DESC);
CREATE INDEX audit_logs_actor_time_idx ON audit_logs (actor_user_id, occurred_at DESC);
