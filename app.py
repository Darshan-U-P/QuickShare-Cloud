import os
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, render_template

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "local-development-only-change-me")
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_CONTENT_LENGTH_MB", "5")) * 1024 * 1024


@app.get("/")
def index():
    storage_repo = os.getenv("GITHUB_REPO", "QuickShare-Storage")
    return render_template("index.html", storage_repo=storage_repo)


@app.get("/health")
def health():
    return {"status": "ok", "service": "QuickShare Cloud"}


@app.errorhandler(413)
def file_too_large(_error):
    return {"error": "File is too large for this demo. Maximum upload size is configured in .env."}, 413


if __name__ == "__main__":
    # Debug server is only for local development, not public deployment.
    app.run(host="127.0.0.1", port=5000, debug=os.getenv("FLASK_DEBUG", "0") == "1")
