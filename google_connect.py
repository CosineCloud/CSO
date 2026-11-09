"""Server-side Google authorization with PKCE and browser/session-bound one-use state."""
import base64
import hashlib
import json
import secrets
import time
from urllib.parse import urlencode, urlparse

import requests
from flask import jsonify, request, session, render_template
from providers import Provider, ProviderError

DRIVE_SCOPE = 'https://www.googleapis.com/auth/drive.file'


def register_google_routes(app, db, cipher, user, admin, positive, valid_name, lock):
    with db() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS google_pending(
            state_hash TEXT PRIMARY KEY, user_id INTEGER, session_hash TEXT,
            expires REAL, payload TEXT)''')

    @app.post('/api/admin/google/connect')
    @admin('admin')
    def connect():
        data = request.get_json() or {}
        config = data.get('client_config', {})
        if not isinstance(config, dict):
            raise ValueError('Select the downloaded Google OAuth client JSON.')
        kind = 'installed' if 'installed' in config else 'web'
        client = config.get(kind)
        if not isinstance(client, dict):
            raise ValueError('Use a Desktop or Web OAuth client JSON, not a service account or API key.')
        client_id, client_secret = client.get('client_id'), client.get('client_secret')
        if not all(isinstance(x, str) and 1 <= len(x) <= 1024 for x in (client_id, client_secret)):
            raise ValueError('The JSON must contain client_id and client_secret.')
        origin = app.config.get('PUBLIC_URL') or request.host_url.rstrip('/')
        parsed = urlparse(origin)
        if parsed.scheme not in ('http', 'https') or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
            raise ValueError('Configure CSO_PUBLIC_URL as the application origin.')
        loopback = parsed.hostname in ('127.0.0.1', 'localhost', '::1')
        if kind == 'installed' and not loopback:
            raise ValueError('Desktop OAuth JSON requires CSO on localhost and a browser on the same machine. Use a Web OAuth client for a remote server.')
        if not loopback and parsed.scheme != 'https':
            raise ValueError('Google authorization requires HTTPS for a remote server.')
        redirect_uri = origin.rstrip('/') + '/auth/google/callback'
        if kind == 'web' and redirect_uri not in client.get('redirect_uris', []):
            raise ValueError('Add this authorized redirect URI to the Google Web client, then download its updated JSON: ' + redirect_uri)
        cloud_id = data.get('cloud_id')
        revision = None
        with db() as c:
            if cloud_id is not None:
                if type(cloud_id) is not int:
                    raise ValueError('Invalid cloud account ID.')
                cloud = c.execute("SELECT * FROM clouds WHERE id=? AND provider='google'",(cloud_id,)).fetchone()
                if not cloud:
                    raise ValueError('Google cloud account not found.')
                name, capacity = cloud['name'], cloud['capacity']
                revision = hashlib.sha256(cloud['credentials'].encode()).hexdigest()
            else:
                name = valid_name(data.get('name'))
                capacity = None if data.get('auto_capacity') is True else positive(data, 'capacity')
                if c.execute('SELECT id FROM clouds WHERE name=?', (name,)).fetchone():
                    raise ValueError('A cloud account with this name already exists.')
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        payload = dict(name=name, capacity=capacity, client_id=client_id, client_secret=client_secret,
                       verifier=verifier, redirect_uri=redirect_uri, cloud_id=cloud_id, revision=revision)
        with db() as c:
            c.execute('DELETE FROM google_pending WHERE expires<?', (time.time(),))
            c.execute('INSERT INTO google_pending VALUES(?,?,?,?,?)', (
                hashlib.sha256(state.encode()).hexdigest(), user()['id'],
                hashlib.sha256(session['login'].encode()).hexdigest(), time.time()+600,
                cipher.encrypt(json.dumps(payload).encode()).decode()))
        params = dict(client_id=client_id, redirect_uri=redirect_uri, response_type='code',
                      scope=DRIVE_SCOPE, state=state, access_type='offline', prompt='consent',
                      code_challenge=challenge, code_challenge_method='S256')
        return jsonify(authorization_url='https://accounts.google.com/o/oauth2/v2/auth?'+urlencode(params), redirect_uri=redirect_uri)

    @app.get('/auth/google/callback')
    def callback():
        # Strict cookies are withheld on the Google -> CSO navigation. Render a bridge
        # without touching session; same-origin fetches then recover the existing login.
        response = app.make_response(render_template('google_callback.html', result={
            'state': request.args.get('state', '')[:256],
            'code': request.args.get('code', '')[:4096],
            'denied': bool(request.args.get('error'))}))
        response.headers['Referrer-Policy'] = 'no-referrer'
        return response

    @app.post('/api/admin/google/complete')
    @admin('admin')
    def complete():
        data = request.get_json() or {}
        state, code = data.get('state', ''), data.get('code', '')
        if not isinstance(state, str) or not isinstance(code, str) or not 16 <= len(state) <= 256 or len(code)>4096:
            raise ValueError('Invalid Google authorization result. Start again from Cloud accounts.')
        hashed = hashlib.sha256(state.encode()).hexdigest()
        with lock, db() as c:
            c.execute('BEGIN IMMEDIATE')
            pending = c.execute('SELECT * FROM google_pending WHERE state_hash=?', (hashed,)).fetchone()
            if not pending or pending['expires']<time.time() or pending['user_id']!=user()['id'] or pending['session_hash']!=hashlib.sha256(session['login'].encode()).hexdigest():
                raise ValueError('Google connection expired or belongs to another browser session. Start again.')
            payload = json.loads(cipher.decrypt(pending['payload'].encode()))
            c.execute('DELETE FROM google_pending WHERE state_hash=?', (hashed,))
        # Consume state before contacting Google; uncertain exchanges require a new flow.
        if data.get('denied') or not code:
            raise ValueError('Google authorization was cancelled. No cloud account was added.')
        try:
            response = requests.post('https://oauth2.googleapis.com/token', data={
                'client_id':payload['client_id'], 'client_secret':payload['client_secret'],
                'code':code, 'code_verifier':payload['verifier'],
                'redirect_uri':payload['redirect_uri'], 'grant_type':'authorization_code'},
                timeout=15, allow_redirects=False)
            if response.status_code != 200:
                raise ValueError('Google authorization exchange failed. Check the OAuth client configuration and start again.')
            tokens = response.json()
            if not isinstance(tokens, dict) or not all(isinstance(tokens.get(k), str) and tokens[k] for k in ('access_token', 'refresh_token')):
                raise ValueError('Google did not return offline access. Start again and grant CSO file access.')
            if DRIVE_SCOPE not in tokens.get('scope', '').split():
                raise ValueError('Google Drive file access was not granted. Start again and allow file access.')
        except requests.RequestException as exc:
            raise ValueError('Google token service is unavailable. Start a new connection attempt.') from exc
        credentials = {k:tokens[k] for k in ('access_token','refresh_token')}
        credentials.update({k:payload[k] for k in ('client_id','client_secret')})
        probe = Provider({'id':0,'provider':'google'}, credentials, app.config['DATA_DIR']).check()
        identity = probe.get('account_id')
        if not isinstance(identity,str) or not identity:
            raise ValueError('Google did not return an account identity. The connection was not changed.')
        credentials['_google_account_id'] = identity
        with lock, db() as c:
            c.execute('BEGIN IMMEDIATE')
            cid = payload.get('cloud_id')
            encrypted = cipher.encrypt(json.dumps(credentials).encode()).decode()
            if cid is not None:
                cloud = c.execute("SELECT * FROM clouds WHERE id=? AND provider='google'",(cid,)).fetchone()
                if not cloud or hashlib.sha256(cloud['credentials'].encode()).hexdigest()!=payload['revision']:
                    raise ValueError('Cloud credentials changed during authorization. Start reauthorization again.')
                saved = json.loads(cipher.decrypt(cloud['credentials'].encode()))
                expected_identity = saved.get('_google_account_id')
                has_files = c.execute('SELECT 1 FROM files JOIN spaces ON spaces.id=files.space_id WHERE spaces.cloud_id=? LIMIT 1',(cid,)).fetchone()
                if expected_identity and expected_identity!=identity:
                    raise ValueError('Choose the same Google account originally connected to this drive. Existing credentials were preserved.')
                if not expected_identity and (has_files or cloud['used']):
                    raise ValueError('Cannot verify the original Google identity for this account with stored files. Administrator recovery is required; existing credentials were preserved.')
                c.execute('UPDATE clouds SET credentials=? WHERE id=?',(encrypted,cid))
            else:
                if payload['capacity'] is None:
                    available = probe.get('available')
                    if type(available) is not int or available <= 0:
                        raise ValueError('Google did not report available storage. Connect again using a manual storage budget.')
                    payload['capacity'] = available
                cid = c.execute('INSERT INTO clouds(name,provider,capacity,credentials) VALUES(?,?,?,?)', (
                    payload['name'], 'google', payload['capacity'], encrypted)).lastrowid
        return jsonify(ok=True, id=cid, message='Google Drive connected.', test=probe)
