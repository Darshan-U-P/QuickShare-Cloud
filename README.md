# QuickShare Cloud

A college Cloud Computing mini-project: QR-code-based file sharing with expiring links, download limits, and GitHub-backed storage.

## Current implementation

- Flask web application with a health endpoint.
- File uploads stored in the separate GitHub repository using the GitHub Contents API.
- Persistent share metadata stored as JSON files in that same GitHub repository, so state survives Vercel serverless invocations.
- QR-code generation, expiring links, download limits, and a bounded list of recent successful/denied attempts.
- A 3 MB maximum upload size to leave room under Vercel Functions' 4.5 MB request/response body limit.

## Local setup

1. Create and activate a Python virtual environment.
2. Install dependencies: `pip install -r requirements.txt`.
3. Copy `.env.example` to `.env` and set your GitHub token and storage repository values.
4. Run `python app.py`.

Never commit `.env`. Use a fine-grained token restricted to the `QuickShare-Storage` repository with Contents: Read and write permission. When deploying to Vercel, add the same variables under Project Settings → Environment Variables; never upload the token to your application repository.

## Deploying to Vercel

Vercel detects a Flask app exported as `app` from root-level `app.py`, so no custom start command is needed for the basic deployment. Connect this repository to Vercel, keep the Hobby plan for this personal college project, and set these environment variables:

- `GITHUB_TOKEN`
- `GITHUB_OWNER` = `Darshan-U-P`
- `GITHUB_REPO` = `QuickShare-Storage`
- `GITHUB_BRANCH` = `main`
- `SECRET_KEY` = a new random value
- `FLASK_DEBUG` = `0`
- `MAX_CONTENT_LENGTH_MB` = `3`

Leave `PUBLIC_BASE_URL` empty initially; the share link is based on the incoming request host. After deployment, test the production URL and generate a new QR code there. Old QR codes created on localhost still point to localhost.

## Public storage warning

The GitHub repository is public, including uploaded files and JSON metadata. Do not upload sensitive files. App-level expiration and download limits do not block direct access to the public GitHub URLs. Deleting a file from the latest revision does not guarantee erasure from Git history. GitHub's Contents API is not a database; SHA-conflict retries help for a low-traffic demo, but this is not suitable as a production transactional store.

## Notes

Vercel Functions have a read-only filesystem apart from temporary storage, so this app does not rely on local SQLite for persistent share metadata. Vercel also limits request and response bodies to 4.5 MB; the app's 3 MB cap leaves room for multipart overhead.
