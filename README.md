# QuickShare Cloud

A college mini-project demonstrating QR-code-based file sharing, expiry enforcement, download limits, and GitHub-backed storage.

## Current implementation

- Flask home page and health endpoint.
- File upload to the configured GitHub repository through the GitHub Contents API.
- SQLite metadata and download activity records.
- QR-code generation for each share page.
- Expiration checks and server-side download limits.
- A 5 MB default upload cap (configurable through `MAX_CONTENT_LENGTH_MB`).

## Local setup

1. Create and activate a virtual environment.
2. Install dependencies: `pip install -r requirements.txt`.
3. Copy `.env.example` to `.env` and configure your token and repository.
4. Run `python app.py`.

Never commit `.env`. A fine-grained GitHub token should be limited to the `QuickShare-Storage` repository and have Contents: Read and write permission.

## Critical storage limitation

The storage repository is public. All files there must be treated as public and non-sensitive. App-level expiration and download limits only protect requests made through QuickShare; they do not prevent direct access to the public repository. Deleting a file from the current branch does not guarantee erasure from Git history. This is a learning prototype, not production-grade private file storage.

## Local-only network behavior

Flask binds to `127.0.0.1` for local development. A QR code generated with this base address will work on the same computer only. For access from another device, configure a reachable base URL and a deliberate network binding/firewall rule. Do not expose Flask's debug server directly to the public internet.
