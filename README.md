# QuickShare Cloud

A college Cloud Computing mini-project for QR-code-based file sharing with browser-local share history, revocation, activity analytics, expiry enforcement, and scheduled cleanup.

## Features

- Upload files up to 3 MB to a separate public GitHub storage repository.
- Generate share pages and QR codes.
- Save newly created shares and their management token in the creator's browser localStorage.
- My Shares dashboard with search, status filters, copy-link and QR downloads.
- Owner-authorized link revocation and recent access/download activity.
- Server-side expiry and download-limit checks with optimistic GitHub SHA updates.
- Daily Vercel Cron cleanup of expired/revoked files from the current branch.
- Upload validation, security response headers, API rate-limit errors, and automated tests.

## Local setup

1. Create and activate a Python virtual environment.
2. Install dependencies with `pip install -r requirements-dev.txt`.
3. Copy `.env.example` to `.env` and set your local token and secret values.
4. Run `python app.py`.

Never commit `.env`. Use a fine-grained GitHub token restricted to `QuickShare-Storage`, with Contents: Read and write permission.

## Vercel setup

Vercel detects the Flask app from root-level `app.py`. Set these Production Environment Variables in Vercel:

- `GITHUB_TOKEN`
- `GITHUB_OWNER` = `Darshan-U-P`
- `GITHUB_REPO` = `QuickShare-Storage`
- `GITHUB_BRANCH` = `main`
- `SECRET_KEY` = a new random value
- `CRON_SECRET` = a random secret with at least 16 characters
- `FLASK_DEBUG` = `0`
- `MAX_CONTENT_LENGTH_MB` = `3`

The project schedules `/api/cron/cleanup` daily at 03:00 UTC. Vercel Cron calls this endpoint using the `Authorization: Bearer <CRON_SECRET>` header. Hobby schedules run once per day and are not guaranteed to fire at the exact minute. After adding `CRON_SECRET`, redeploy so the function and cron configuration are refreshed.

## My Shares and owner controls

The high-entropy owner-management token is delivered only to the browser that creates a share, then stored in that browser's localStorage. It is not placed in the share URL. Clearing browser storage loses the management capability. Shares created before owner controls were introduced do not have a management token and cannot be revoked in the dashboard.

Share metadata (including the hash of the owner token and recent activity events) is stored as JSON files in the public GitHub repository. Treat all repository contents as public. Never upload sensitive files.

## Limitations

- GitHub's Contents API is a low-traffic prototype backend, not a transactional database. Optimistic SHA conflict checks reduce concurrent counter races but are not production-grade.
- Expiry and download limits apply only to QuickShare requests. The files in the public GitHub repository remain directly accessible.
- Cleanup removes file content from the current branch and marks metadata; it does not erase older Git history.
- Cleanup is scheduled daily and processes a bounded number of expired/revoked files per run.
- The browser-local My Shares list does not synchronize across devices.
- The 3 MB cap leaves room under Vercel Functions' request/response payload limit.
