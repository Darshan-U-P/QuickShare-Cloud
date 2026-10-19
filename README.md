# QuickShare Cloud

A zero-extra-service-cost cloud computing mini-project: QR-code file sharing hosted on Vercel, with Google sign-in, account-scoped share management, expiring links and selectable GitHub or Google Drive file storage.

## Features

- Google OpenID Connect sign-in; each account sees and manages only its own shares.
- Resumable Google Drive uploads: 5 GB per file by default; accounts explicitly approved by the administrator may upload up to 50 GB per file.
- File contents upload directly from the browser to Google Drive in resumable chunks, avoiding Vercel's request-body limit.
- Large private downloads use 4 MiB byte ranges through Vercel and are written directly to the recipient's local file; no additional VPS is required.
- Choose a file-storage provider per upload:
  - **Google Drive**: once the administrator connects one Drive account, this becomes the default central storage for every user's uploads; file contents stay private in that account.
  - **GitHub**: retained as an alternative; file contents are public.
- Public QR-code share pages; recipients do not need to sign in.
- Server-enforced expiry, download limits, revocation, recent activity, and automatic cleanup routed to the selected file provider.
- Drive OAuth connection is restricted to the configured storage administrator.
- CSRF checks, server-side file-size authorization, Drive upload-finalization verification, signed download tickets, and GitHub Actions tests.

## Architecture note: metadata is still on GitHub

In this implementation, the **file bytes** may be stored in GitHub or in Google Drive depending on the provider selected during upload. Share metadata continues to be stored as JSON in the configured public GitHub storage repository for both options. This includes original filename, share expiry, download counts, event summaries and a keyed owner hash. Therefore Google Drive makes file contents private, but it does **not** make share metadata private. Use non-sensitive filenames and avoid placing private information in file names.

Older GitHub shares continue to use the default `storage_provider: github` when no provider is specified. New shares remember their provider so download, revoke, expiry and cleanup use the matching backend.

## Local setup

1. Install Python 3.10 or newer.
2. Create and activate a virtual environment.
3. Install dependencies: `pip install -r requirements-dev.txt`.
4. Copy `.env.example` to `.env`; configure the GitHub and regular Google sign-in settings.
5. For Google Drive mode, configure the additional storage settings below.
6. Run `python app.py`.

Never commit `.env` or publish OAuth client secrets, refresh tokens, or GitHub tokens. Use a fine-grained GitHub token restricted to `QuickShare-Storage`, with Contents: Read and write permission.

## Google Drive storage setup

Only the storage administrator connects a Drive account; individual users do not connect their own Drive accounts. Once connected, Google Drive is selected by default for every user, while GitHub remains an optional per-upload choice.

1. In the same Google Cloud project, enable the [Google Drive API](https://console.cloud.google.com/apis/library/drive.googleapis.com).
2. In Google Auth Platform → Clients → QuickShare Cloud Web, add this exact authorized redirect URI for the live custom domain: `https://qshare.darshanup.online/auth/google-drive/callback`. Keep the normal login callback too: `https://qshare.darshanup.online/auth/google/callback`.
3. Add `STORAGE_ADMIN_EMAIL` in Vercel with the verified Google email address that will own the private Drive folder and files.
4. Sign in to QuickShare using that account and open **Storage** in the navigation.
5. Click **Connect Google Drive** and consent to `drive.file`. QuickShare asks only for access to files it creates/uses, not general access to all Drive files.
6. Copy the one-time refresh token from the callback page directly into Vercel as `GOOGLE_DRIVE_REFRESH_TOKEN`. Also set `GOOGLE_DRIVE_REDIRECT_URI` to the exact callback URI. Keep the existing `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`.
7. Redeploy. The upload form will then enable the Google Drive option.

The Drive refresh token belongs to the storage administrator and must be kept secret. If it is revoked or expires, the Drive provider reports that it must be reconnected. Google states that refresh tokens for an external OAuth app in Testing status generally expire after seven days when non-basic scopes such as Drive access are requested. For ongoing use, review the app's publishing status in Google Auth Platform; if it is in Testing, expect to reconnect periodically.

## Vercel Environment Variables

Keep the existing GitHub and Google login variables. Add these for Google Drive:

- `STORAGE_ADMIN_EMAIL` = the verified Google email permitted to connect storage.
- `GOOGLE_DRIVE_REDIRECT_URI` = `https://qshare.darshanup.online/auth/google-drive/callback`
- `GOOGLE_DRIVE_REFRESH_TOKEN` = the refresh token generated by the storage connection flow.

The Drive selector is enabled only when `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, and `GOOGLE_DRIVE_REFRESH_TOKEN` are configured. Setting or changing Vercel environment variables requires a redeploy before the running functions use the new values.

## Storage limits and privacy

- **GitHub option:** the file contents are public and may be accessed directly even if a QuickShare link expires or is revoked.
- **Google Drive option:** file content is private in the connected Drive account. QuickShare serves file bytes only after checking expiry, revocation and download count. Google Drive file IDs in metadata do not grant public access by themselves.
- **Both options:** metadata is currently stored in the public GitHub repository. Metadata includes file name, file size, timing, download counts, activity summaries, provider, and a keyed owner hash. Do not use sensitive filenames.
- Cleanup deletes a GitHub file from the latest branch revision (historical Git content may remain) or deletes the file from Google Drive, depending on provider.
- This prototype uses GitHub Contents API as a metadata store and optimistic SHA updates; it is not a transactional database, so high-concurrency guarantees are limited.
- The GitHub storage option remains limited to small demo files. For large private files, Google Drive is required. Vercel authorizes the upload and creates a resumable session. The sender's browser slices the file locally, then posts chunks of at most 4 MiB to a same-origin Vercel endpoint; the function forwards each chunk to Google's upload session. This avoids relying on direct cross-origin requests from the browser to Google's upload host while keeping each Vercel request below its payload limit. Google Drive assembles the chunks into one complete private file.

Downloads are split into individual 4 MiB byte ranges fetched through Vercel and written by the recipient's browser to their chosen file. Each function request/response is bounded, but large transfers still use Vercel invocations and bandwidth.

## Large-file transfers (5 GB standard / 50 GB approved)

**No extra VPS is required.** The browser sends the file directly to Google Drive using a resumable upload session. Google Drive assembles the received ranges into one complete private file; QuickShare does not store separate packet files or run a server-side reassembly job.

For downloads larger than 4 MiB, QuickShare displays a downloader page. In desktop Microsoft Edge or Google Chrome, the user chooses a destination file, then the browser fetches 4 MiB byte ranges through the Vercel app and writes each range directly to disk. The app never buffers the entire multi-gigabyte file in one function response or one large browser Blob. Google Drive remains private; recipients do not need to sign in to the storage administrator's account.

### Permission model

- Standard signed-in Google accounts may upload up to 5 GB per file.
- Add approved verified Google account emails to Vercel `LARGE_UPLOAD_ALLOWED_EMAILS`, comma-separated, to allow those accounts up to 50 GB per file. Leave it blank to approve nobody.
- The tier is enforced server-side; changing the browser UI cannot raise an account's permitted limit.
- Large files must use the central Google Drive option. GitHub storage remains the small-file/public option.

### Important trade-offs

- A 5 GiB file needs about 1,280 upload chunks and 1,280 download ranges; a 50 GiB file needs about 12,800 of each. Upload chunks and download ranges consume Vercel function invocations and network transfer, so check plan quotas/usage before promoting 50 GB transfers to many users.
- If the browser does not support the File System Access API, the fallback is limited to smaller downloads to avoid exhausting browser memory. For 5–50 GB files, use a recent desktop Edge or Chrome browser.
- A link's expiry is embedded in each signed download ticket. The app refreshes the ticket periodically and checks current revocation/expiry when refreshing. Revocation can therefore take effect at the next ticket refresh for an already-started large download.
- A 50 GB per-file limit does not increase the central Google Drive account's available storage quota. Ensure the Drive account has sufficient free space.

## Tests

Run `pytest -v`. GitHub Actions runs the test suite on pushes and pull requests to `main`.
