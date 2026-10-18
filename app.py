import hashlib
import hmac
import mimetypes
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit

import qrcode
from authlib.integrations.flask_client import OAuth
from dotenv import load_dotenv
from flask import (
    Flask, abort, flash, jsonify, redirect, render_template, request,
    send_file, session, url_for,
)

from services.github_storage import (
    GitHubStorageError,
    cleanup_expired_files,
    create_share_metadata,
    download_public_file,
    get_share_analytics,
    get_share_metadata,
    list_user_shares,
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
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=7)
# Vercel request/response payloads are limited; cap uploads at 3 MiB.
app.config["MAX_CONTENT_LENGTH"] = max(1, min(3, int(os.getenv("MAX_CONTENT_LENGTH_MB", "3")))) * 1024 * 1024

oauth = OAuth(app)
oauth.register(
    name="google",
    client_id=os.getenv("GOOGLE_CLIENT_ID", ""),
    client_secret=os.getenv("GOOGLE_CLIENT_SECRET", ""),
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

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
    ".lnk", ".html", ".htm", ".js", ".mjs", ".svg", ".xhtml", ".mhtml", ".wasm",
}


def _safe_next_url(value: str | None) -> str | None:
    if not value or not isinstance(value, str):
        return None
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or not value.startswith("/") or value.startswith("//"):
        return None
    return value


def _current_user() -> dict | None:
    user = session.get("user")
    if not isinstance(user, dict) or not user.get("sub") or not user.get("email"):
        return None
    return user


def _current_owner_hash() -> str | None:
    user = _current_user()
    if not user:
        return None
    key = str(os.getenv("OWNER_HASH_SECRET") or app.config["SECRET_KEY"]).encode("utf-8")
    subject = str(user["sub"]).encode("utf-8")
    return hmac.new(key, subject, hashlib.sha256).hexdigest()


def _csrf_token() -> str:
    token = session.get("_csrf_token")
    if not isinstance(token, str) or len(token) < 32:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


app.jinja_env.globals["csrf_token"] = _csrf_token


@app.before_request
def enforce_csrf_for_state_changes():
    if request.method != "POST" or not _current_user():
        return None
    expected = session.get("_csrf_token", "")
    supplied = request.form.get("csrf_token", "") or request.headers.get("X-CSRF-Token", "")
    if not expected or not supplied or not hmac.compare_digest(str(expected), str(supplied)):
        abort(400, description="The request security token is missing or invalid. Refresh the page and try again.")
    return None


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not _current_user():
            if request.path.startswith("/api/"):
                return jsonify({"error": "Sign in with Google to access this feature."}), 401
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


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
    }


def _is_current_user_owner(metadata: dict) -> bool:
    owner_hash = _current_owner_hash()
    stored_hash = metadata.get("owner_id_hash", "")
    return bool(
        owner_hash
        and isinstance(stored_hash, str)
        and stored_hash
        and hmac.compare_digest(owner_hash, stored_hash)
    )


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


@app.get("/login")
def login():
    if _current_user():
        return redirect(url_for("my_shares"))
    if not os.getenv("GOOGLE_CLIENT_ID") or not os.getenv("GOOGLE_CLIENT_SECRET"):
        flash("Google sign-in is not configured yet. The site owner must add Google OAuth credentials in Vercel.", "error")
        return redirect(url_for("index"))
    next_url = _safe_next_url(request.args.get("next"))
    session["after_login"] = next_url or url_for("my_shares")
    redirect_uri = os.getenv("GOOGLE_REDIRECT_URI", "").strip() or url_for("google_callback", _external=True)
    return oauth.google.authorize_redirect(redirect_uri, prompt="select_account")


@app.get("/auth/google/callback")
def google_callback():
    if not os.getenv("GOOGLE_CLIENT_ID") or not os.getenv("GOOGLE_CLIENT_SECRET"):
        flash("Google sign-in is not configured.", "error")
        return redirect(url_for("index"))
    try:
        token = oauth.google.authorize_access_token()
        userinfo = token.get("userinfo")
        if not userinfo:
            userinfo = oauth.google.userinfo(token=token)
        subject = str(userinfo.get("sub", "")).strip()
        email = str(userinfo.get("email", "")).strip().lower()
        verified = userinfo.get("email_verified") is True
        if not subject or not email or not verified:
            flash("Google did not return a verified email address. Please try a verified Google account.", "error")
            return redirect(url_for("index"))
        next_url = _safe_next_url(session.get("after_login")) or url_for("my_shares")
        user = {
            "sub": subject,
            "email": email,
            "name": str(userinfo.get("name") or email.split("@")[0])[:120],
            "picture": str(userinfo.get("picture") or "")[:1000],
        }
        # Replace the pre-auth session to reduce session-fixation risk.
        session.clear()
        session["user"] = user
        flash(f"Signed in as {email}.", "success")
        return redirect(next_url)
    except Exception:
        app.logger.warning("Google sign-in failed", exc_info=True)
        flash("Google sign-in could not be completed. Please try again.", "error")
        return redirect(url_for("index"))


@app.get("/logout")
def logout():
    session.clear()
    flash("You have been signed out.", "success")
    return redirect(url_for("index"))


@app.get("/my-shares")
@login_required
def my_shares():
    return render_template("my_shares.html")


@app.get("/health")
def health():
    return {"status": "ok", "service": "QuickShare Cloud"}


@app.post("/upload")
@login_required
def upload():
    uploaded = request.files.get("file")
    if uploaded is None or not uploaded.filename:
        flash("Choose a file before uploading.", "error")
        return redirect(url_for("index"))

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

    user = _current_user()
    owner_id_hash = _current_owner_hash()
    if not user or not owner_id_hash:
        return redirect(url_for("login"))

    share_id = secrets.token_hex(16)
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
        "owner_id_hash": owner_id_hash,
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

    flash(f"File uploaded. Sharing link expires in {label}.", "success")
    return redirect(url_for("share_page", share_id=share_id), code=303)


@app.get("/s/<share_id>")
def share_page(share_id: str):
    if not SHARE_ID_PATTERN.fullmatch(share_id):
        abort(404)
    file_record = get_share_metadata(share_id)
    if not file_record:
        abort(404)
    return render_template(
        "share.html",
        file=file_record,
        status=share_status(file_record),
        error=request.args.get("error", ""),
        share_url=make_share_url(share_id),
        is_owner=_is_current_user_owner(file_record),
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


@app.get("/api/my-shares")
@login_required
def api_my_shares():
    try:
        shares = list_user_shares(_current_owner_hash() or "")
    except GitHubStorageError as exc:
        app.logger.warning("Could not list account shares: %s", exc)
        return jsonify({"error": "Share list is temporarily unavailable. Refresh in a moment."}), 503
    return jsonify({"shares": shares})


@app.post("/api/shares/<share_id>/analytics")
@login_required
def api_share_analytics(share_id: str):
    if not SHARE_ID_PATTERN.fullmatch(share_id):
        abort(404)
    result, analytics = get_share_analytics(share_id, _current_owner_hash() or "")
    if result == "not_found":
        return jsonify({"error": "Share not found"}), 404
    if result == "unauthorized":
        return jsonify({"error": "This share belongs to another account."}), 403
    return jsonify(analytics)


@app.post("/api/shares/<share_id>/revoke")
@login_required
def api_revoke_share(share_id: str):
    if not SHARE_ID_PATTERN.fullmatch(share_id):
        abort(404)
    result = revoke_share(share_id, _current_owner_hash() or "")
    if result == "not_found":
        return jsonify({"error": "Share not found"}), 404
    if result == "unauthorized":
        return jsonify({"error": "This share belongs to another account."}), 403
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
