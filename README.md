# QuickShare Cloud

A college Cloud Computing mini-project for QR-code-based file sharing with Google sign-in, account-scoped share management, analytics, expiry enforcement, revocation, and scheduled cleanup.

## Features

- Google OpenID Connect sign-in using Authlib; verified Google email is required.
- Uploads limited to 4 MB and restricted file-extension validation.
- Files and share metadata stored in a separate public GitHub repository.
- QR-code share pages that recipients can open without having a QuickShare account.
- My Shares dashboard scoped to the signed-in Google account, with search, filters, links, QR downloads, and activity history.
- Server-side revocation, expiration, download-limit checks, and recent download/access event logging.
- Protected daily Vercel Cron cleanup for expired/revoked file entries.
- CSRF checks for authenticated state-changing requests, security response headers, GitHub API rate-limit handling, and tests.

## Local setup

1. Install Python 3.10 or newer.
2. Create and activate a virtual environment.
3. Install dependencies: `pip install -r requirements-dev.txt`.
4. Copy `.env.example` to `.env`; set GitHub and Google OAuth values.
5. Configure the Google OAuth web client with this local redirect URI: `http://127.0.0.1:5000/auth/google/callback`.
6. Run `python app.py`.

Never commit `.env`. Use a fine-grained GitHub token restricted to `QuickShare-Storage`, with Contents: Read and write permission.

## Google OAuth setup

In Google Cloud Console, create a project and configure the OAuth consent screen / Google Auth Platform. Create an OAuth client of type **Web application**. Add the exact local and deployed callback addresses under **Authorized redirect URIs**:

- Local: `http://127.0.0.1:5000/auth/google/callback`
- Production: `https://YOUR-STABLE-VERCEL-DOMAIN/auth/google/callback`

The production value must match `GOOGLE_REDIRECT_URI` exactly, including scheme, host, path, and trailing-slash choice. Use the stable Production domain shown under Vercel → Project → Settings → Domains, not an ephemeral deployment-preview URL. If the consent screen is in Testing mode, add the intended Google accounts as Test users.

Set the following Production Environment Variables in Vercel:

- `GITHUB_TOKEN`
- `GITHUB_OWNER` = `Darshan-U-P`
- `GITHUB_REPO` = `QuickShare-Storage`
- `GITHUB_BRANCH` = `main`
- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `GOOGLE_REDIRECT_URI` = the exact production callback address above
- `SECRET_KEY` = a random Flask session secret
- `OWNER_HASH_SECRET` = a separate random secret; keep it stable across deployments
- `CRON_SECRET` = a separate random bearer secret
- `FLASK_DEBUG` = `0`
- `MAX_CONTENT_LENGTH_MB` = `4`

Do not put OAuth client secrets or GitHub tokens in source code or in `.env.example`. After configuring variables, redeploy the application.

## Account ownership

A verified Google account is required to upload and manage files. Recipient-facing share pages and authorized downloads remain public to anyone holding a share link. Each new metadata record stores a keyed hash of the Google subject, not the Google email address. My Shares lists only records belonging to the current signed-in account. Shares created before Google ownership was added are not automatically claimed by any account and will not appear in My Shares.

## Important limitations

- **The GitHub storage repository is public.** Files and metadata in it must be treated as public. QuickShare's expiry/revocation/download limits cannot stop direct GitHub access.
- Cleanup deletes the file from the current branch revision but does not erase earlier Git history.
- GitHub's Contents API is a low-traffic prototype backend, not a transactional database. SHA-based conflict retry helps, but the service is not production-grade under high concurrency.
- My Shares depends on GitHub metadata reads; more shares mean more API requests and can run into GitHub rate limits.
- The 4 MiB cap leaves a small margin under Vercel Functions' 4.5 MB request/response payload limit. Larger files require a direct-to-storage upload architecture.
- The project schedules `/api/cron/cleanup` daily at 03:00 UTC. Vercel Hobby cron runs once daily; exact execution minute is not guaranteed.
