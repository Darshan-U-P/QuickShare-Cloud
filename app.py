import hashlib
import hmac
import mimetypes
import os
import re
import secrets
import time
from threading import Lock
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
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from services import google_drive_storage
from services.github_storage import (
    cleanup_expired_files,
    create_share_metadata,
    delete_storage_file as delete_github_file,
    download_public_file,
    get_share_analytics,
    get_share_metadata,
    list_user_shares,
    record_download,
    record_rejected_attempt,
    revoke_share,
    share_status as calculate_share_status,
    upload_file_bytes as upload_github_file_bytes,
)
from services.storage_errors import StorageError

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "local-development-only-change-me")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("VERCEL", "") == "1"
_session_cookie_domain = os.getenv("SESSION_COOKIE_DOMAIN", "").strip()
if not _session_cookie_domain and os.getenv("VERCEL_ENV", "").strip().lower() == "production":
    _session_cookie_domain = ".darshanup.online"
app.config["SESSION_COOKIE_DOMAIN"] = _session_cookie_domain or None
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=7)
# Vercel Function request and response payloads are limited to 4.5 MB; cap uploads at 4 MiB.
app.config["MAX_CONTENT_LENGTH"] = max(1, min(4, int(os.getenv("MAX_CONTENT_LENGTH_MB", "4")))) * 1024 * 1024

oauth = OAuth(app)
oauth.register(
    name="google",
    client_id=os.getenv("GOOGLE_CLIENT_ID", ""),
    client_secret=os.getenv("GOOGLE_CLIENT_SECRET", ""),
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

# Storage authorization is separate from regular user sign-in. It is requested
# only by the configured storage administrator and uses the narrow drive.file scope.
oauth.register(
    name="google_drive",
    client_id=os.getenv("GOOGLE_CLIENT_ID", ""),
    client_secret=os.getenv("GOOGLE_CLIENT_SECRET", ""),
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile https://www.googleapis.com/auth/drive.file"},
)

EXPIRATION_OPTIONS = {
    "10m": ("10 minutes", timedelta(minutes=10)),
    "1h": ("1 hour", timedelta(hours=1)),
    "24h": ("24 hours", timedelta(hours=24)),
    "7d": ("7 days", timedelta(days=7)),
}
DOWNLOAD_LIMITS = {1, 3, 5, 10}
MAX_GITHUB_UPLOAD_BYTES = 4 * 1024 * 1024
MAX_STANDARD_UPLOAD_BYTES = 5 * 1024**3
MAX_APPROVED_UPLOAD_BYTES = 50 * 1024**3
DRIVE_UPLOAD_CHUNK_BYTES = 4 * 1024 * 1024
UPLOAD_TICKET_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
DOWNLOAD_CHUNK_BYTES = 4 * 1024 * 1024
DOWNLOAD_TICKET_WINDOW_SECONDS = 30 * 60
DOWNLOAD_TICKET_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
SHARE_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")
ADMIN_PANEL_HOST = "adminqshare.darshanup.online"
ADMIN_PANEL_BASE_URL = f"https://{ADMIN_PANEL_HOST}"
TRANSFER_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_LARGE_UPLOAD_ALLOWLIST_CACHE = {"emails": frozenset(), "expires_at": 0.0}
_LARGE_UPLOAD_ALLOWLIST_LOCK = Lock()
LARGE_UPLOAD_ALLOWLIST_CACHE_SECONDS = 15
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
    except StorageError:
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
        "storage_provider": metadata.get("storage_provider", "github"),
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



def _is_storage_admin() -> bool:
    user = _current_user()
    admin_email = os.getenv("STORAGE_ADMIN_EMAIL", "").strip().lower()
    return bool(user and admin_email and user.get("email", "").strip().lower() == admin_email)


def _is_admin_host_request() -> bool:
    host = (request.host or "").split(":", 1)[0].rstrip(".").lower()
    if host == ADMIN_PANEL_HOST:
        return True
    # Keep the admin UI testable in local Flask tests and local development.
    return bool(app.testing and host in {"localhost", "127.0.0.1", "testserver"})


def _require_admin_host() -> None:
    if not _is_admin_host_request():
        abort(404)


def _get_large_upload_allowlist(*, force_refresh: bool = False) -> set[str]:
    """Read approved accounts from a private Drive config, failing closed on errors."""
    now = time.monotonic()
    if not force_refresh and now < _LARGE_UPLOAD_ALLOWLIST_CACHE["expires_at"]:
        return set(_LARGE_UPLOAD_ALLOWLIST_CACHE["emails"])

    with _LARGE_UPLOAD_ALLOWLIST_LOCK:
        now = time.monotonic()
        if not force_refresh and now < _LARGE_UPLOAD_ALLOWLIST_CACHE["expires_at"]:
            return set(_LARGE_UPLOAD_ALLOWLIST_CACHE["emails"])
        try:
            if not _drive_storage_ready():
                emails = set()
            else:
                emails = {
                    email.strip().lower()
                    for email in google_drive_storage.get_large_upload_allowlist()
                    if isinstance(email, str) and email.strip()
                }
            _LARGE_UPLOAD_ALLOWLIST_CACHE["emails"] = frozenset(emails)
            _LARGE_UPLOAD_ALLOWLIST_CACHE["expires_at"] = now + LARGE_UPLOAD_ALLOWLIST_CACHE_SECONDS
            return set(emails)
        except StorageError:
            app.logger.warning("Could not read the private 50 GB permission list; failing closed.", exc_info=True)
            if force_refresh:
                raise
            _LARGE_UPLOAD_ALLOWLIST_CACHE["emails"] = frozenset()
            _LARGE_UPLOAD_ALLOWLIST_CACHE["expires_at"] = now + 3
            return set()


def _save_large_upload_allowlist(emails: list[str]) -> list[str]:
    saved = google_drive_storage.set_large_upload_allowlist(emails)
    normalized = {email.strip().lower() for email in saved if isinstance(email, str) and email.strip()}
    with _LARGE_UPLOAD_ALLOWLIST_LOCK:
        _LARGE_UPLOAD_ALLOWLIST_CACHE["emails"] = frozenset(normalized)
        _LARGE_UPLOAD_ALLOWLIST_CACHE["expires_at"] = time.monotonic() + LARGE_UPLOAD_ALLOWLIST_CACHE_SECONDS
    return sorted(normalized)


def _large_upload_allowed() -> bool:
    user = _current_user()
    if not user:
        return False
    email = str(user.get("email", "")).strip().lower()
    return bool(email and email in _get_large_upload_allowlist())


def _large_transfer_ready() -> bool:
    # Large-file data moves in bounded 4 MiB chunks through Vercel and Drive's
    # resumable session; the browser never calls Google's upload host directly.
    return _drive_storage_ready()


def _upload_ticket_serializer():
    return URLSafeTimedSerializer(app.config["SECRET_KEY"], salt="quickshare-drive-upload-v1")


def _download_ticket_serializer():
    return URLSafeTimedSerializer(
        app.config["SECRET_KEY"],
        salt="quickshare-drive-download-v1",
    )


def _parse_share_expiry(metadata: dict) -> datetime:
    expiry = datetime.fromisoformat(str(metadata.get("expiry_time", "")))
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return expiry


def _make_download_ticket(metadata: dict, *, jti: str | None = None) -> tuple[str, int]:
    """Issue a short-window ticket for browser-to-Vercel range downloads."""
    share_id = str(metadata.get("share_id", "")).strip()
    file_id = str(metadata.get("drive_file_id", "")).strip()
    try:
        size = int(metadata.get("file_size", 0))
        share_exp_at = int(_parse_share_expiry(metadata).timestamp())
    except (TypeError, ValueError, OverflowError) as exc:
        raise StorageError("This share has invalid file size or expiry metadata.") from exc
    if not SHARE_ID_PATTERN.fullmatch(share_id) or not file_id:
        raise StorageError("This share is missing a valid private Google Drive file reference.")
    if size <= MAX_GITHUB_UPLOAD_BYTES or size > MAX_APPROVED_UPLOAD_BYTES:
        raise StorageError("This file is outside the supported large-download range.")
    now = int(datetime.now(timezone.utc).timestamp())
    if share_exp_at <= now:
        raise StorageError("This share has expired.")
    exp_at = min(share_exp_at, now + DOWNLOAD_TICKET_WINDOW_SECONDS)
    payload = {
        "jti": jti or secrets.token_hex(16),
        "share_id": share_id,
        "file_id": file_id,
        "filename": str(metadata.get("original_filename", "download.bin"))[:200],
        "size": size,
        "share_exp_at": share_exp_at,
        "exp_at": exp_at,
    }
    return _download_ticket_serializer().dumps(payload), exp_at


def _decode_download_ticket(token: str, *, enforce_window: bool = True) -> dict:
    if not isinstance(token, str) or not token:
        raise ValueError("Missing download ticket.")
    try:
        payload = _download_ticket_serializer().loads(
            token,
            max_age=DOWNLOAD_TICKET_MAX_AGE_SECONDS,
        )
    except SignatureExpired as exc:
        raise ValueError("The download ticket is too old. Reopen the share page to start again.") from exc
    except BadSignature as exc:
        raise ValueError("The download ticket is invalid.") from exc
    if not isinstance(payload, dict):
        raise ValueError("The download ticket is invalid.")
    try:
        size = int(payload.get("size", 0))
        exp_at = int(payload.get("exp_at", 0))
        share_exp_at = int(payload.get("share_exp_at", 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("The download ticket is invalid.") from exc
    if (
        not SHARE_ID_PATTERN.fullmatch(str(payload.get("share_id", "")))
        or not isinstance(payload.get("file_id"), str)
        or not payload.get("file_id")
        or not isinstance(payload.get("filename"), str)
        or size <= MAX_GITHUB_UPLOAD_BYTES
        or size > MAX_APPROVED_UPLOAD_BYTES
        or share_exp_at <= int(datetime.now(timezone.utc).timestamp())
    ):
        raise ValueError("The download ticket is invalid or the share has expired.")
    if enforce_window and exp_at <= int(datetime.now(timezone.utc).timestamp()):
        raise ValueError("The download ticket needs refreshing.")
    return payload


def _drive_storage_ready() -> bool:
    return google_drive_storage.is_configured()


def upload_file_bytes(storage_provider: str, storage_path: str, content: bytes, share_id: str) -> dict:
    """Route a new file to its selected provider while keeping share metadata in GitHub."""
    if storage_provider == "google_drive":
        return google_drive_storage.upload_file_bytes(storage_path, content, share_id)
    if storage_provider == "github":
        return upload_github_file_bytes(storage_path, content, share_id)
    raise StorageError("Choose a valid storage provider.")


def download_share_file(metadata: dict) -> bytes:
    if metadata.get("storage_provider", "github") == "google_drive":
        return google_drive_storage.download_private_file(metadata.get("drive_file_id", ""))
    return download_public_file(metadata["storage_path"])


@app.context_processor
def inject_storage_settings():
    return {
        "is_storage_admin": _is_storage_admin(),
        "google_drive_storage_ready": _drive_storage_ready(),
        "large_upload_allowed": _large_upload_allowed(),
        "upload_limit_gb": 50 if _large_upload_allowed() else 5,
        "large_transfer_ready": _large_transfer_ready(),
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
    if _is_admin_host_request():
        return redirect(url_for("admin_transfer_permissions"))
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
    session["after_login_admin_host"] = bool(
        _is_admin_host_request() and next_url and next_url.startswith("/admin/")
    )
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
        return_to_admin_host = session.get("after_login_admin_host") is True and next_url.startswith("/admin/")
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
        if return_to_admin_host:
            return redirect(f"{ADMIN_PANEL_BASE_URL}{next_url}")
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


@app.get("/admin/storage")
@login_required
def admin_storage():
    _require_admin_host()
    if not _is_storage_admin():
        abort(403)
    redirect_uri = os.getenv("GOOGLE_DRIVE_REDIRECT_URI", "").strip() or url_for("google_drive_callback", _external=True)
    return render_template(
        "drive_setup.html",
        drive_storage_ready=_drive_storage_ready(),
        storage_admin_email=os.getenv("STORAGE_ADMIN_EMAIL", ""),
        drive_redirect_uri=redirect_uri,
    )


@app.get("/admin/transfers")
@login_required
def admin_transfer_permissions():
    _require_admin_host()
    if not _is_storage_admin():
        abort(403)
    drive_ready = _drive_storage_ready()
    approved_emails = []
    permission_error = ""
    if drive_ready:
        try:
            approved_emails = sorted(_get_large_upload_allowlist(force_refresh=True))
        except StorageError as exc:
            permission_error = str(exc)
    else:
        permission_error = "Connect Google Drive before managing transfer permissions."
    return render_template(
        "transfer_permissions.html",
        approved_emails=approved_emails,
        drive_storage_ready=drive_ready,
        storage_admin_email=os.getenv("STORAGE_ADMIN_EMAIL", ""),
        permission_error=permission_error,
    )


@app.post("/admin/transfers/add")
@login_required
def admin_add_transfer_permission():
    _require_admin_host()
    if not _is_storage_admin():
        abort(403)
    email = str(request.form.get("email", "")).strip().lower()
    if not email or len(email) > 254 or not TRANSFER_EMAIL_PATTERN.fullmatch(email):
        flash("Enter a valid email address for the Google account to approve.", "error")
        return redirect(url_for("admin_transfer_permissions"))
    if not _drive_storage_ready():
        flash("Connect Google Drive before changing transfer permissions.", "error")
        return redirect(url_for("admin_transfer_permissions"))
    try:
        emails = _get_large_upload_allowlist(force_refresh=True)
        if email in emails:
            flash(f"{email} already has 50 GB transfer permission.", "info")
        else:
            emails.add(email)
            _save_large_upload_allowlist(sorted(emails))
            flash(f"Approved {email} for uploads up to 50 GB per file.", "success")
    except StorageError as exc:
        flash(f"Could not update transfer permissions: {exc}", "error")
    return redirect(url_for("admin_transfer_permissions"))


@app.post("/admin/transfers/remove")
@login_required
def admin_remove_transfer_permission():
    _require_admin_host()
    if not _is_storage_admin():
        abort(403)
    email = str(request.form.get("email", "")).strip().lower()
    if not email or len(email) > 254 or not TRANSFER_EMAIL_PATTERN.fullmatch(email):
        flash("The selected email address is invalid.", "error")
        return redirect(url_for("admin_transfer_permissions"))
    if not _drive_storage_ready():
        flash("Connect Google Drive before changing transfer permissions.", "error")
        return redirect(url_for("admin_transfer_permissions"))
    try:
        emails = _get_large_upload_allowlist(force_refresh=True)
        if email not in emails:
            flash(f"{email} does not currently have 50 GB transfer permission.", "info")
        else:
            emails.remove(email)
            _save_large_upload_allowlist(sorted(emails))
            flash(f"Revoked 50 GB transfer permission for {email}. New uploads are limited to 5 GB.", "success")
    except StorageError as exc:
        flash(f"Could not update transfer permissions: {exc}", "error")
    return redirect(url_for("admin_transfer_permissions"))


@app.get("/admin/storage/google-drive/connect")
@login_required
def connect_google_drive():
    _require_admin_host()
    if not _is_storage_admin():
        abort(403)
    if not os.getenv("GOOGLE_CLIENT_ID") or not os.getenv("GOOGLE_CLIENT_SECRET"):
        flash("Google OAuth client credentials must be configured first.", "error")
        return redirect(url_for("admin_storage"))
    session["drive_connect_pending"] = True
    redirect_uri = os.getenv("GOOGLE_DRIVE_REDIRECT_URI", "").strip() or url_for("google_drive_callback", _external=True)
    return oauth.google_drive.authorize_redirect(
        redirect_uri,
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )


@app.get("/auth/google-drive/callback")
def google_drive_callback():
    if not _is_storage_admin() or not session.pop("drive_connect_pending", False):
        abort(403)
    try:
        token = oauth.google_drive.authorize_access_token()
        userinfo = token.get("userinfo")
        if not userinfo:
            userinfo = oauth.google_drive.userinfo(token=token)
        email = str(userinfo.get("email", "")).strip().lower()
        if userinfo.get("email_verified") is not True or email != os.getenv("STORAGE_ADMIN_EMAIL", "").strip().lower():
            flash("Connect Drive using the Google account configured as STORAGE_ADMIN_EMAIL.", "error")
            return redirect(url_for("admin_storage"))
        refresh_token = str(token.get("refresh_token") or "").strip()
        return render_template(
            "drive_setup.html",
            drive_storage_ready=_drive_storage_ready(),
            storage_admin_email=os.getenv("STORAGE_ADMIN_EMAIL", ""),
            drive_redirect_uri=os.getenv("GOOGLE_DRIVE_REDIRECT_URI", "").strip() or url_for("google_drive_callback", _external=True),
            one_time_refresh_token=refresh_token,
            refresh_token_missing=not bool(refresh_token),
        )
    except Exception:
        app.logger.warning("Google Drive connection failed", exc_info=True)
        flash("Google Drive authorization failed. Check the callback URI and try again.", "error")
        return redirect(url_for("admin_storage"))


@app.get("/health")
def health():
    return {"status": "ok", "service": "QuickShare Cloud"}


@app.post("/api/uploads/initiate")
@login_required
def api_upload_initiate():
    data = request.get_json(silent=True) or {}
    original_name = str(data.get("filename", "")).replace("\\", "/").split("/")[-1].strip()[:200]
    if not original_name:
        return jsonify({"error": "Choose a valid filename."}), 400
    suffix = Path(original_name).suffix.lower()
    if suffix in BLOCKED_EXTENSIONS:
        return jsonify({"error": "That file type is not accepted."}), 400
    if suffix and not EXTENSION_PATTERN.fullmatch(suffix):
        return jsonify({"error": "The file extension is invalid."}), 400

    try:
        file_size = int(data.get("file_size", 0))
        max_downloads = int(data.get("max_downloads", 3))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid file size or download limit."}), 400
    if file_size <= 0:
        return jsonify({"error": "Empty files cannot be uploaded."}), 400
    limit_bytes = MAX_APPROVED_UPLOAD_BYTES if _large_upload_allowed() else MAX_STANDARD_UPLOAD_BYTES
    if file_size > limit_bytes:
        limit_gb = 50 if _large_upload_allowed() else 5
        return jsonify({"error": f"This account can upload files up to {limit_gb} GB."}), 413
    if max_downloads not in DOWNLOAD_LIMITS:
        return jsonify({"error": "Choose a valid download limit."}), 400
    expiry_choice = str(data.get("expiry", "24h"))
    if expiry_choice not in EXPIRATION_OPTIONS:
        return jsonify({"error": "Choose a valid expiration period."}), 400
    if str(data.get("storage_provider", "google_drive")).strip().lower() != "google_drive":
        return jsonify({"error": "Resumable large uploads require Google Drive storage."}), 400
    if not _drive_storage_ready():
        return jsonify({"error": "Google Drive storage is not connected."}), 503
    user = _current_user()
    owner_id_hash = _current_owner_hash()
    if not user or not owner_id_hash:
        return jsonify({"error": "Sign in again before uploading."}), 401
    share_id = secrets.token_hex(16)
    upload_nonce = secrets.token_hex(16)
    storage_path = f"files/{share_id}{suffix or '.bin'}"
    label, duration = EXPIRATION_OPTIONS[expiry_choice]
    now = datetime.now(timezone.utc)
    mime_type = str(data.get("mime_type") or mimetypes.guess_type(original_name)[0] or "application/octet-stream")[:120]
    metadata = {
        "share_id": share_id,
        "original_filename": original_name,
        "storage_path": storage_path,
        "storage_provider": "google_drive",
        "file_size": file_size,
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
        session_uri = google_drive_storage.initiate_resumable_upload(
            storage_path=storage_path,
            share_id=share_id,
            upload_nonce=upload_nonce,
            file_size=file_size,
            mime_type=mime_type,
        )
    except StorageError as exc:
        app.logger.warning("Could not start resumable Drive upload: %s", exc)
        return jsonify({"error": str(exc)}), 502

    upload_ticket_data = {
        "metadata": metadata,
        "upload_nonce": upload_nonce,
        "session_uri": session_uri,
        "max_upload_bytes": limit_bytes,
    }
    upload_ticket = _upload_ticket_serializer().dumps(upload_ticket_data)
    return jsonify({
        "upload_ticket": upload_ticket,
        "chunk_size": DRIVE_UPLOAD_CHUNK_BYTES,
        "file_size": file_size,
        "share_id": share_id,
    })


@app.post("/api/uploads/chunk")
@login_required
def api_upload_chunk():
    upload_ticket = request.headers.get("X-Upload-Ticket", "")
    try:
        ticket_data = _upload_ticket_serializer().loads(
            upload_ticket,
            max_age=UPLOAD_TICKET_MAX_AGE_SECONDS,
        )
    except SignatureExpired:
        return jsonify({"error": "This upload session expired. Start the upload again."}), 410
    except BadSignature:
        return jsonify({"error": "This upload ticket is invalid."}), 400

    metadata = ticket_data.get("metadata") if isinstance(ticket_data, dict) else None
    session_uri = ticket_data.get("session_uri", "") if isinstance(ticket_data, dict) else ""
    if not isinstance(metadata, dict) or not isinstance(session_uri, str):
        return jsonify({"error": "The upload ticket is invalid."}), 400
    try:
        file_size = int(metadata.get("file_size", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "The upload file size is invalid."}), 400

    expected_owner_hash = str(metadata.get("owner_id_hash", ""))
    current_owner_hash = _current_owner_hash()
    if not current_owner_hash or not hmac.compare_digest(current_owner_hash, expected_owner_hash):
        return jsonify({"error": "This upload belongs to another signed-in account."}), 403
    try:
        ticket_max_bytes = int(ticket_data.get("max_upload_bytes", MAX_STANDARD_UPLOAD_BYTES))
    except (TypeError, ValueError):
        return jsonify({"error": "This upload ticket has an invalid permission tier."}), 400
    if ticket_max_bytes not in (MAX_STANDARD_UPLOAD_BYTES, MAX_APPROVED_UPLOAD_BYTES):
        return jsonify({"error": "This upload ticket has an invalid permission tier."}), 400
    if file_size <= 0 or file_size > ticket_max_bytes:
        return jsonify({"error": "This upload ticket does not permit a file of this size."}), 413

    content_range = request.headers.get("Content-Range", "").strip()
    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range)
    if not match:
        return jsonify({"error": "The upload chunk is missing a valid Content-Range header."}), 400
    start, end, total = map(int, match.groups())
    if (
        total != file_size
        or start < 0
        or end < start
        or end >= total
        or end - start + 1 > DRIVE_UPLOAD_CHUNK_BYTES
    ):
        return jsonify({"error": "The upload chunk range is invalid."}), 416
    try:
        chunk = request.get_data(cache=False, as_text=False)
    except Exception:
        app.logger.exception("Could not read upload chunk")
        return jsonify({"error": "QuickShare could not read this upload chunk. Retry it."}), 400
    if len(chunk) != end - start + 1:
        return jsonify({"error": "The upload chunk byte count does not match its Content-Range."}), 400

    try:
        result = google_drive_storage.upload_resumable_chunk(
            session_uri,
            start=start,
            end=end,
            total_size=total,
            content=chunk,
        )
    except StorageError as exc:
        app.logger.warning("Could not forward Drive upload chunk for share %s: %s", metadata.get("share_id"), exc)
        return jsonify({"error": str(exc)}), 502

    if result.get("complete"):
        drive_file = result.get("file") or {}
        if not drive_file.get("id"):
            return jsonify({"error": "Google Drive completed the chunk but returned no file ID."}), 502
        return jsonify({
            "complete": True,
            "next_offset": total,
            "drive_file_id": drive_file["id"],
        })
    next_offset = result.get("next_offset", 0)
    if not isinstance(next_offset, int) or next_offset < 0 or next_offset > total:
        return jsonify({"error": "Google Drive returned an invalid upload offset."}), 502
    return jsonify({"complete": False, "next_offset": next_offset})


@app.post("/api/uploads/finalize")
@login_required
def api_upload_finalize():
    data = request.get_json(silent=True) or {}
    upload_ticket = data.get("upload_ticket", "")
    drive_file_id = data.get("drive_file_id", "")
    if not isinstance(upload_ticket, str) or not isinstance(drive_file_id, str) or not drive_file_id.strip():
        return jsonify({"error": "Missing upload ticket or completed Drive file ID."}), 400
    try:
        ticket_data = _upload_ticket_serializer().loads(
            upload_ticket, max_age=UPLOAD_TICKET_MAX_AGE_SECONDS
        )
    except SignatureExpired:
        return jsonify({"error": "This upload session expired. Start the upload again."}), 410
    except BadSignature:
        return jsonify({"error": "This upload ticket is invalid."}), 400

    metadata = ticket_data.get("metadata") if isinstance(ticket_data, dict) else None
    upload_nonce = ticket_data.get("upload_nonce", "") if isinstance(ticket_data, dict) else ""
    if not isinstance(metadata, dict) or not isinstance(upload_nonce, str):
        return jsonify({"error": "The upload ticket is invalid."}), 400
    current_owner_hash = _current_owner_hash()
    expected_owner_hash = str(metadata.get("owner_id_hash", ""))
    if not current_owner_hash or not hmac.compare_digest(current_owner_hash, expected_owner_hash):
        return jsonify({"error": "This upload belongs to another signed-in account."}), 403
    try:
        verified_file = google_drive_storage.verify_resumable_upload(
            drive_file_id.strip(),
            share_id=str(metadata["share_id"]),
            upload_nonce=upload_nonce,
            expected_size=int(metadata["file_size"]),
        )
        metadata["drive_file_id"] = verified_file["id"]
        already_saved = get_share_metadata(metadata["share_id"])
        if already_saved:
            if (
                already_saved.get("drive_file_id") != metadata["drive_file_id"]
                or already_saved.get("owner_id_hash") != expected_owner_hash
            ):
                return jsonify({"error": "A different file is already registered for this upload."}), 409
        else:
            create_share_metadata(metadata)
    except StorageError as exc:
        app.logger.warning("Could not finalize resumable Drive upload: %s", exc)
        return jsonify({"error": str(exc)}), 502
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "The completed upload metadata is invalid."}), 400
    return jsonify({
        "status": "success",
        "share_id": metadata["share_id"],
        "share_url": url_for("share_page", share_id=metadata["share_id"]),
    })


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
        flash("The selected file is too large. The maximum is 4 MB.", "error")
        return redirect(url_for("index"))

    storage_provider = request.form.get(
        "storage_provider",
        "google_drive" if _drive_storage_ready() else "github",
    ).strip().lower()
    if storage_provider not in {"github", "google_drive"}:
        flash("Choose a valid storage provider.", "error")
        return redirect(url_for("index"))
    if storage_provider == "google_drive" and not _drive_storage_ready():
        flash("Google Drive storage is not configured yet. Ask the storage administrator to connect Drive in Settings → Storage.", "error")
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
        "storage_provider": storage_provider,
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

    uploaded_file = None
    try:
        uploaded_file = upload_file_bytes(
            storage_provider=storage_provider,
            storage_path=storage_path,
            content=content,
            share_id=share_id,
        )
        if storage_provider == "google_drive":
            metadata["drive_file_id"] = uploaded_file.get("id", "")
        create_share_metadata(metadata)
    except StorageError as exc:
        app.logger.warning("%s upload failed: %s", storage_provider, exc)
        # Best-effort cleanup if the file uploaded but its metadata commit failed.
        if uploaded_file:
            try:
                if storage_provider == "google_drive":
                    google_drive_storage.delete_private_file(uploaded_file.get("id", ""))
                else:
                    delete_github_file(storage_path)
            except StorageError:
                app.logger.warning("Could not clean up an orphaned uploaded file %s", share_id)
        flash(f"Upload to {('Google Drive' if storage_provider == 'google_drive' else 'GitHub')} failed: {exc}", "error")
        return redirect(url_for("index"))
    except Exception:
        app.logger.exception("Could not save share metadata")
        if uploaded_file:
            try:
                if storage_provider == "google_drive":
                    google_drive_storage.delete_private_file(uploaded_file.get("id", ""))
                else:
                    delete_github_file(storage_path)
            except StorageError:
                app.logger.warning("Could not clean up an orphaned uploaded file %s", share_id)
        flash("The file may have reached storage, but its share metadata could not be saved. Check the storage backend before retrying.", "error")
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


@app.post("/api/download/initiate/<share_id>")
def api_download_initiate(share_id: str):
    if not SHARE_ID_PATTERN.fullmatch(share_id):
        abort(404)
    file_record = get_share_metadata(share_id)
    if not file_record:
        return jsonify({"error": "Share not found."}), 404
    status = share_status(file_record)
    if status != "active":
        record_rejected_attempt(share_id, status)
        return jsonify({"error": "This share is " + status + "."}), 410
    if file_record.get("storage_provider", "github") != "google_drive":
        return jsonify({"error": "Large downloads are supported for private Google Drive files only."}), 400
    try:
        ticket, expires_at = _make_download_ticket(file_record)
        result = record_download(share_id)
    except StorageError as exc:
        return jsonify({"error": str(exc)}), 400
    if result != "success":
        return jsonify({"error": "Could not reserve this download. Please retry."}), 409
    return jsonify({
        "ticket": ticket,
        "ticket_expires_at": expires_at,
        "file_size": int(file_record["file_size"]),
        "chunk_size": DOWNLOAD_CHUNK_BYTES,
    })


@app.post("/api/download/refresh")
def api_download_refresh():
    data = request.get_json(silent=True) or {}
    token = data.get("ticket", "")
    try:
        payload = _decode_download_ticket(token, enforce_window=False)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 410

    share_id = payload["share_id"]
    metadata = get_share_metadata(share_id)
    if not metadata:
        return jsonify({"error": "This share no longer exists."}), 404
    try:
        expiry = _parse_share_expiry(metadata)
        same_file = (
            metadata.get("storage_provider") == "google_drive"
            and hmac.compare_digest(str(metadata.get("drive_file_id", "")), str(payload["file_id"]))
            and int(metadata.get("file_size", 0)) == int(payload["size"])
        )
    except (TypeError, ValueError):
        same_file = False
        expiry = datetime.fromtimestamp(0, tz=timezone.utc)
    if not same_file:
        return jsonify({"error": "This download ticket no longer matches the share."}), 403
    if metadata.get("revoked") or expiry <= datetime.now(timezone.utc):
        return jsonify({"error": "This share has expired or was revoked."}), 410
    try:
        refreshed, expires_at = _make_download_ticket(metadata, jti=str(payload.get("jti") or ""))
    except StorageError as exc:
        return jsonify({"error": str(exc)}), 410
    return jsonify({"ticket": refreshed, "ticket_expires_at": expires_at})


@app.post("/api/download/chunk")
def api_download_chunk():
    data = request.get_json(silent=True) or {}
    try:
        payload = _decode_download_ticket(data.get("ticket", ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 410
    start, end = data.get("start"), data.get("end")
    if type(start) is not int or type(end) is not int:
        return jsonify({"error": "Chunk start and end must be integers."}), 400
    size = int(payload["size"])
    length = end - start + 1
    if start < 0 or end < start or end >= size or length > DOWNLOAD_CHUNK_BYTES:
        return jsonify({"error": "The requested byte range is invalid."}), 416

    try:
        upstream = google_drive_storage.open_private_file_stream(
            payload["file_id"],
            f"bytes={start}-{end}",
        )
    except StorageError:
        app.logger.exception("Could not stream Drive chunk for share %s", payload["share_id"])
        return jsonify({"error": "Google Drive could not retrieve this file segment. Retry shortly."}), 502

    try:
        upstream_range = upstream.headers.get("Content-Range", "")
        match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", upstream_range)
        if upstream.status_code != 206 or not match:
            return jsonify({"error": "Google Drive did not return a valid partial-file response."}), 502
        actual_start, actual_end, actual_size = map(int, match.groups())
        if actual_start != start or actual_end != end or actual_size != size:
            return jsonify({"error": "Google Drive returned an unexpected file segment."}), 502
        chunks = bytearray()
        for piece in upstream.iter_content(chunk_size=1024 * 1024):
            if piece:
                chunks.extend(piece)
                if len(chunks) > length:
                    return jsonify({"error": "Google Drive returned more bytes than requested."}), 502
        if len(chunks) != length:
            return jsonify({"error": "Google Drive returned an incomplete file segment. Retry the chunk."}), 502
    except Exception:
        app.logger.exception("Error reading Drive chunk for share %s", payload["share_id"])
        return jsonify({"error": "Could not read this file segment. Retry shortly."}), 502
    finally:
        upstream.close()

    response = app.response_class(bytes(chunks), status=206, mimetype="application/octet-stream")
    response.headers["Content-Length"] = str(length)
    response.headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    response.headers["Accept-Ranges"] = "bytes"
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


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

    is_drive_file = file_record.get("storage_provider", "github") == "google_drive"
    try:
        file_size = int(file_record.get("file_size", 0))
    except (TypeError, ValueError):
        file_size = 0

    if is_drive_file and file_size > MAX_GITHUB_UPLOAD_BYTES:
        return render_template("large_download.html", file=file_record, chunk_size=DOWNLOAD_CHUNK_BYTES)

    try:
        content = download_share_file(file_record)
        result = record_download(share_id)
    except StorageError as exc:
        app.logger.warning("Storage download failed for share %s: %s", share_id, exc)
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
    except StorageError as exc:
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
    except StorageError as exc:
        app.logger.warning("Scheduled cleanup failed: %s", exc)
        return jsonify({"error": str(exc)}), 503


@app.errorhandler(413)
def file_too_large(_error):
    flash("File is too large. The maximum upload size is capped at 4 MB.", "error")
    return redirect(url_for("index"))


@app.errorhandler(404)
def not_found(_error):
    return render_template("error.html", title="Share not found", message="This sharing link does not exist or is no longer available."), 404


@app.errorhandler(StorageError)
def storage_service_error(error):
    app.logger.warning("Storage service failed: %s", error)
    return render_template(
        "error.html",
        title="Storage temporarily unavailable",
        message="QuickShare could not reach its configured storage backend. Check the storage configuration and try again.",
    ), 503


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=os.getenv("FLASK_DEBUG", "0") == "1")
