"""Privileged BackMan configuration snapshots, stored through CSO's providers."""
import hashlib
import io
import json
import secrets
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet
from flask import g, jsonify, request


def register_config_backup(app, *, admin, db, lock, encryption_key, scheduler,
                           operations, perform_upload, file_operation):
    def settings(c):
        row = c.execute("SELECT value FROM settings WHERE name='config_backup'").fetchone()
        return json.loads(row['value']) if row else None

    def destination(c, config):
        return c.execute('''SELECT spaces.*,clouds.provider FROM spaces
            JOIN clouds ON clouds.id=spaces.cloud_id WHERE spaces.id=?''',
            (config['space_id'],)).fetchone()

    @app.route('/api/admin/config-backup', methods=['GET', 'POST', 'DELETE'])
    @admin('admin')
    def configure_config_backup():
        with lock, db() as c:
            if request.method == 'GET':
                config = settings(c)
                return jsonify(configured=bool(config), space_id=config['space_id'] if config else None,
                               agent='BackMan')
            if request.method == 'DELETE':
                c.execute("DELETE FROM settings WHERE name='config_backup'")
                return jsonify(ok=True)
            payload = request.get_json() or {}
            sid = payload.get('space_id')
            if type(sid) is not int:
                return jsonify(error='space_id must be an integer.'), 400
            space = destination(c, {'space_id': sid})
            if not space or space['provider'] == 'local' or not space['encrypted'] or space['deleting']:
                return jsonify(error='Select an encrypted storage space already assigned to a cloud provider.'), 400
            token = secrets.token_urlsafe(48)
            c.execute("INSERT OR REPLACE INTO settings VALUES('config_backup',?)",
                      (json.dumps({'space_id': sid, 'token_hash': hashlib.sha256(token.encode()).hexdigest()}),))
            return jsonify(agent='BackMan', space_id=sid, key=token), 201

    def authorize():
        header = request.headers.get('Authorization', '')
        token = header[7:] if header.startswith('Bearer ') else ''
        with db() as c:
            config = settings(c)
        if not config or not token or not secrets.compare_digest(
                hashlib.sha256(token.encode()).hexdigest(), config['token_hash']):
            return None
        return config

    @app.route('/api/v1/config-backups', methods=['GET', 'POST'])
    @app.route('/api/v1/config-backups/<int:fid>', methods=['GET', 'DELETE'])
    def config_backups(fid=None):
        config = authorize()
        if not config:
            return jsonify(error='Invalid BackMan configuration-backup key.'), 401
        if fid is None and request.method == 'GET':
            with db() as c:
                rows = c.execute("SELECT id,path,size,created FROM files WHERE space_id=? AND path LIKE 'cso-config/%' ORDER BY id DESC", (config['space_id'],))
                return jsonify(backups=[dict(r) for r in rows])
        if fid is not None:
            with db() as c:
                entry = c.execute("SELECT id FROM files WHERE id=? AND space_id=? AND path LIKE 'cso-config/%'", (fid, config['space_id'])).fetchone()
            if not entry:
                return jsonify(error='Configuration backup not found.'), 404
        oid = operations.start('config_backup' if fid is None else ('delete_config_backup' if request.method == 'DELETE' else 'download_config_backup'),
                               'CSO configuration' if fid is None else f'Configuration backup #{fid}', 'BackMan')
        g.operation_id = oid
        if fid is not None:
            # Authorization is the dedicated BackMan capability, already checked above.
            return file_operation(fid, admin_access=True, delete=request.method == 'DELETE')
        recovery_key = request.headers.get('X-CSO-Backup-Encryption-Key', '')
        try:
            recovery_cipher = Fernet(recovery_key.encode('ascii'))
        except (ValueError, UnicodeError):
            return jsonify(error='Supply a valid Fernet recovery key in X-CSO-Backup-Encryption-Key.'), 400

        def create():
            with tempfile.TemporaryDirectory(prefix='cso-config-') as directory:
                snapshot = Path(directory) / 'cso.sqlite3'
                with lock, db() as source:
                    current = settings(source)
                    if current != config:
                        return jsonify(error='BackMan configuration changed. Retry with the current key.'), 409
                    space = destination(source, config)
                    if not space or not space['encrypted'] or space['provider'] == 'local' or space['deleting']:
                        return jsonify(error='Backup destination must remain an encrypted cloud storage space.'), 409
                    if source.execute('SELECT 1 FROM upload_reservations LIMIT 1').fetchone() or source.execute('SELECT 1 FROM spaces WHERE deleting=1 LIMIT 1').fetchone():
                        return jsonify(error='Cloud mutations are active. Retry after uploads and space deletions finish.'), 409
                    with sqlite3.connect(snapshot) as target:
                        source.backup(target)
                with sqlite3.connect(snapshot) as target:
                    for table in ('sessions', 'pending', 'invitations', 'attempts', 'upload_tickets', 'upload_reservations'):
                        target.execute('DELETE FROM ' + table)
                    # A restored instance must explicitly re-authorize BackMan.
                    target.execute("DELETE FROM settings WHERE name='config_backup'")
                    target.commit()
                    target.execute('VACUUM')
                created = datetime.now(timezone.utc)
                runtime = {key: app.config[key] for key in ('OUTHMAN_BASE_URL', 'OUTHMAN_APP_ID', 'OUTHMAN_APP_SECRET', 'PUBLIC_URL', 'MAX_UPLOAD_FILE_BYTES')}
                buffer = io.BytesIO()
                with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
                    archive.write(snapshot, 'cso.sqlite3')
                    archive.writestr('encryption.key', encryption_key)
                    archive.writestr('runtime.json', json.dumps(runtime))
                    archive.writestr('manifest.json', json.dumps({'format': 'cso-config-v1', 'created': created.isoformat(), 'agent': 'BackMan', 'includes_file_contents': False}))
                encrypted = recovery_cipher.encrypt(buffer.getvalue())
                path = 'cso-config/' + created.strftime('%Y%m%dT%H%M%SZ') + '-' + secrets.token_hex(8) + '.cso-backup'
                response = app.make_response(perform_upload(config['space_id'], path, encrypted, oid))
                if response.status_code == 201:
                    payload = response.get_json()
                    payload.update(format='cso-config-v1', agent='BackMan', sha256=hashlib.sha256(encrypted).hexdigest())
                    return jsonify(payload), 201
                return response
        return scheduler.run(oid, create)
