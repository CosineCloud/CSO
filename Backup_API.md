# CSO Backup API

External applications and backup scripts can upload, list, download, and delete backup files using the API key generated for a **Storage space** in CSO. A backup is an ordinary file; For ordinary application backups, create your own archive. CSO can also generate its own configuration snapshots for BackMan using the dedicated API described below.

## 1. Configure access

1. In CSO, connect a cloud account and create a Storage space with the required quota, maximum single-file size, compression, and encryption settings.
2. Save the generated **Storage space API key**. If necessary, use **Rotate API key** to generate a replacement; the old key stops working immediately.
3. Configure your backup client:

```sh
export CSO_URL='http://127.0.0.1:10081'
export CSO_KEY='YOUR_STORAGE_SPACE_API_KEY'
```

This local address works only on the CSO server itself. For a client on another machine, configure a reachable server address and use HTTPS, for example `https://cso.example.com`. CSO binds to loopback by default; external access requires appropriate hosting/reverse-proxy configuration. Setting the client URL alone does not expose the server.

Send this header with every request:

```http
Authorization: Bearer YOUR_STORAGE_SPACE_API_KEY
```

`X-API-Key: YOUR_STORAGE_SPACE_API_KEY` is also supported. Use the Storage space key, not OuthMan credentials or cloud-provider keys. API clients do not need a browser login or CSRF token. Each key permits uploads, listing, downloads, and deletion **only within its own space**. There are no separate read-only or upload-only permissions for these keys. Store keys in your server's secret configuration.

## 2. Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/v1/files` | Upload a backup |
| GET | `/api/v1/files` | List backups in the key's space |
| GET | `/api/v1/files/{id}` | Download a backup by numeric ID |
| DELETE | `/api/v1/files/{id}` | Permanently delete a backup |
| GET | `/api/v1/storage` | Read space quota and usage |

All examples below use the environment variables above. `curl --fail-with-body` returns a nonzero exit code for HTTP errors.

## 3. Upload a backup

Create your backup archive first, then send **multipart/form-data**:

```sh
curl --fail-with-body \
  -H "Authorization: Bearer $CSO_KEY" \
  -F 'file=@/path/to/database-backup.tar.gz' \
  -F 'path=backups/database/2026-11-08/database-backup.tar.gz' \
  "$CSO_URL/api/v1/files"
```

- `file`: required; the local file's contents.
- `path`: optional; the logical path within the Storage space. Defaults to the uploaded filename. Use `/` for nested folders. Absolute paths, backslashes, empty segments, `.` and `..` segments are rejected.
- Use a unique path for every backup. Existing paths are not overwritten; a duplicate or concurrently uploading path returns `409`.
- Do not manually set the multipart Content-Type header; your HTTP client must generate its boundary.

Successful upload returns **201 Created**, for example:

```json
{
  "id": 42,
  "path": "backups/database/2026-11-08/database-backup.tar.gz",
  "size": 1048576,
  "stored_size": 1048576,
  "compression": "none",
  "saved_bytes": 0,
  "created": "2026-11-08T18:00:00+00:00"
}
```

Save `id` for later download/deletion. `size` is the original file size in bytes; `stored_size` is the provider payload size after configured compression/encryption. Actual compression and savings vary by file.

Uploads through this external API are **synchronous**: the response waits for an execution slot, processing, and cloud transfer. Keep the connection open and configure client/proxy timeouts for large backups. The browser's background-upload ticket flow is separate and is not used here. There is no resumable-upload endpoint. If the connection is lost, list files and check the path before retrying: the upload may have completed despite the lost response.

## 4. List backups

```sh
curl --fail-with-body \
  -H "Authorization: Bearer $CSO_KEY" \
  "$CSO_URL/api/v1/files"
```

Successful response: **200 OK**.

```json
{
  "files": [
    {
      "id": 42,
      "path": "backups/database/2026-11-08/database-backup.tar.gz",
      "size": 1048576,
      "created": "2026-11-08T18:00:00+00:00"
    }
  ]
}
```

An empty space returns `{"files": []}`. The endpoint returns all file metadata in the space; filter paths and sort timestamps in your client. There are no server-side search, pagination, or retention parameters on this endpoint.

## 5. Download a backup

Replace `42` with the returned file ID:

```sh
curl --fail-with-body \
  -H "Authorization: Bearer $CSO_KEY" \
  --output restored-backup.tar.gz.part \
  "$CSO_URL/api/v1/files/42" && \
  mv restored-backup.tar.gz.part restored-backup.tar.gz
```

Successful response: **200 OK** with binary file contents. CSO automatically decrypts and decompresses configured storage transformations, delivering the original uploaded bytes. Your own `.tar.gz`, `.zip`, or database backup format remains intact. The attachment filename is the final component of the logical path.

Only use the downloaded file after the request succeeds. With `--fail-with-body`, an error response may be written into the `.part` file; the command above renames it only on success. For backup verification, save a SHA-256 checksum at creation and compare it after restoring.

## 6. Delete a backup

Deletion is permanent. Confirm the ID from the listing before calling:

```sh
curl --fail-with-body \
  -X DELETE \
  -H "Authorization: Bearer $CSO_KEY" \
  "$CSO_URL/api/v1/files/42"
```

Successful response: **200 OK**:

```json
{"ok": true}
```

CSO deletes the provider object before removing its catalog entry and releasing recorded storage usage. This does not purge unrelated files or other versions created outside CSO. A repeated delete returns `404`; a file belonging to another Storage space also returns `404`.

For retention, have your backup client list files, select expired backups within its own path prefix, and delete their IDs. Verify a new backup before deleting the previous usable copy. CSO does not implement automatic retention through these endpoints.

## 7. Check quota and transfer allowance

```sh
curl --fail-with-body \
  -H "Authorization: Bearer $CSO_KEY" \
  "$CSO_URL/api/v1/storage"
```

Example **200 OK** response:

```json
{
  "name": "Database backups",
  "quota": 10737418240,
  "used": 1048576,
  "encrypted": true,
  "bandwidth_limit": 0,
  "bandwidth_used": 1048576
}
```

All sizes are bytes. `bandwidth_limit: 0` means unlimited. Space quota and monthly transfer accounting use original file sizes; cloud capacity accounting uses stored payload sizes. Both uploads and downloads consume the space's transfer allowance. The effective single-file upload limit is the lower of the space setting and the server ceiling. Provider capacity and transfer restrictions also apply.

## 8. Errors and retries

Errors normally return JSON:

```json
{"error": "Explanation of the failure."}
```

| HTTP status | Meaning / client action |
| --- | --- |
| 400 | Invalid input/path or exceeded transfer allowance; inspect `error`. |
| 401 | Missing, invalid, or rotated Storage space API key. |
| 404 | File not found in this key's space. |
| 409 | Duplicate/in-progress path, or space being deleted; inspect `error`. |
| 413 | Request/file limit or Storage space quota exceeded. |
| 507 | No suitable cloud capacity or pinned cloud is full. |
| 502 | Cloud provider operation failed; inspect `error`. |

A proxy may return its own error format or size/timeout limit. Do not blindly retry uploads after an ambiguous timeout; check the file listing first. Retry transient failures with a bounded delay/backoff. There is no idempotency-key or overwrite API.

## 9. Where backup data is stored

For a cloud provider, CSO stages uploads temporarily on the server while queued/processing/transferring, then deletes the staged copy when the operation succeeds or fails. Crash leftovers are cleaned on server startup. Completed file contents remain with the selected provider; the server retains the catalog, configuration, and operation history. A space assigned to **Local storage** retains its files on the CSO server instead.

Compression and encryption are controlled by the Storage space settings, not per-upload API parameters. Preserve and back up CSO's catalog and encryption-key material separately: provider objects alone are not a complete recovery plan for CSO-managed encrypted backups.

## 10. BackMan: back up CSO's own configuration

BackMan can ask CSO to create a recoverable configuration snapshot and upload it using CSO's existing provider integration. BackMan owns scheduling and retention. These endpoints require a **separate privileged BackMan key**, not a Storage space key.

### Administrator setup

Create a dedicated **encrypted** Storage space for configuration backups. It must already be pinned to a non-local cloud account. A first successful ordinary upload assigns the cloud; that test file can then be deleted. Allow enough quota for the catalog archive.

While signed in as a CSO administrator, open the browser developer console on CSO and run the following, replacing `SPACE_ID` with the numeric space ID from `/api/admin/state`:

```js
const state = await fetch('/api/admin/state').then(r => r.json());
console.table(state.spaces.map(({id, name, encrypted, cloud_id}) => ({id, name, encrypted, cloud_id})));
// Replace SPACE_ID below with the chosen numeric ID.
const response = await fetch('/api/admin/config-backup', {
  method: 'POST',
  headers: {'Content-Type': 'application/json', 'X-CSRF-Token': state.csrf},
  body: JSON.stringify({space_id: SPACE_ID})
});
console.log(response.status, await response.json());
```

The `201` response contains `{ "agent": "BackMan", "space_id": 1, "key": "..." }`. Save the returned key in BackMan's secret configuration. Repeating setup rotates/revokes the previous key. `GET /api/admin/config-backup` returns the configured destination without revealing the key. Administrator `DELETE /api/admin/config-backup` (with CSRF header) revokes access without deleting existing backups. Setup is API-based; there is currently no settings-page form for this feature.

Generate a separate **Fernet recovery key** on BackMan's machine (requires Python's `cryptography` package):

```sh
python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Store this key securely in BackMan and in your disaster-recovery records. CSO uses it for one request and does not persist it. Losing it makes the snapshot unrecoverable. It is different from CSO's existing encryption key. Keep a record of which recovery key protects each snapshot, especially after rotating keys.

### Create and upload a configuration backup

```sh
export BACKMAN_KEY='PRIVILEGED_KEY_FROM_ADMIN_SETUP'
export BACKMAN_RECOVERY_KEY='FERNET_RECOVERY_KEY'

curl --fail-with-body -X POST \
  -H "Authorization: Bearer $BACKMAN_KEY" \
  -H "X-CSO-Backup-Encryption-Key: $BACKMAN_RECOVERY_KEY" \
  "$CSO_URL/api/v1/config-backups"
```

No request body is needed. CSO creates a consistent SQLite snapshot, packages it, encrypts it with BackMan's recovery key, and uploads it through the configured space. Its normal compression, encryption, quota, transfer allowance and concurrency settings also apply. The call is synchronous. Creation/download/deletion appear in Running Operations with actor **BackMan**.

A successful `201` response includes the ordinary uploaded-file fields plus `agent: "BackMan"`, `format: "cso-config-v1"`, and `sha256` of the downloadable encrypted archive. Paths are unique: `cso-config/<UTC timestamp>-<random>.cso-backup`. Save the response ID, checksum and recovery-key reference in BackMan. A timeout can have an ambiguous result: list before retrying.

The snapshot contains:

- `cso.sqlite3`: cloud credentials, spaces, file catalog, users, and saved application settings.
- `encryption.key`: the effective CSO master key, including when configured through the environment.
- `runtime.json`: effective OuthMan configuration, public URL and upload ceiling.
- `manifest.json`: format version and creation time.

Sessions, pending sign-ins, invitations, login-attempt counters, upload tickets/reservations, and the BackMan access configuration are cleared in the snapshot. The archive does **not** contain uploaded file contents, local-provider files, operation history, application source/dependencies, reverse-proxy configuration, or every environment variable. The snapshot precedes its own upload/catalog entry. Other cloud files must remain available for recovery; this is a configuration/catalog backup, not cloud file versioning. Cloud objects deleted after the snapshot cannot be recovered from this archive.

Creation returns `409` if uploads have active reservations or a space deletion is in progress; BackMan should retry later. Queued/receiving uploads are not included until committed. The destination must remain encrypted and assigned to a cloud provider.

### List, download and delete configuration backups

```sh
# Returns {"backups": [{"id":42,"path":"cso-config/...","size":1234,"created":"..."}]}
curl --fail-with-body -H "Authorization: Bearer $BACKMAN_KEY" \
  "$CSO_URL/api/v1/config-backups"

# Returns the recovery-key-encrypted archive, not a plaintext configuration dump.
curl --fail-with-body -H "Authorization: Bearer $BACKMAN_KEY" \
  --output cso-config.cso-backup.part \
  "$CSO_URL/api/v1/config-backups/42" && \
  mv cso-config.cso-backup.part cso-config.cso-backup

# Permanently remove an expired configuration backup; response {"ok":true}.
curl --fail-with-body -X DELETE -H "Authorization: Bearer $BACKMAN_KEY" \
  "$CSO_URL/api/v1/config-backups/42"
```

Only backups under `cso-config/` in the configured destination are accessible through these routes. Missing/out-of-scope IDs return `404`; invalid BackMan keys return `401`. BackMan decides which IDs to delete. The recovery key is required only for creation and offline decryption, not for listing/download/deletion.

### Disaster recovery

Download at least one recent archive into BackMan's independent storage. If CSO is unavailable, its API cannot serve backups: retain the provider account access and object-location records independently if relying on direct cloud recovery. An encrypted CSO storage object has an additional storage-encryption layer; BackMan's downloaded archive has already had that outer layer removed.

On a trusted recovery machine, decrypt a downloaded archive:

```python
import os
from pathlib import Path
from cryptography.fernet import Fernet

source = Path('cso-config.cso-backup').read_bytes()
plain = Fernet(os.environ['BACKMAN_RECOVERY_KEY'].encode()).decrypt(source)
# Contains credentials and master keys; keep the directory private.
out = Path('cso-recovery')
out.mkdir(mode=0o700, exist_ok=True)
fd = os.open(out / 'configuration.zip', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'wb') as target:
    target.write(plain)
```

Verify the stored SHA-256 checksum before decrypting. Inspect the ZIP and extract only the expected files listed above into a private directory. Stop CSO before restoring. Preserve the current data directory first, then replace `cso.sqlite3` and `encryption.key` in the target `CSO_DATA_DIR` with the recovered files. Keep their permissions restricted. Ensure `CSO_ENCRYPTION_KEY`, if set, matches the recovered key or remove that override. Restore effective runtime settings from `runtime.json` to the appropriate environment variables (`PUBLIC_URL` maps to `CSO_PUBLIC_URL`; convert `MAX_UPLOAD_FILE_BYTES` to MiB for `CSO_MAX_UPLOAD_MIB`). Saved OuthMan DB settings take precedence over environment settings. Use a compatible CSO code version and configure its serving port/HTTPS independently.

Start CSO, sign in again, verify cloud access and a representative file download, then reconfigure BackMan to obtain a new privileged key. There is intentionally no API that replaces a running CSO database.
