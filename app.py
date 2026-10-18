import mimetypes
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

import qrcode
from dotenv import load_dotenv
from flask import Flask, abort, flash, redirect, render_template, request, send_file, url_for

from services.github_storage import (
    GitHubStorageError,
    create_share_metadata,
    download_public_file,
    get_share_metadata,
    record_download,
    record_rejected_attempt,
    upload_file_bytes,
)

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "local-development-only-change-me")
# Vercel Functions accept at most 4.5 MB request/response payloads. A 3 MB cap
# leaves headroom for multipart form overhead and file-download responses.
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


def make_share_url(share_id: str) -> str:
    configured_base = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    base = configured_base or request.url_root.rstrip("/")
    return f"{base}{url_for('share_page', share_id=share_id)}"


def share_status(file_record: dict) -> str:
    try:
        expiry = datetime.fromisoformat(file_record["expiry_time"])
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if expiry <= datetime.now(timezone.utc):
            return "expired"
        if int(file_record["download_count"]) >= int(file_record["max_downloads"]):
            return "limit"
        return "active"
    except (KeyError, TypeError, ValueError):
        return "invalid"


@app.get("/")
def index():
    return render_template("index.html", storage_repo=os.getenv("GITHUB_REPO", "QuickShare-Storage"))


@app.get("/health")
def health():
    return {"status": "ok", "service": "QuickShare Cloud"}


@app.post("/upload")
def upload():
    uploaded = request.files.get("file")
    if uploaded is None or not uploaded.filename:
        flash("Choose a file before uploading.", "error")
        return redirect(url_for("index"))

    # This public repository is for small, non-sensitive demo files only.
    original_name = uploaded.filename.replace("\\", "/").split("/")[-1].strip()[:200]
    if not original_name:
        flash("The uploaded filename is invalid.", "error")
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

    share_id = secrets.token_hex(16)
    suffix = Path(original_name).suffix
    if not EXTENSION_PATTERN.fullmatch(suffix):
        suffix = ".bin"
    storage_path = f"files/{share_id}{suffix.lower()}"
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

    flash(f"File uploaded. Sharing link expires in {label}.", "success")
    return redirect(url_for("share_page", share_id=share_id))


@app.get("/s/<share_id>")
def share_page(share_id: str):
    if not SHARE_ID_PATTERN.fullmatch(share_id):
        abort(404)
    file_record = get_share_metadata(share_id)
    if not file_record:
        abort(404)
    status = share_status(file_record)
    error = request.args.get("error", "")
    return render_template(
        "share.html",
        file=file_record,
        status=status,
        error=error,
        share_url=make_share_url(share_id),
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
        # The GitHub metadata SHA is checked and updated, retrying stale updates.
        # This suits a low-traffic college prototype, not a transactional production system.
        result = record_download(share_id)
    except GitHubStorageError as exc:
        app.logger.warning("GitHub download failed for share %s: %s", share_id, exc)
        return redirect(url_for("share_page", share_id=share_id, error="storage"))

    if result != "success":
        return redirect(url_for("share_page", share_id=share_id, error=result))

    mimetype = mimetypes.guess_type(file_record["original_filename"])[0] or "application/octet-stream"
    return send_file(
        BytesIO(content),
        mimetype=mimetype,
        as_attachment=True,
        download_name=file_record["original_filename"],
        max_age=0,
    )


@app.errorhandler(413)
def file_too_large(_error):
    flash("File is too large. The maximum upload size is configured in .env and capped at 3 MB for Vercel.", "error")
    return redirect(url_for("index"))


@app.errorhandler(404)
def not_found(_error):
    return render_template("error.html", title="Share not found", message="This sharing link does not exist or is no longer available."), 404


@app.errorhandler(GitHubStorageError)
def github_storage_error(_error):
    app.logger.exception("GitHub storage service failed")
    return render_template(
        "error.html",
        title="Storage temporarily unavailable",
        message="QuickShare could not read its GitHub storage metadata. Check the repository configuration and try again.",
    ), 503


if __name__ == "__main__":
    # Local development only. Vercel imports the Flask app instance.
    app.run(host="127.0.0.1", port=5000, debug=os.getenv("FLASK_DEBUG", "0") == "1")
