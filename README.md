# Cloud Storage Orchestrator

A small self-hosted Flask application that combines configured cloud capacity in one dashboard and exposes one file REST API. Uses SQLite for the file catalog and OuthMan for authentication. The interface is light, blue and white, and responsive.

## Run locally

Python 3.11+:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export OUTHMAN_BASE_URL='http://127.0.0.1:10070'
export OUTHMAN_APP_ID='YOUR_GENERATED_APP_ID'
export OUTHMAN_APP_SECRET='YOUR_APP_SECRET'
python app.py
```

Open http://127.0.0.1:10081. `.env.example` documents configuration; `.env` files are **not** automatically loaded. App credentials come from OuthMan's “Connect your apps” screen. Never put them in frontend code.

On first launch, CSO creates `data/bootstrap.key`. Read it locally and enter it under **Link account**, together with an existing OuthMan account, password, and newly generated six-digit code. This creates the first administrator. Later members need an invitation generated under **Team**. Invitations expire after 24 hours and are single use. Enrollment signs the user in; later sign-ins require a fresh OuthMan code.

## Storage behavior

1. Add cloud accounts under **Cloud accounts**. Each contributes the capacity you explicitly allocate to CSO. Two accounts allocated 15 GB each show 30 GB total; GB is decimal (1,000,000,000 bytes).
2. Create a named **Storage space**, set its maximum logical file storage, monthly transfer allowance, and encryption option. Save the generated 32-character hexadecimal API key immediately. It has 128 bits of randomness; a literal 32-bit secret is too small for an API credential.
3. The first successful upload selects the account with the most available configured capacity that can fit the file. All subsequent files for that space stay on that account, even after files are deleted or other clouds are added. A full pinned account returns 507; there is no spillover or migration.
4. Standard-space provider paths are `CSO/<space name>/<requested relative file path>`. New encrypted uploads use random extensionless names under `CSO/objects/`, without exposing the space name or original path to the storage provider. SQLite tracks the provider object ID and logical path. Folder paths are supported through the upload path; there are no standalone empty folders.
5. A space quota counts original file bytes. Cloud usage counts actual stored bytes, including encryption overhead. Space quotas are limits, not reservations, and may exceed the currently connected capacity. Aggregate capacity does not mean a single pinned space can fill all accounts.
6. Upload/download bytes both count toward the monthly UTC transfer allowance. Zero means unlimited. Reaching the allowance blocks transfers. This is a monthly byte budget, **not a network speed throttle**. Limits can be edited; encryption and space names are fixed at creation.
7. Paths cannot escape the space directory. Case-equivalent paths are treated as duplicates to accommodate Dropbox and case-insensitive local filesystems. Existing paths return 409; delete before replacing.

### Cloud accounts

- **Local storage:** immediately usable without cloud credentials. Files live under `data/objects/<cloud-id>/CSO/...`. Allocate only actual disk capacity available to CSO.
- **Google Drive:** choose **Google sign-in (JSON)**, select the downloaded OAuth client file (including `.txt` files such as `gd_api.txt`), then **Continue with Google**. CSO requests permission for files it creates, obtains offline refresh access, checks the account, and saves encrypted credentials after Google returns. Alternatively choose **Access token** to supply pre-authorized tokens manually. Manual tokens can include a refresh token, client ID and client secret for automatic renewal. Uses Drive v3 folders and resumable uploads.
- **Dropbox:** supply a pre-authorized access token with file content read/write and metadata write scopes. CSO creates parent folders before uploading. Renew expired tokens using **Edit account** with a new access token; automatic Dropbox token refresh is not implemented.

### Google JSON setup and Test drive

`gd_api.txt` contains an `installed` OAuth client configuration (Desktop client). It contains a client ID and secret, not an access token or permission to a Drive account. Select it in the **Connect cloud → Google Drive → Google sign-in (JSON)** file picker; CSO does not serve this file publicly or hardcode its contents. Enable the Google Drive API in that Google Cloud project. If the OAuth consent app is in testing, add the Google account under its test users before authorizing.

Desktop clients require CSO on `http://127.0.0.1:10081` or localhost, with the browser on the same machine. For a remotely hosted CSO, use a **Web application** OAuth client and register the exact redirect URI `https://YOUR_CSO_HOST/auth/google/callback`; download its updated JSON. Set `CSO_PUBLIC_URL=https://YOUR_CSO_HOST` when the public origin differs from the origin seen by the backend. Google authentication uses one-use, expiring session-bound state and PKCE; OAuth client secrets and refresh tokens remain encrypted server-side. The callback page completes authorization using the existing CSO login. If your CSO session expires, sign in and start a new connection.

If a saved Google account reports invalid/expired credentials, use **Cloud accounts → Reauthorize**, select the OAuth JSON, and complete consent for the same Google account. This replaces credentials on the existing cloud record, retaining its capacity, assigned spaces, API keys and file catalog. CSO stores the verified Google account identity and rejects switching an established record to a different Google account. For an older account without a saved identity, automatic recovery is allowed only when it has no files; accounts with files need administrator recovery to verify ownership. Client IDs ending in `.apps.googleusercontent.com` and OAuth client secrets are not access tokens; CSO now rejects these in token fields before sending requests.

Use **Test drive** before saving manually supplied credentials, inside **Edit account**, or on an existing cloud account. With JSON sign-in, authorization must finish first; the callback performs the account test automatically. Testing reads account and quota information only, writes no files, and does **not** prove upload/delete permission. Google reports provider quota/usage separately from the capacity you allocate to CSO; tests do not change configured capacity, file usage or transfer counters. Some Google accounts do not report a quota limit. Dropbox testing also requires the `account_info.read` scope.

Account capacity and credentials can be updated under **Edit account**. Always retain the same underlying provider account because pinned files depend on it.

Account capacity is **manually allocated**, not automatically discovered or periodically synced with provider quotas. Existing files outside CSO and changes made directly in cloud storage are not tracked. Cloud status means “Configured”; use **Test drive** for a current access/quota check. Keep the CSO folders under this app's control. Real transfers require valid provider credentials and were not exercised without them.

## REST API

Use `Authorization: Bearer YOUR_SPACE_KEY` (or `X-API-Key`). The key scopes all operations to its space. Admin browser sessions use separate routes and CSRF protection.

```sh
# Upload: response includes id, path, size, created
curl --fail-with-body http://127.0.0.1:10081/api/v1/files \
  -H "Authorization: Bearer $CSO_KEY" \
  -F 'file=@report.pdf' \
  -F 'path=documents/report.pdf'

# List files
curl --fail-with-body http://127.0.0.1:10081/api/v1/files \
  -H "Authorization: Bearer $CSO_KEY"

# Download by returned numeric file ID
curl --fail-with-body http://127.0.0.1:10081/api/v1/files/1 \
  -H "Authorization: Bearer $CSO_KEY" --output report.pdf

# Delete
curl --fail-with-body -X DELETE http://127.0.0.1:10081/api/v1/files/1 \
  -H "Authorization: Bearer $CSO_KEY"

# Space usage, quota, encryption and bandwidth
curl --fail-with-body http://127.0.0.1:10081/api/v1/storage \
  -H "Authorization: Bearer $CSO_KEY"
```

Errors are JSON `{"error":"message"}`. Common codes: 400 invalid path or exceeded transfer allowance, 401 invalid key/session, 403 insufficient role or invalid CSRF, 404 missing file, 409 duplicate name/path, 413 space quota or request size, 507 pinned/available cloud full, 502 provider failure. Rotating a key immediately revokes the previous key.

## Authentication and roles

Follows [OuthMan's supplied integration guide](OuthMan_INTEGRATION.md): backend `/enroll`, local scrypt password verification, backend `/verify` and `/confirm`, immutable identity/app/request checks, expiring browser-bound pending login, and server-side session creation. No password-only fallback. OuthMan secrets/PINs never reach the browser; user keys and cloud tokens are encrypted. Failed login attempts are throttled locally.

- Administrators connect clouds, create spaces, edit limits, rotate keys, invite members and manage roles/access.
- Managers view storage and upload/download/delete files across this workspace. They cannot manage accounts, spaces, keys or membership.
- Revocation invalidates sessions and blocks login while preserving the OuthMan enrollment binding. An admin can restore the role later. Users cannot revoke or change their own role.

The local password hash must match the password used at enrollment. If an OuthMan password changes, an administrator must perform a controlled local hash update after verifying identity; password synchronization/reset is not implemented. OuthMan cannot recover a lost enrollment key. If enrollment succeeds remotely but local persistence fails, manual OuthMan administrator recovery is required; do not repeatedly re-enroll.

## Encryption, backups, and deployment

Optional file encryption uses authenticated Fernet encryption before the provider upload; decryption occurs only on download. New encrypted uploads protect file contents and use opaque provider object names. Original paths remain in the local catalog; ciphertext size and transfer metadata remain visible to the provider. Legacy objects retain their previous provider names. Back up SQLite, local object data and **the encryption key**. Losing that key loses access to encrypted files, cloud credentials and OuthMan user keys. For deployment, set `CSO_ENCRYPTION_KEY` separately from the database backup, plus an independent `CSO_SESSION_SECRET`. Default local keys are generated as mode-0600 files under ignored `data/`.

Waitress binds to loopback by default. Use HTTPS through a reverse proxy and set `CSO_SECURE_COOKIES=1` for network deployment. Configure a reachable HTTPS OuthMan URL. There is no public demo login or authentication bypass.

This is a single-process server with bounded heavy-operation concurrency. Uploads are staged on disk, while compression, encryption and provider adapters still buffer file data in memory. The default file ceiling is 512 MiB (`CSO_MAX_UPLOAD_MIB`), plus 1 MiB for multipart overhead. Short SQLite transactions reserve quota and capacity before parallel cloud transfer. Cloud writes and SQLite commits cannot form a distributed transaction: a crash or uncertain provider response can leave orphaned objects or stale catalog records; inspect provider state before retrying. Automatic orphan reconciliation, antivirus scanning and multi-server deployment are not implemented. Backblaze multipart retries, paginated operation history and safe removal of empty accounts/spaces are available.

## Validation

```sh
.venv/bin/python -m unittest discover -s tests -v
node --check static/app.js
node --check static/login.js
```

Tests use temporary SQLite/local providers and mocked OuthMan responses, with no external accounts. Tests also cover Google OAuth PKCE, callback session binding, expiry and replay rejection, encrypted credential persistence, and read-only Drive checks. Live Google consent, provider/OuthMan acceptance testing and rendered desktop/mobile browser testing remain required in your environment.

Adapter reference: [Google Drive upload protocol](https://developers.google.com/workspace/drive/api/guides/manage-uploads) , [Google Desktop OAuth](https://developers.google.com/identity/protocols/oauth2/native-app), [Google Drive account/quota API](https://developers.google.com/workspace/drive/api/reference/rest/v3/about/get), and [Dropbox HTTP API](https://www.dropbox.com/developers/documentation/http/documentation).


### MEGA setup

Install [rclone](https://rclone.org/install/) on the backend server and ensure `rclone` is on the CSO service user's PATH. No additional Python packages are required. Sign in to your MEGA account at mega.nz once first so its encryption keys are initialized.

In **Connect cloud → MEGA**, enter your account email, password and capacity to allocate, then use **Test drive** and **Connect account**. CSO encrypts credentials in its existing credential store. Upload, download and delete use rclone's MEGA backend; credentials are passed through stdin/environment, never shell commands or command arguments. Serve CSO over HTTPS on remote servers. Local temporary upload files are removed after each transfer.

**Edit account** accepts a replacement email and password together; leave both blank to retain the saved credentials. Only renew credentials for the same MEGA account when files are stored. Test drive reads account quota without uploading a file and does not prove write permission. MEGA deletion uses permanent deletion rather than moving files to its rubbish bin.

This adapter does not support accounts requiring 2FA. Each operation logs in through rclone and has a five-minute timeout, so it is intended for occasional transfers. MEGA can throttle frequent logins and downloads; do not use this implementation for high-volume traffic. There is no lifetime-access guarantee. See [rclone MEGA limitations and authentication](https://rclone.org/mega/).


### Backblaze B2 setup

Use an existing B2 bucket (private is recommended). In Backblaze **Application Keys**, create a key restricted to that bucket, without a filename prefix restriction, allowing `listBuckets`, `listFiles`, `readFiles`, `writeFiles`, and `deleteFiles`. Copy the **keyID** and **applicationKey** when creating the key; the application key is shown once. Find the bucket ID and exact bucket name under **Buckets**.

In CSO **Connect cloud → Backblaze B2**, enter those four fields and the capacity in GB to allocate. Use **Test drive** then connect. Test drive checks key capabilities, bucket identity and file-list access without writing anything. It does not scan bucket usage or establish a provider storage limit; configured capacity is your CSO budget. No rclone, additional Python package, S3 endpoint, or browser authorization is needed. Credentials are encrypted in CSO's existing store; serve remote CSO over HTTPS.

CSO stores the exact B2 file ID/version and filename, downloads that version and permanently deletes that specific version. Object Lock or retention can prevent deletion. Existing external files and old versions aren't counted toward CSO usage. Use a dedicated bucket and avoid external modifications or lifecycle rules that remove active CSO versions. Backblaze storage, API and transfer charges follow your account plan.

To rotate keys in **Edit account**, supply all four fields for the same bucket. Leave all blank to retain the saved connection. Add another cloud record for a different bucket. Application keys work until expired or revoked; CSO obtains fresh authorization tokens automatically for each operation. Live acceptance testing requires your bucket and key.


### Remove a cloud account

Administrators can use **Cloud accounts → Delete account** and confirm removal. Accounts with cataloged files (including zero-byte files) or recorded usage cannot be removed. Delete their files first. Removing an empty account deletes its saved credentials and unpins empty spaces; their API keys and settings remain, and the next upload chooses an available cloud. The provider account or bucket is not deleted.


### Storage space deletion and detected capacity

Administrators can use **Storage spaces → Delete space** to remove an empty space and revoke its API key. Delete its files first, including zero-byte files. The connected cloud account remains.

New cloud connections default to detecting available storage for Google Drive, Dropbox, MEGA and local disk. CSO verifies capacity on the server before saving; Google JSON authorization detects it after consent. If the provider does not report a finite limit, choose a manual budget. Backblaze B2 uses a manual CSO storage budget because buckets do not advertise a fixed available capacity. Existing accounts can use **Refresh capacity** to replace their budget with currently available provider storage plus the bytes already tracked by CSO. This is a snapshot, not continuous monitoring or a reservation; accounts sharing the same physical disk/provider quota share its available space.


For a non-empty space, **Delete space** offers **Delete all files in this space**. Select it and type the exact space name to confirm. CSO deletes cataloged provider objects one at a time, persists each successful deletion and updates usage. The space and API key are removed only after every file is deleted. If a provider fails (for example, B2 retention blocks deletion), completed deletions remain committed and the space/key and remaining files stay available for retry. Provider-specific deletion semantics still apply; external bucket files are not affected.


### Running Operations and deletion verification

Administrators can open **Running Operations** for persistent, paginated history of authenticated storage actions and account/space changes, including actor, target, start/update timestamps, status and error details. Uploads/downloads and individual file deletions show item progress, not network byte progress. Space deletion is queued in a background worker and reports progress after each provider file is deleted; its final step removes the space and revokes its key. History lives in `data/operations.sqlite3`, separate from catalog writes, so polling remains responsive during deletion. History begins with this feature; prior actions cannot be reconstructed.

Completed file deletions are committed individually. Failed or interrupted jobs keep the remaining catalog entries and space for inspection/retry. On restart, unfinished jobs are marked **Interrupted** and are not silently resumed. Run one CSO server process for this embedded worker/history design. Do not start a second CSO process against the same data directory while operations run.

Backblaze deletes the exact cataloged version and checks `b2_get_file_info` to confirm it is absent before removing the local record. Provider retention/permission failures are shown as failures; the file stays cataloged. Files not cataloged by CSO, older external versions and empty provider folders are not bulk-purged. MEGA requests permanent deletion. Dropbox provider deletion can still use its recoverable deleted-file semantics.

### Encrypted file contents and opaque cloud names

Encrypted spaces use `cryptography.fernet.Fernet` authenticated encryption on file contents before upload. New encrypted objects receive random 256-bit extensionless names, with no original filename, extension or space name sent as the object path. CSO keeps the original logical path in its local SQLite catalog and restores the original filename when downloading. This is server-side client encryption: the CSO server holds the encryption key and can decrypt files. Keep `data/encryption.key` and the catalog backed up securely; losing the key loses access to encrypted contents. Ciphertext size and transfer metadata are still visible to the provider.

Existing encrypted contents remain compatible. Existing cloud-visible names are not automatically rewritten; new uploads use opaque names. To protect a legacy filename, download the file through CSO, delete its old object through CSO, then re-upload into the encrypted space. Do not rename provider objects outside CSO.


### Administrator settings

**Settings → OuthMan integration** shows the configured base URL, app ID and whether a secret is configured. Enter a replacement secret to rotate it; a blank field keeps the current secret. Saved integration settings are encrypted in SQLite, applied immediately, and loaded on restart in preference to environment variables. Existing user bindings prevent changing the server/app identity through this form; switching identities requires a relinking/recovery workflow. Initial enrollment still requires server environment configuration.

**Settings → Upload compression → Maximum ZIP (level 9)** compresses new uploads using ZIP DEFLATE at level 9 before encryption, and keeps compression only when the ZIP archive is smaller than the original. This is optional and defaults to Off. Already-compressed media/archives often do not shrink. Downloads restore the original content and filename transparently; existing files retain their original storage format. Compression settings can be changed without losing compatibility with prior files. Cloud usage counts actual stored bytes (including encryption), while space quotas and bandwidth continue to count original logical bytes. Upload success reports actual storage savings. Maximum compression can add CPU time.

Running Operations uses one row per operation with a small progress bar. Status, action and search filters apply to all stored history before pagination. Use row details to see longer messages without enlarging the list.


A 317 MB file fits under the default 512 MiB request limit. Multipart form overhead also counts; choose a limit above the file size. Set `CSO_MAX_UPLOAD_MIB` and restart to change it. Compression runs after request parsing, so it cannot bypass this limit. Uploads, compression and encryption are currently buffered in server memory; large encrypted files can require several times their original size in RAM. Space quota counts original bytes, while cloud capacity must fit the actual stored bytes including encryption overhead.


### Per-space upload limit and background uploads

Use **Storage spaces → Edit limits → Maximum file size (MiB)** to set a limit for an existing space, or set it when creating a space. Default is the server ceiling (512 MiB). This setting applies to the original file size, before compression/encryption, and is checked on both console and REST uploads. It cannot exceed the server ceiling `CSO_MAX_UPLOAD_MIB`; change that environment value and restart if a higher ceiling is required. The request parser reserves another 1 MiB for multipart overhead. Space quota and monthly transfer checks are separate.

The console upload dialog closes immediately after local validation. A preflight request creates a Running Operations entry before the browser sends file bytes; session-bound, one-use upload tokens prevent request replay. Keep the browser tab open until the server reports the file has been received. Then the server stages the file with owner-only permissions and returns HTTP 202, while a background worker compresses/encrypts and transfers it to the cloud. Temporary upload files are removed on completion/failure; a server restart marks jobs interrupted and removes stale staging files rather than automatically retrying them. Managers receive transfer feedback; administrators can track Running Operations. REST uploads remain synchronous unless using the console's background-upload workflow.

Backblaze objects larger than 64 MiB use 10 MiB parts, per-part checksum confirmation, up to three attempts for transient part failures, and a 10-minute transfer timeout per part. Progress advances with confirmed cloud parts. Incomplete multipart uploads are canceled on pre-finalization failures where possible. Uncertain finalization is not automatically repeated, to avoid duplicating objects; inspect B2 if an operation fails during finalization. Small B2 transfers also use a longer transfer timeout. Error messages distinguish timeout, interrupted connection, and provider access failures. Compression/encryption still buffer one cloud transfer in memory; this is not a streaming-encryption implementation.


Running Operations displays known progress as a percentage. The small current-step text under each bar opens a live detail dialog with the full target, actor, timestamps, status, percentage and a scrollable timestamped activity log. New steps are stored in `operation_events` in the operation history database and loaded incrementally. Unknown totals remain indeterminate rather than showing a fabricated percentage. Existing operations have only their current snapshot; earlier step-by-step history cannot be reconstructed. The dialog follows new text only while the log is near its bottom, so reading older messages does not jump the scroll position.


### Queue, concurrency and pause controls

**Settings → Concurrent heavy operations** accepts 1–4 and defaults to 1. Background uploads and space deletions enter the bounded worker queue. Synchronous REST transfers and downloads also wait for an execution slot; configuration and operation polling remain separate. Raising the limit permits parallel work; lowering it lets already-running work finish while new work waits. Parallel uploads reserve space quota, bandwidth and cloud capacity in short catalog transactions before transferring outside the catalog lock, preventing oversubscription and duplicate paths.

**Pause / Resume** is available for queued/background operations. Pause is cooperative: the current compression chunk or provider request finishes before stopping at a checkpoint; it does not cancel an in-flight upload part. Paused operations retain their in-memory context but release their execution permit. The fixed pool contains up to four resident background jobs, so four paused jobs can occupy the pool until one resumes. Resume returns work to the execution limit. Browser-to-server receipt and synchronous HTTP requests cannot be paused with these controls. Restarting the single server marks queued/running/paused work interrupted rather than resuming it automatically.

New upload progress covers acceptance/receipt, compression, encryption, cloud transfer and the catalog commit using overall workflow weights. It does not reset when multipart transfer starts and is not an estimate of elapsed time or transferred browser bytes. The current-step text beneath the list bar is hidden on completed, failed and interrupted rows; details and the audit log remain available.

### Compression choices per storage space

**Storage spaces → Edit limits** includes **Maximum single file size (MiB)** and **Compression**. Choices are workspace default, Off, ZIP at DEFLATE level 9, Bzip2 at level 9, LZMA/XZ at preset 9 with EXTREME, and Automatic. Automatic tries ZIP, Bzip2 and XZ on the complete file and compares each result with the original, keeping the smallest. Compression is lossless and performed before encryption; downloads transparently restore the original file. Codec metadata is stored per file, so changing a space's setting affects only future uploads. Maximum compression cannot guarantee savings for existing archives, videos or other high-entropy files.

Extreme LZMA can require hundreds of MiB for its encoder alone, in addition to file/compression/encryption buffers; Automatic is CPU-intensive because it evaluates multiple codecs. Keep concurrency at 1 for large files on limited-memory servers. Cooperative pauses occur between input chunks; an encoder flush may finish before pausing.

New encrypted uploads store the same authenticated Fernet ciphertext in a compact binary envelope instead of Base64 text. This removes most of the avoidable Base64 expansion; it does not weaken encryption. Existing Base64 Fernet objects remain readable without rewriting them. Existing cloud objects are not recompressed or converted automatically.

## Raspberry Pi deployment

See [deployment_instructions.md](deployment_instructions.md) for 64-bit ARM installation, systemd, HTTPS, memory limits and migration.

## BackMan configuration backups

CSO supports privileged, recovery-key-encrypted configuration snapshots stored through an encrypted cloud Storage space. BackMan controls scheduling and retention using `/api/v1/config-backups`; administrators provision its dedicated capability through `/api/admin/config-backup`. See [Backup_API.md](Backup_API.md#10-backman-back-up-csos-own-configuration) for setup, requests, contents and offline recovery.
