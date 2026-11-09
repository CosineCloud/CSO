# Deploy CSO on Raspberry Pi / ARM

## Compatibility and sizing

CSO's Python/Flask/Waitress server, SQLite catalog and browser frontend contain no x86-only code. Cloud adapters use HTTPS APIs; only MEGA additionally requires the native `rclone` executable on PATH. No Node.js build is needed to run the web app.

Recommended deployment: Raspberry Pi 4 or 5, **64-bit Raspberry Pi OS Lite**, Python **3.11 or 3.13**, at least **4 GB RAM**, and an SSD for data/staging. These are sizing recommendations from the implementation, not hardware benchmarks. All requirements, including transitive native dependencies, successfully resolved to Linux ARM64 wheels for CPython 3.13 during this review; the project has not yet been run on physical Raspberry Pi hardware. Raspberry Pi OS 64-bit supports Pi 4/5 and other listed models; see the [official OS downloads](https://www.raspberrypi.com/software/operating-systems/).

The `cryptography` dependency supports ARM64 Linux and publishes Linux wheels; see its [installation instructions](https://cryptography.io/en/46.0.3/installation/). [rclone downloads](https://rclone.org/downloads/) include ARM64 and 32-bit ARM builds. Prefer 64-bit OS: 32-bit ARM has not been validated for this app, can need source builds, and has tighter address-space limits.

**Memory matters:** upload staging uses disk, but compression, Fernet encryption, provider upload and downloads use complete in-memory buffers, sometimes with several copies. A 317 MB backup can use substantially more RAM than its file size. Start with **one concurrent operation**, a **128 MiB** ceiling, and compression off or ZIP. XZ extreme and dynamic compression are CPU/memory intensive; dynamic mode tries multiple algorithms. Increase limits only after observing real Pi memory usage. A 4 GB recommendation does not guarantee all 512 MiB uploads succeed.

Use **one CSO process**. Its embedded queue, startup cleanup and locks are single-process; do not launch multiple Gunicorn workers, replicas or overlapping services against the same data directory.

## 1. Install the operating system and packages

Install 64-bit Raspberry Pi OS Lite with SSH enabled, connect to the Pi, then:

```sh
uname -m
python3 --version
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip ca-certificates curl
```

`uname -m` should report `aarch64`. Python must include `sqlite3`, `bz2` and `lzma`, normally present in Raspberry Pi OS's Python:

```sh
python3 -c 'import sqlite3, bz2, lzma; print("Python standard-library dependencies OK")'
```

For MEGA only:

```sh
sudo apt install -y rclone
rclone version
rclone help backends
```

Confirm the installed rclone supports MEGA. If the OS package is too old, follow the official rclone installation instructions for the Pi's architecture. Backblaze, Google Drive, Dropbox and Local storage do not require rclone.

## 2. Create a service account and install the app

```sh
sudo useradd --system --user-group --home-dir /var/lib/cso --create-home --shell /usr/sbin/nologin cso
sudo git clone https://github.com/CosineCloud/CSO.git /opt/cso
sudo python3 -m venv /opt/cso/.venv
sudo /opt/cso/.venv/bin/python -m pip install --upgrade pip
sudo /opt/cso/.venv/bin/python -m pip install -r /opt/cso/requirements.txt
sudo install -d -o cso -g cso -m 700 /var/lib/cso
sudo install -d -o root -g root -m 700 /etc/cso
```

If the repository is private, clone using an authenticated deployment account; do not put access tokens in clone URLs or configuration committed to Git. The code and virtual environment can stay root-owned; the service only needs write access to its data directory and temporary directory.

If pip attempts to build `cryptography`, first upgrade pip and confirm a 64-bit supported Python/OS combination. Source builds additionally require a C compiler, Python/OpenSSL/libffi headers and a sufficiently recent Rust toolchain; use the linked cryptography instructions rather than assuming the distro Rust version is sufficient.

## 3. Configure the service environment

Create `/etc/cso/cso.env` using `sudoedit`:

```sh
sudoedit /etc/cso/cso.env
```

Example (replace the OuthMan values):

```ini
CSO_DATA_DIR=/var/lib/cso
CSO_HOST=127.0.0.1
PORT=10081
CSO_MAX_UPLOAD_MIB=128
OUTHMAN_BASE_URL=http://127.0.0.1:10070
OUTHMAN_APP_ID=REPLACE_WITH_16_HEX_CHARACTERS
OUTHMAN_APP_SECRET=REPLACE_WITH_32_HEX_CHARACTERS
CSO_SECURE_COOKIES=0
```

```sh
sudo chmod 600 /etc/cso/cso.env
```

This is a systemd EnvironmentFile: use `NAME=value`, without `export`. CSO does **not** automatically load a project `.env`. OuthMan must be running and reachable from the Pi; `127.0.0.1:10070` is valid only if OuthMan runs on that same Pi. Use the correct trusted HTTPS origin when it is hosted elsewhere. App IDs and secrets must match the OuthMan app registration. Values saved later in CSO's OuthMan settings override environment credentials.

On first start, CSO generates `encryption.key`, `session.key`, and `bootstrap.key` in `/var/lib/cso`. Preserve `encryption.key` together with the database: it decrypts cloud credentials and encrypted file payloads. Avoid regenerating or changing it on an existing installation. The data directory also holds the SQLite catalog, operations history, temporary `pending_uploads/`, and local-provider `objects/`.

## 4. Install a systemd service

Create `/etc/systemd/system/cso.service`:

```ini
[Unit]
Description=CSO internal storage console
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=cso
Group=cso
WorkingDirectory=/opt/cso
EnvironmentFile=/etc/cso/cso.env
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=PATH=/opt/cso/.venv/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/opt/cso/.venv/bin/python /opt/cso/app.py
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/cso

[Install]
WantedBy=multi-user.target
```

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now cso
sudo systemctl status cso --no-pager
sudo journalctl -u cso -n 100 --no-pager
curl --fail http://127.0.0.1:10081/ -o /dev/null
```

Do not use port **10080**: browsers block it as unsafe. The app defaults to **10081**.

The hardening above permits writes to `/var/lib/cso` and a private temporary directory. If placing data on an SSD, mount it persistently, set `CSO_DATA_DIR` to the new owned directory, update `ReadWritePaths`, and add a `RequiresMountsFor=/your/mount/path` entry under `[Unit]`. Do not let the service start before its data volume is mounted.

## 5. Access the console

For initial setup, keep CSO bound to loopback and use an SSH tunnel **from your regular computer**:

```sh
ssh -N -L 10081:127.0.0.1:10081 YOUR_PI_USER@YOUR_PI_ADDRESS
```

Open `http://localhost:10081` on that computer. Choose another local tunnel port if a local CSO already uses 10081. Read the one-time initial enrollment token on the Pi:

```sh
sudo cat /var/lib/cso/bootstrap.key
```

Complete CSO/OuthMan enrollment in the browser. Configure cloud accounts, Storage spaces, file limits and compression. In **Settings**, keep maximum concurrent operations at **1** initially.

For a private LAN HTTP deployment you can bind `CSO_HOST=0.0.0.0` and restart, then visit `http://PI_ADDRESS:10081`. Use a trusted private network; API keys and credentials require HTTPS when transported outside the SSH tunnel/trusted local setup. A BackMan agent on another host needs a reachable origin, not its own `localhost`.

### HTTPS reverse proxy

For normal remote/internal-domain access, terminate TLS at a reverse proxy and keep CSO bound to loopback. Set:

```ini
CSO_PUBLIC_URL=https://cso.example.com
CSO_SECURE_COOKIES=1
```

Set secure cookies only when the browser actually uses HTTPS. Obtain/install the correct certificate for your domain. Example Nginx server block, after installing Nginx and provisioning those certificate paths:

```nginx
server {
    listen 443 ssl;
    server_name cso.example.com;
    ssl_certificate /etc/letsencrypt/live/cso.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/cso.example.com/privkey.pem;

    # 128 MiB file ceiling plus multipart overhead; keep aligned with CSO.
    client_max_body_size 129m;
    client_body_timeout 900s;

    location / {
        proxy_pass http://127.0.0.1:10081;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_connect_timeout 30s;
        proxy_send_timeout 3600s;
        proxy_read_timeout 3600s;
        proxy_request_buffering off;
        proxy_buffering off;
    }
}
```

Validate with `sudo nginx -t`, reload Nginx, and restart CSO after changing its environment. Restrict access using your private network/VPN/firewall policy. Configure Google OAuth with the exact public callback `https://cso.example.com/auth/google/callback` and a Web application OAuth client for a remotely hosted Pi. A Google Desktop OAuth client requires a loopback origin in the user's browser; use the SSH tunnel or the documented web-client flow.

Timeouts above are examples: a synchronous backup request can wait in CSO's queue and perform slow cloud transfers. BackMan/client timeouts must also allow that duration.

## 6. Verify on the actual Pi

Run the project's tests under the installed Pi interpreter:

```sh
cd /opt/cso
sudo /opt/cso/.venv/bin/python -m unittest discover -s tests -q
```

Tests use isolated temporary data. They mock provider calls and do not prove live cloud connectivity. Then use the console's **Test drive**, upload a representative small backup, download and compare SHA-256, and delete it. Repeat with a realistic large file while watching:

```sh
free -h
sudo journalctl -u cso -f
```

Check for out-of-memory kills if the service restarts during encryption/compression. Reduce the file ceiling, concurrency or compression intensity if necessary. Ensure free staging disk accommodates queued uploads as well as active ones; operation concurrency does not limit the number of incoming files staged on disk.

## 7. Backups, migration and updates

See [Backup_API.md](Backup_API.md) for external backup CRUD and BackMan configuration snapshots. BackMan needs a separately provisioned privileged key and an independent recovery encryption key. Keep downloaded recovery archives outside the Pi as well as cloud copies. Configuration snapshots exclude user file contents, local-provider objects and operations history.

For a complete cold data backup, wait for Running Operations to finish, stop CSO, copy `/var/lib/cso` into secured backup storage, and restart. Also preserve the service/environment/proxy configuration securely. Never publish these files to GitHub.

To migrate this existing installation, stop the source instance after its operations finish, transfer its **entire data directory** securely to `/var/lib/cso`, preserve the effective encryption key/environment overrides, set ownership to `cso:cso`, and start the Pi. Do not run both instances against the same catalog/cloud namespace. Local-provider objects must migrate too. Start from an empty data directory only when creating a separate new installation.

Before updating, take that stopped-service backup, then:

```sh
cd /opt/cso
sudo systemctl stop cso
sudo git -C /opt/cso pull --ff-only
sudo /opt/cso/.venv/bin/python -m pip install -r /opt/cso/requirements.txt
sudo /opt/cso/.venv/bin/python -m unittest discover -s /opt/cso/tests -q
sudo systemctl start cso
```

Restarting interrupts in-flight jobs; staged leftovers are cleared on startup. Keep backups before schema changes; rolling code back alone may not reverse a database migration.
