import hashlib
import io
import json
import sqlite3
import zipfile
import unittest
from pathlib import Path
from unittest.mock import patch
from cryptography.fernet import Fernet, InvalidToken
import test_app


class ConfigBackupTests(unittest.TestCase):
    setUp = test_app.AppTests.setUp
    tearDown = test_app.AppTests.tearDown
    admin = test_app.AppTests.admin
    post = test_app.AppTests.post
    space = test_app.AppTests.space

    def configure(self):
        self.admin()
        cipher = Fernet(Path(self.temp.name, 'encryption.key').read_bytes())
        self.db.execute('INSERT INTO clouds(id,name,provider,capacity,used,credentials) VALUES(1,?,?,?,?,?)',
                        ('Backup cloud', 'b2', 10**9, 0, cipher.encrypt(b'{}').decode()))
        self.db.commit()
        space_key = self.space(quota=10**8)
        self.db.execute('UPDATE spaces SET cloud_id=1')
        self.db.commit()
        response = self.post('/api/admin/config-backup', {'space_id': 1})
        self.assertEqual(response.status_code, 201, response.json)
        return response.json['key'], space_key

    def test_authorization_and_destination(self):
        self.assertEqual(self.client.post('/api/v1/config-backups').status_code, 401)
        self.assertEqual(self.post('/api/admin/config-backup', {'space_id': 1}).status_code, 401)
        token, space_key = self.configure()
        self.assertEqual(self.client.post('/api/v1/config-backups', headers={'Authorization': 'Bearer '+space_key}).status_code, 401)
        headers = {'Authorization': 'Bearer '+token}
        self.assertEqual(self.client.post('/api/v1/config-backups', headers=headers).status_code, 400)
        self.db.execute('UPDATE spaces SET encrypted=0'); self.db.commit()
        headers['X-CSO-Backup-Encryption-Key'] = Fernet.generate_key().decode()
        self.assertEqual(self.client.post('/api/v1/config-backups', headers=headers).status_code, 409)

    def test_snapshot_roundtrip_and_scoping(self):
        token, _ = self.configure()
        recovery = Fernet.generate_key()
        headers = {'Authorization': 'Bearer '+token, 'X-CSO-Backup-Encryption-Key': recovery.decode()}
        objects = {}
        class FakeProvider:
            def __init__(self, *args): pass
            def put(self, path, content): objects[path] = content; return path
            def get(self, path): return objects[path]
            def delete(self, path): del objects[path]
        with patch('app.Provider', FakeProvider):
            response = self.client.post('/api/v1/config-backups', headers=headers)
            self.assertEqual(response.status_code, 201, response.json)
            fid = response.json['id']
            self.assertEqual(len(self.client.get('/api/v1/config-backups', headers=headers).json['backups']), 1)
            download = self.client.get(f'/api/v1/config-backups/{fid}', headers=headers)
            self.assertEqual(download.status_code, 200)
            self.assertEqual(hashlib.sha256(download.data).hexdigest(), response.json['sha256'])
            with self.assertRaises(InvalidToken): Fernet(Fernet.generate_key()).decrypt(download.data)
            with zipfile.ZipFile(io.BytesIO(Fernet(recovery).decrypt(download.data))) as archive:
                self.assertEqual(archive.read('encryption.key'), Path(self.temp.name, 'encryption.key').read_bytes())
                self.assertEqual(json.loads(archive.read('manifest.json'))['format'], 'cso-config-v1')
                snapshot = Path(self.temp.name, 'restore.sqlite3'); snapshot.write_bytes(archive.read('cso.sqlite3'))
                with sqlite3.connect(snapshot) as c:
                    self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                    self.assertEqual(c.execute('SELECT count(*) FROM sessions').fetchone()[0], 0)
                    self.assertEqual(c.execute("SELECT count(*) FROM settings WHERE name='config_backup'").fetchone()[0], 0)
                    self.assertEqual(c.execute('SELECT count(*) FROM clouds').fetchone()[0], 1)
            self.assertEqual(self.client.get('/api/v1/config-backups/9999', headers=headers).status_code, 404)
            self.assertEqual(self.client.delete(f'/api/v1/config-backups/{fid}', headers=headers).status_code, 200)
            self.assertFalse(objects)
            self.assertEqual(self.client.delete(f'/api/v1/config-backups/{fid}', headers=headers).status_code, 404)
        self.client.delete('/api/admin/config-backup', headers=self.csrf)
        self.assertEqual(self.client.get('/api/v1/config-backups', headers=headers).status_code, 401)
