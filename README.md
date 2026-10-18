# QuickShare Cloud

A college mini-project demonstrating QR-code-based file sharing, expiry enforcement, download limits, and GitHub-backed storage.

## Architecture

- **Flask** serves the web UI and validates share requests.
- **SQLite** stores share metadata and download activity.
- **GitHub REST API** stores demonstration files in the separate public `QuickShare-Storage` repository.
- **qrcode** generates QR codes for application share URLs.

> **Public-storage warning:** Files committed to the public storage repository must be treated as public. Deleting a file from the latest revision does not guarantee removal from Git history. Do not upload private or sensitive data.

## Local setup

1. Create and activate a virtual environment.
2. Install requirements: `pip install -r requirements.txt`
3. Copy `.env.example` to `.env` and configure the values.
4. Run `python app.py`.

The app is intended for local development. Sharing from outside your network requires a publicly reachable app host; GitHub stores files, but does not host this Flask application.

## Planned milestones

1. Flask home page and configuration validation.
2. Upload demonstration files through the GitHub API.
3. Store metadata and generate QR-code share pages.
4. Enforce expiry and download limits on the server.
5. Add cleanup, tests, and project documentation.
