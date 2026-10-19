"""Private, resumable download data plane for QuickShare Cloud.

Run this on a persistent VPS, not a Vercel Function. The Vercel app issues a
short-lived signed ticket only after validating share expiry and download count.
"""
import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv
import requests
from flask import Flask, Response, abort, make_response, request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from services import google_drive_storage
from services.storage_errors import StorageError

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

TOKEN_SECRET = os.getenv("TRANSFER_TOKEN_SECRET", "").strip()
TOKEN_MAX_AGE = max(60, int(os.getenv("TRANSFER_TICKET_MAX_AGE_SECONDS", "172800")))
TRANSFER_WEB_BASE_URL = os.getenv("TRANSFER_WEB_BASE_URL", "https://qshare.darshanup.online").strip().rstrip("/")
DB_PATH = Path(os.getenv("TRANSFER_DB_PATH", str(BASE_DIR / "transfer_sessions.sqlite3"))).resolve()
MAX_FILE_BYTES = 50 * 1024**3
COOKIE_PREFIX = "qsdl_"
app = Flask("quickshare_transfer")
serializer = URLSafeTimedSerializer(TOKEN_SECRET, salt="quickshare-transfer-v1") if TOKEN_SECRET else None


def _connect_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS download_tickets (
            ticket_id TEXT PRIMARY KEY,
            cookie_hash TEXT NOT NULL,
            expires_at INTEGER NOT NULL,
            ranges_json TEXT NOT NULL DEFAULT '[]',
            completed INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL
        )"""
    )
    conn.commit()
    return conn


def _cookie_name(ticket_id: str) -> str:
    return COOKIE_PREFIX + ticket_id


def _cookie_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _parse_range(value: str, size: int):
    """Return a normalized single byte range or raise ValueError."""
    if not value:
        return 0, size - 1, False
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value.strip())
    if not match or (not match.group(1) and not match.group(2)):
        raise ValueError("Only a single byte range is supported.")
    first, last = match.groups()
    if not first:
        suffix = int(last)
        if suffix <= 0:
            raise ValueError("Invalid suffix range.")
        start = max(0, size - suffix)
        end = size - 1
    else:
        start = int(first)
        end = int(last) if last else size - 1
    if start >= size or end < start:
        raise ValueError("Range is outside the file.")
    return start, min(end, size - 1), True


def _validate_with_web(token: str):
    """Re-check expiry/revocation in the main app for each initial or ranged download request."""
    if not TRANSFER_WEB_BASE_URL:
        abort(503, description="The QuickShare validation endpoint is not configured.")
    try:
        response = requests.post(
            f"{TRANSFER_WEB_BASE_URL}/api/transfer/validate",
            json={"ticket": token},
            timeout=15,
        )
    except requests.RequestException:
        app.logger.exception("Could not validate large-download ticket against QuickShare.")
        abort(503, description="QuickShare could not validate this download. Retry shortly.")
    if response.status_code == 410:
        abort(410, description="This share has expired or was revoked.")
    if response.status_code in (403, 404):
        abort(response.status_code, description="This transfer is no longer authorized.")
    if not response.ok:
        abort(503, description="QuickShare could not validate this download. Retry shortly.")


def _ticket_data(token: str) -> dict:
    if not serializer:
        abort(503, description="The large-file transfer service is not configured.")
    try:
        payload = serializer.loads(token, max_age=TOKEN_MAX_AGE)
    except SignatureExpired:
        abort(410, description="This download ticket expired. Reopen the original QuickShare link.")
    except BadSignature:
        abort(403, description="Invalid download ticket.")
    if not isinstance(payload, dict):
        abort(403, description="Invalid download ticket.")
    ticket_id = payload.get("jti", "")
    try:
        size = int(payload.get("size", 0))
    except (TypeError, ValueError):
        size = 0
    if (
        not isinstance(ticket_id, str)
        or not re.fullmatch(r"[a-f0-9]{32}", ticket_id)
        or not payload.get("file_id")
        or size < 1
        or size > MAX_FILE_BYTES
        or not payload.get("filename")
    ):
        abort(403, description="Invalid download ticket.")
    return payload


def _read_ticket(ticket_id: str):
    with _connect_db() as conn:
        row = conn.execute(
            "SELECT cookie_hash, expires_at, ranges_json, completed FROM download_tickets WHERE ticket_id = ?",
            (ticket_id,),
        ).fetchone()
    return row


def _claim_ticket(ticket_id: str, expires_at: int):
    cookie_secret = secrets.token_urlsafe(32)
    with _connect_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO download_tickets "
            "(ticket_id, cookie_hash, expires_at, ranges_json, completed, created_at) "
            "VALUES (?, ?, ?, '[]', 0, ?)",
            (ticket_id, _cookie_digest(cookie_secret), expires_at, int(time.time())),
        )
        row = conn.execute(
            "SELECT cookie_hash, expires_at, ranges_json, completed FROM download_tickets WHERE ticket_id = ?",
            (ticket_id,),
        ).fetchone()
    if row and secrets.compare_digest(row[0], _cookie_digest(cookie_secret)):
        return cookie_secret, row
    return "", row


def _unclaim_ticket_if_new(ticket_id: str, cookie_secret: str):
    """Allow a retry if Drive rejected the initial stream before any bytes were sent."""
    if not cookie_secret:
        return
    with _connect_db() as conn:
        conn.execute(
            "DELETE FROM download_tickets WHERE ticket_id = ? AND cookie_hash = ? "
            "AND ranges_json = '[]' AND completed = 0",
            (ticket_id, _cookie_digest(cookie_secret)),
        )


def _ticket_allowed(ticket_id: str, expires_at: int):
    row = _read_ticket(ticket_id)
    if row is None:
        cookie_secret, row = _claim_ticket(ticket_id, expires_at)
        if not cookie_secret:
            abort(409, description="This download ticket is already in use.")
        return cookie_secret, True, row
    cookie_name = _cookie_name(ticket_id)
    cookie_secret = request.cookies.get(cookie_name, "")
    if not cookie_secret or not secrets.compare_digest(row[0], _cookie_digest(cookie_secret)):
        abort(403, description="This download ticket was already claimed in another browser.")
    if int(row[1]) < int(time.time()):
        abort(410, description="This download ticket expired. Reopen the original QuickShare link.")
    if row[3]:
        abort(410, description="This download was already completed. Reopen the QuickShare link for another allowed download.")
    return cookie_secret, False, row


def _merge_range_ranges(existing_json: str, start: int, end: int):
    try:
        ranges = json.loads(existing_json)
        if not isinstance(ranges, list):
            ranges = []
    except (TypeError, ValueError):
        ranges = []
    ranges.append([start, end])
    ranges.sort(key=lambda item: item[0])
    merged = []
    for left, right in ranges:
        if not isinstance(left, int) or not isinstance(right, int):
            continue
        if not merged or left > merged[-1][1] + 1:
            merged.append([left, right])
        else:
            merged[-1][1] = max(merged[-1][1], right)
    return merged


def _record_range(ticket_id: str, expected_cookie_hash: str, start: int, end: int, total_size: int):
    with _connect_db() as conn:
        row = conn.execute(
            "SELECT cookie_hash, expires_at, ranges_json, completed FROM download_tickets WHERE ticket_id = ?",
            (ticket_id,),
        ).fetchone()
        if not row or not secrets.compare_digest(row[0], expected_cookie_hash):
            return
        merged = _merge_range_ranges(row[2], start, end)
        complete = any(left == 0 and right >= total_size - 1 for left, right in merged)
        conn.execute(
            "UPDATE download_tickets SET ranges_json = ?, completed = ? WHERE ticket_id = ?",
            (json.dumps(merged), 1 if complete else 0, ticket_id),
        )


@app.get("/health")
def health():
    if not serializer or not google_drive_storage.is_configured():
        return {"status": "not_ready"}, 503
    _connect_db().close()
    return {"status": "ok", "service": "QuickShare transfer"}, 200


@app.get("/download/<token>")
def download_from_drive(token: str):
    payload = _ticket_data(token)
    _validate_with_web(token)
    ticket_id = payload["jti"]
    size = int(payload["size"])
    try:
        start, end, is_range = _parse_range(request.headers.get("Range", ""), size)
    except (ValueError, TypeError):
        response = make_response("", 416)
        response.headers["Content-Range"] = f"bytes */{size}"
        response.headers["Accept-Ranges"] = "bytes"
        return response

    cookie_secret, is_new, row = _ticket_allowed(
        ticket_id, int(payload.get("exp_at", int(time.time()) + TOKEN_MAX_AGE))
    )
    if row[3]:
        abort(410, description="This download was already completed.")

    range_header = f"bytes={start}-{end}" if is_range else ""
    try:
        upstream = google_drive_storage.open_private_file_stream(payload["file_id"], range_header)
    except StorageError:
        if is_new:
            _unclaim_ticket_if_new(ticket_id, cookie_secret)
        app.logger.exception("Could not open Drive stream for ticket %s", ticket_id)
        abort(502, description="Google Drive could not open the file. Try again from the share page.")

    if is_range and upstream.status_code != 206:
        upstream.close()
        if is_new:
            _unclaim_ticket_if_new(ticket_id, cookie_secret)
        abort(502, description="Google Drive did not honor the requested byte range.")
    upstream_range = upstream.headers.get("Content-Range", "")
    if upstream_range:
        match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", upstream_range)
        if not match:
            upstream.close()
            if is_new:
                _unclaim_ticket_if_new(ticket_id, cookie_secret)
            abort(502, description="Google Drive returned an invalid byte range.")
        actual_start, actual_end, actual_total = map(int, match.groups())
        if actual_total != size or actual_start != start or actual_end > end:
            upstream.close()
            if is_new:
                _unclaim_ticket_if_new(ticket_id, cookie_secret)
            abort(502, description="Google Drive returned an unexpected byte range.")
        start, end = actual_start, actual_end
    elif not is_range:
        start, end = 0, size - 1

    content_length = int(upstream.headers.get("Content-Length", end - start + 1))
    if content_length != end - start + 1:
        upstream.close()
        if is_new:
            _unclaim_ticket_if_new(ticket_id, cookie_secret)
        abort(502, description="Google Drive returned an incomplete file segment.")

    filename = "".join(ch for ch in str(payload["filename"]) if ch not in "\r\n\0")
    fallback = re.sub(r"[^A-Za-z0-9._ -]", "_", filename).strip() or "download.bin"
    disposition = f'attachment; filename="{fallback.replace(chr(34), "_")}"; filename*=UTF-8\'\'{quote(filename)}'
    expected_cookie_hash = _cookie_digest(cookie_secret)

    def body():
        sent = 0
        try:
            for piece in upstream.iter_content(chunk_size=1024 * 1024):
                if not piece:
                    continue
                sent += len(piece)
                yield piece
        finally:
            upstream.close()
            if sent == content_length:
                _record_range(ticket_id, expected_cookie_hash, start, end, size)

    response = Response(
        body(),
        status=206 if is_range else upstream.status_code,
        mimetype=upstream.headers.get("Content-Type", "application/octet-stream"),
        direct_passthrough=True,
    )
    response.headers["Content-Length"] = str(content_length)
    response.headers["Content-Disposition"] = disposition
    response.headers["Accept-Ranges"] = "bytes"
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Accel-Buffering"] = "no"
    response.headers["X-Content-Type-Options"] = "nosniff"
    if is_range:
        response.headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    if is_new:
        response.set_cookie(
            _cookie_name(ticket_id),
            cookie_secret,
            max_age=TOKEN_MAX_AGE,
            httponly=True,
            secure=True,
            samesite="Lax",
            path="/",
        )
    return response


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8081")))
