import hashlib
import hmac
import mimetypes
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

import qrcode
from dotenv import load_dotenv
from flask import Flask, abort, flash, jsonify, redirect, render_template, request, send_file, session, url_for

from services.github_storage import (
    GitHubStorageError,
    cleanup_expired_files,
    create_share_metadata,
    download_public_file,
    get_share_analytics,
    get_share_metadata,
    record_download,
    record_rejected_attempt,
    revoke_share,
    share_status as calculate_share_status,
    upload_file_bytes,
)

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "local-development-only-change-me")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("VERCEL", "") == "1"
# Vercel Function payloads are limited; cap application uploads at 3 MiB.
app.config["MAX_CONTENT_LENGTH"] = max(1, min(3, int(os.getenv("MAX_CONTENT_LENGTH_MB", "3")))) * 1024 * 1024

EXPIRATION_OPTIONS = {
    "10m": ("10 minutes", timedelta(minutes=10)),
    "1h": ("1 hour", timedelta(hours=1)),
    "24h": ("24 hours", timedelta(hours=24)),
    "7d": ("7 days", timedelta(days=7)),
}
DOWNLOAD_LIMITS = {1, 3, 5, 10}
SHARE_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")
EXTENSION_PATTERN = re.compile(r"^\.[a-zA-Z0-9]{1,10}$")
BLOCKED_EXTENSIONS = {
    ".exe", ".dll", ".sys", ".bat", ".cmd", ".ps1", ".sh", ".msi",
    ".com", ".scr", ".vbs", ".vbe", ".hta", ".jar", ".apk", ".reg",
    ".lnk", ".html", ".htm", ".js", ".mjs",
}


def make_share_url(share_id: str) -> str:
    configured_base = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    base = configured_base or request.url_root.rstrip("/")
    return f"{base}{url_for('share_page', share_id=share_id)}"


def share_status(file_record: dict) -> str:
    try:
        return calculate_share_status(file_record)
    except GitHubStorageError:
        return "invalid"


def _public_share_data(metadata: dict) -> dict:
    return {
        "share_id": metadata.get("share_id"),
        "original_filename": metadata.get("original_filename", "Unnamed file"),
        "file_size": int(metadata.get("file_size", 0)),
        "upload_time": metadata.get("upload_time", ""),
        "expiry_time": metadata.get("expiry_time", ""),
        "max_downloads": int(metadata.get("max_downloads", 0)),
        "download_count": int(metadata.get("download_count", 0)),
        "status": share_status(metadata),
        "revoked_at": metadata.get("revoked_at"),
        "cleanup_status": metadata.get("cleanup_status", "pending"),
    }


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")
    if request.path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "public, max-age=3600")
    else:
        response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.get("/")
def index():
    return render_template("index.html", storage_repo=os.getenv("GITHUB_REPO", "QuickShare-Storage"))


@app.get("/my-shares")
def my_shares():
    return render_template("my_shares.html")


@app.get("/health")
def health():
    return {"status": "ok", "service": "QuickShare Cloud"}


@app.post("/upload")
def upload():
    uploaded = request.files.get("file")
    if uploaded is None or not uploaded.filename:
        flash("Choose a file before uploading.", "error")
        return redirect(url_for("index"))

    # Only small, non-sensitive demo files should ever enter the public repo.
    original_name = uploaded.filename.replace("\\", "/").split("/")[-1].strip()[:200]
    if not original_name:
        flash("The uploaded filename is invalid.", "error")
        return redirect(url_for("index"))

    suffix = Path(original_name).suffix.lower()
    if suffix in BLOCKED_EXTENSIONS:
        flash("That file type is not accepted. Upload a document, image, archive, or other non-executable demo file.", "error")
        return redirect(url_for("index"))
    if suffix and not EXTENSION_PATTERN.fullmatch(suffix):
        flash("The file extension is invalid.", "error")
        return redirect(url_for("index"))

    expiry_choice = request.form.get("expiry", "24h")
    if expiry_choice not in EXPIRATION_OPTIONS:
        flash("Choose a valid expiration period.", "error")
        return redirect(url_for("index"))

    try:
        max_downloads = int(request.form.get("max_downloads", "3"))
    except (TypeError, ValueError):
        max_downloads = 0
    if max_downloads not in DOWNLOAD_LIMITS:
        flash("Choose a valid download limit.", "error")
        return redirect(url_for("index"))

    content = uploaded.read()
    if not content:
        flash("The selected file is empty.", "error")
        return redirect(url_for("index"))
    if len(content) > app.config["MAX_CONTENT_LENGTH"]:
        flash("The selected file is too large. The maximum is 3 MB.", "error")
        return redirect(url_for("index"))

    share_id = secrets.token_hex(16)
    owner_token = secrets.token_urlsafe(32)
    storage_path = f"files/{share_id}{suffix or '.bin'}"
    label, duration = EXPIRATION_OPTIONS[expiry_choice]
    now = datetime.now(timezone.utc)
    metadata = {
        "share_id": share_id,
        "original_filename": original_name,
        "storage_path": storage_path,
        "file_size": len(content),
        "upload_time": now.isoformat(timespec="seconds"),
        "expiry_time": (now + duration).isoformat(timespec="seconds"),
        "max_downloads": max_downloads,
        "download_count": 0,
        "downloads": [],
        "owner_token_hash": hashlib.sha256(owner_token.encode("utf-8")).hexdigest(),
        "revoked": False,
        "cleanup_status": "pending",
    }

    try:
        upload_file_bytes(storage_path=storage_path, content=content, share_id=share_id)
        create_share_metadata(metadata)
    except GitHubStorageError as exc:
        app.logger.warning("GitHub upload failed: %s", exc)
        flash(f"Upload to GitHub failed: {exc}", "error")
        return redirect(url_for("index"))
    except Exception:
        app.logger.exception("Could not save share metadata")
        flash("The file may have reached GitHub, but its metadata could not be saved. Check the storage repository before retrying.", "error")
        return redirect(url_for("index"))

    # Deliver the one-time owner token to the browser through a short redirect
    # session. It is never placed in the public share URL.
    session["quickshare_created"] = {"share_id": share_id, "owner_token": owner_token}
    flash(f"File uploaded. Sharing link expires in {label}.", "success")
    return redirect(url_for("share_page", share_id=share_id), code=303)


@app.get("/s/<share_id>")
def share_page(share_id: str):
    if not SHARE_ID_PATTERN.fullmatch(share_id):
        abort(404)
    file_record = get_share_metadata(share_id)
    if not file_record:
        abort(404)
    created = session.get("quickshare_created", {})
    owner_token = None
    if created.get("share_id") == share_id:
        owner_token = created.get("owner_token")
        session.pop("quickshare_created", None)
    status = share_status(file_record)
    error = request.args.get("error", "")
    return render_template(
        "share.html",
        file=file_record,
        status=status,
        error=error,
        share_url=make_share_url(share_id),
        owner_token=owner_token,
    )


@app.get("/qr/<share_id>.png")
def qr_image(share_id: str):
    if not SHARE_ID_PATTERN.fullmatch(share_id) or not get_share_metadata(share_id):
        abort(404)
    image = qrcode.make(make_share_url(share_id))
    output = BytesIO()
    image.save(output, format="PNG")
    output.seek(0)
    return send_file(output, mimetype="image/png", download_name=f"quickshare-{share_id}.png")


@app.get("/download/<share_id>")
def download(share_id: str):
    if not SHARE_ID_PATTERN.fullmatch(share_id):
        abort(404)
    file_record = get_share_metadata(share_id)
    if not file_record:
        abort(404)

    status = share_status(file_record)
    if status != "active":
        record_rejected_attempt(share_id, status)
        return redirect(url_for("share_page", share_id=share_id, error=status))

    try:
        content = download_public_file(file_record["storage_path"])
        result = record_download(share_id)
    except GitHubStorageError as exc:
        app.logger.warning("GitHub download failed for share %s: %s", share_id, exc)
        return redirect(url_for("share_page", share_id=share_id, error="storage"))

    if result != "success":
        return redirect(url_for("share_page", share_id=share_id, error=result))

    mimetype = mimetypes.guess_type(file_record["original_filename"])[0] or "application/octet-stream"
    response = send_file(
        BytesIO(content),
        mimetype=mimetype,
        as_attachment=True,
        download_name=file_record["original_filename"],
        max_age=0,
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/shares/<share_id>")
def api_share(share_id: str):
    if not SHARE_ID_PATTERN.fullmatch(share_id):
        abort(404)
    metadata = get_share_metadata(share_id)
    if not metadata:
        abort(404)
    return jsonify(_public_share_data(metadata))


@app.post("/api/shares/<share_id>/analytics")
def api_share_analytics(share_id: str):
    if not SHARE_ID_PATTERN.fullmatch(share_id):
        abort(404)
    body = request.get_json(silent=True) or {}
    result, analytics = get_share_analytics(share_id, body.get("owner_token", ""))
    if result == "not_found":
        return jsonify({"error": "Share not found"}), 404
    if result == "unauthorized":
        return jsonify({"error": "Management token is invalid"}), 403
    return jsonify(analytics)


@app.post("/api/shares/<share_id>/revoke")
def api_revoke_share(share_id: str):
    if not SHARE_ID_PATTERN.fullmatch(share_id):
        abort(404)
    body = request.get_json(silent=True) or {}
    result = revoke_share(share_id, body.get("owner_token", ""))
    if result == "not_found":
        return jsonify({"error": "Share not found"}), 404
    if result == "unauthorized":
        return jsonify({"error": "Management token is invalid"}), 403
    if result == "conflict":
        return jsonify({"error": "The share changed concurrently. Please retry."}), 409
    if result in {"success", "revoked"}:
        return jsonify({"ok": True, "status": "revoked"})
    return jsonify({"error": "Could not revoke the share"}), 503


@app.get("/api/cron/cleanup")
def api_cron_cleanup():
    expected = os.getenv("CRON_SECRET", "")
    supplied = request.headers.get("Authorization", "")
    if not expected:
        return jsonify({"error": "Cleanup is not configured"}), 503
    if not hmac.compare_digest(supplied, f"Bearer {expected}"):
        return jsonify({"error": "Unauthorized"}), 401
    try:
        return jsonify({"ok": True, **cleanup_expired_files(max_items=20)})
    except GitHubStorageError as exc:
        app.logger.warning("Scheduled cleanup failed: %s", exc)
        return jsonify({"error": str(exc)}), 503


@app.errorhandler(413)
def file_too_large(_error):
    flash("File is too large. The maximum upload size is capped at 3 MB.", "error")
    return redirect(url_for("index"))


@app.errorhandler(404)
def not_found(_error):
    return render_template("error.html", title="Share not found", message="This sharing link does not exist or is no longer available."), 404


@app.errorhandler(GitHubStorageError)
def github_storage_error(error):
    app.logger.warning("GitHub storage service failed: %s", error)
    return render_template(
        "error.html",
        title="Storage temporarily unavailable",
        message="QuickShare could not read its GitHub storage metadata. Check the repository configuration and try again.",
    ), 503


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=os.getenv("FLASK_DEBUG", "0") == "1")
