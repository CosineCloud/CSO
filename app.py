import hashlib
import io
import json
import math
import os
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
import zipfile
import tempfile
from urllib.parse import urlparse
from datetime import datetime, timezone
from contextlib import contextmanager, nullcontext
from functools import wraps
from pathlib import Path

import requests
from cryptography.fernet import Fernet, InvalidToken
from flask import Flask, jsonify, request, session, render_template, send_file, g
from werkzeug.exceptions import HTTPException
from werkzeug.security import generate_password_hash, check_password_hash
from providers import Provider, ProviderError, validate_credentials
from google_connect import register_google_routes
from operation_log import OperationLog
from job_scheduler import JobScheduler
from compression_codec import compress_content, decompress_content, MODES
from encrypted_payload import encrypt_content, decrypt_content
import bz2
import lzma


def create_app(test_config=None):
    app = Flask(__name__)
    root = Path(os.environ.get('CSO_DATA_DIR', 'data'))
    root.mkdir(parents=True, exist_ok=True)
    def secret_file(name, factory):
        path = root / name
        if not path.exists():
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as f:
                f.write(factory())
        return path.read_text().strip()
    app.config.update(SECRET_KEY=os.environ.get('CSO_SESSION_SECRET') or secret_file('session.key',lambda:secrets.token_hex(32)),
        PUBLIC_URL=os.environ.get('CSO_PUBLIC_URL','').rstrip('/'),
        DATA_DIR=str(root), DATABASE=str(root/'cso.sqlite3'), MAX_CONTENT_LENGTH=(int(os.environ.get('CSO_MAX_UPLOAD_MIB','512'))+1)*1024*1024,
        MAX_UPLOAD_FILE_BYTES=int(os.environ.get('CSO_MAX_UPLOAD_MIB','512'))*1024*1024,
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Strict',
        SESSION_COOKIE_SECURE=os.environ.get('CSO_SECURE_COOKIES')=='1', PERMANENT_SESSION_LIFETIME=28800,
        OUTHMAN_BASE_URL=os.environ.get('OUTHMAN_BASE_URL','http://127.0.0.1:10070').rstrip('/'),
        OUTHMAN_APP_ID=os.environ.get('OUTHMAN_APP_ID',''), OUTHMAN_APP_SECRET=os.environ.get('OUTHMAN_APP_SECRET',''),
        BOOTSTRAP_TOKEN=os.environ.get('CSO_BOOTSTRAP_TOKEN') or secret_file('bootstrap.key',lambda:secrets.token_urlsafe(24)))
    if test_config:
        app.config.update(test_config)
    encryption_key = os.environ.get('CSO_ENCRYPTION_KEY') or secret_file('encryption.key',lambda:Fernet.generate_key().decode())
    cipher = Fernet(encryption_key)
    lock = threading.RLock()
    @contextmanager
    def db():
        con = sqlite3.connect(app.config['DATABASE'], timeout=90)
        con.row_factory = sqlite3.Row
        con.execute('PRAGMA foreign_keys=ON')
        try:
            with con:
                yield con
        finally:
            con.close()
    with db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT COLLATE NOCASE UNIQUE NOT NULL,password TEXT NOT NULL,role TEXT NOT NULL,provider TEXT NOT NULL,remote_id INTEGER NOT NULL,user_key TEXT NOT NULL, UNIQUE(provider,remote_id));
        CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY,user_id INTEGER REFERENCES users(id),expires REAL);
        CREATE TABLE IF NOT EXISTS pending(id TEXT PRIMARY KEY,user_id INTEGER,browser TEXT,expires REAL,completed INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS invitations(token TEXT PRIMARY KEY,role TEXT,expires REAL,used INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS clouds(id INTEGER PRIMARY KEY,name TEXT COLLATE NOCASE UNIQUE,provider TEXT,capacity INTEGER,used INTEGER DEFAULT 0,credentials TEXT);
        CREATE TABLE IF NOT EXISTS spaces(id INTEGER PRIMARY KEY,name TEXT COLLATE NOCASE UNIQUE,quota INTEGER,used INTEGER DEFAULT 0,encrypted INTEGER,bandwidth_limit INTEGER,bandwidth_used INTEGER DEFAULT 0,month TEXT,cloud_id INTEGER REFERENCES clouds(id),key_hash TEXT UNIQUE);
        CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,space_id INTEGER REFERENCES spaces(id),path TEXT,size INTEGER,stored_size INTEGER,object_id TEXT,created TEXT,UNIQUE(space_id,path));
        CREATE TABLE IF NOT EXISTS upload_reservations(operation_id INTEGER PRIMARY KEY,space_id INTEGER,path TEXT,cloud_id INTEGER,size INTEGER,stored_size INTEGER,UNIQUE(space_id,path));
        CREATE TABLE IF NOT EXISTS upload_tickets(token TEXT PRIMARY KEY,operation_id INTEGER,user_id INTEGER,session_hash TEXT,space_id INTEGER,path TEXT,size INTEGER,expires REAL);
        CREATE TABLE IF NOT EXISTS settings(name TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS attempts(subject TEXT PRIMARY KEY,count INTEGER,expires REAL);
        ''')
    with db() as c:
        if 'compression' not in [r['name'] for r in c.execute('PRAGMA table_info(files)')]:
            c.execute("ALTER TABLE files ADD COLUMN compression TEXT NOT NULL DEFAULT 'none'")
        saved=c.execute("SELECT value FROM settings WHERE name='outhman'").fetchone()
        if saved:
            config=json.loads(cipher.decrypt(saved['value'].encode()))
            app.config.update(OUTHMAN_BASE_URL=config['base_url'],OUTHMAN_APP_ID=config['app_id'],OUTHMAN_APP_SECRET=config['app_secret'])

    with db() as c:
        if 'max_upload_mib' not in [r['name'] for r in c.execute('PRAGMA table_info(spaces)')]:
            c.execute('ALTER TABLE spaces ADD COLUMN max_upload_mib INTEGER NOT NULL DEFAULT 512')
        fields=[r['name'] for r in c.execute('PRAGMA table_info(spaces)')]
        if 'compression_mode' not in fields:
            c.execute("ALTER TABLE spaces ADD COLUMN compression_mode TEXT NOT NULL DEFAULT 'inherit'")
        if 'deleting' not in fields:
            c.execute('ALTER TABLE spaces ADD COLUMN deleting INTEGER NOT NULL DEFAULT 0')
        c.execute('UPDATE spaces SET deleting=0')
        c.execute('DELETE FROM upload_reservations')
        c.execute('DELETE FROM upload_tickets')
    upload_dir=root/'pending_uploads'
    upload_dir.mkdir(mode=0o700,exist_ok=True)
    # This embedded worker runs in a single process. Interrupted uploads are not resumed.
    for staged in upload_dir.glob('*.upload'):
        staged.unlink()
    operations=OperationLog(root/'operations.sqlite3')
    app.extensions['operations']=operations
    with db() as c:
        execution=c.execute("SELECT value FROM settings WHERE name='max_concurrent'").fetchone()
    scheduler=JobScheduler(operations,int(execution['value']) if execution else 1)
    app.extensions['scheduler']=scheduler

    def step(oid,completed=None,message=None):
        scheduler.checkpoint()
        if oid is not None:
            operations.update(oid,total=10000,completed=completed,message=message)


    def error(message,status=400):
        return jsonify(error=message),status
    def csrf():
        if 'csrf' not in session:
            session['csrf'] = secrets.token_urlsafe(32)
        return session['csrf']
    def user():
        with db() as c:
            row = c.execute('SELECT users.* FROM sessions JOIN users ON users.id=sessions.user_id WHERE token=? AND expires>?',(session.get('login',''),time.time())).fetchone()
            return dict(row) if row else None
    def admin(role=None):
        def decorator(fn):
            @wraps(fn)
            def wrapped(*args,**kwargs):
                account = user()
                if not account:
                    return error('Please sign in.',401)
                if role and account['role'] != role:
                    return error('Administrator access required.',403)
                if request.method not in ('GET','HEAD') and not secrets.compare_digest(request.headers.get('X-CSRF-Token',''),csrf()):
                    return error('Invalid CSRF token. Refresh and try again.',403)
                return fn(*args,**kwargs)
            return wrapped
        return decorator
    def remote(endpoint, payload, expected=200):
        if not app.config['OUTHMAN_APP_ID'] or not app.config['OUTHMAN_APP_SECRET']:
            raise ValueError('Configure OuthMan app credentials on the server first.')
        try:
            r = requests.post(app.config['OUTHMAN_BASE_URL']+'/api/v1/'+endpoint,json=payload,headers={'X-OuthMan-App':app.config['OUTHMAN_APP_ID'],'Authorization':'Bearer '+app.config['OUTHMAN_APP_SECRET']},timeout=10,allow_redirects=False)
            if r.status_code != expected:
                # Only classify documented errors; never forward secret-bearing response bodies.
                detail = None
                try:
                    failure = r.json()
                    if isinstance(failure, dict):
                        detail = failure.get('error') or failure.get('message')
                except ValueError:
                    pass
                if r.status_code == 401 and detail == 'Invalid app credentials.':
                    raise ValueError('OuthMan rejected the server app credentials. Check the app ID, complete app secret, and whether the app was revoked; restart CSO after changing environment variables.')
                if r.status_code == 401 and detail == 'Verification failed or expired.':
                    raise ValueError('OuthMan could not verify this account or code. Use your matching OuthMan username/password and a newly generated code. If it persists, ask the OuthMan administrator to check the app/user binding.')
                if r.status_code == 409:
                    raise ValueError('This OuthMan account is already linked to this app. Sign in using the saved local account. If its enrollment key was lost, OuthMan administrator recovery is required.')
                if r.status_code == 429:
                    raise ValueError('OuthMan has temporarily throttled authentication. Wait for its 15-minute window; clearing CSO attempts does not clear the OuthMan throttle.')
                if r.status_code == 403:
                    raise ValueError('OuthMan denied the backend request. Check the server integration configuration and access policy.')
                raise ValueError(f'OuthMan {endpoint} returned HTTP {r.status_code}. Check the OuthMan server configuration before retrying.')
            data = r.json()
            if not isinstance(data,dict):
                raise ValueError('Invalid OuthMan response.')
            return data
        except (requests.RequestException, json.JSONDecodeError) as exc:
            raise ValueError('OuthMan is unavailable. Try again with a fresh code.') from exc
    def new_session(uid, con=None):
        old = session.get('login','')
        session.clear()
        session.permanent = True
        session['login'] = secrets.token_urlsafe(32)
        with (nullcontext(con) if con is not None else db()) as c:
            c.execute('DELETE FROM sessions WHERE token=? OR expires<?',(old,time.time()))
            c.execute('INSERT INTO sessions VALUES(?,?,?)',(session['login'],uid,time.time()+28800))
    @app.errorhandler(Exception)
    def errors(exc):
        if isinstance(exc, HTTPException):
            if exc.code==413:
                limit=app.config['MAX_CONTENT_LENGTH']//(1024*1024)
                return error(f'Upload request exceeds the {limit} MiB server limit. Choose a smaller file or increase CSO_MAX_UPLOAD_MIB on the server. Compression happens after this limit is checked.',413)
            return error(exc.description,exc.code)
        if isinstance(exc,(ValueError,ProviderError)):
            return error(str(exc),400 if isinstance(exc,ValueError) else 502)
        if isinstance(exc,sqlite3.IntegrityError):
            return error('That name, path, or account already exists.',409)
        app.logger.error('Request failed (%s)',type(exc).__name__)
        return error('The operation failed. Check server configuration and retry.',500)
    @app.after_request
    def headers(response):
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['X-Frame-Options']='DENY'
        response.headers['Referrer-Policy']='same-origin'
        response.headers['Cache-Control']='no-store'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; font-src 'self'; img-src 'self' data:; frame-ancestors 'none'"
        return response
    @app.before_request
    def audit_start():
        # Capture authenticated actions only, never credentials, request bodies or API keys.
        if not request.path.startswith(('/api/admin/','/api/v1/')) or request.endpoint in ('state','list_operations','prepare_upload','fail_browser_upload','config_backups'):
            return
        if request.method=='GET' and request.endpoint not in ('admin_download','api_file'):
            return
        account=user()
        actor=account['username'] if account else None
        if request.path.startswith('/api/admin/'):
            if not account:
                return
            if request.method not in ('GET','HEAD') and not secrets.compare_digest(request.headers.get('X-CSRF-Token',''),csrf()):
                return
            if account['role']!='admin' and request.endpoint not in ('admin_upload','admin_download','admin_delete'):
                return
        elif not actor:
            with db() as c:
                space=space_for_key(c)
                if not space:
                    return
                actor='API: '+space['name']
        if request.endpoint=='admin_upload' and request.headers.get('X-CSO-Upload-Token'):
            token=hashlib.sha256(request.headers['X-CSO-Upload-Token'].encode()).hexdigest()
            with db() as c:
                c.execute('BEGIN IMMEDIATE')
                ticket=c.execute('SELECT * FROM upload_tickets WHERE token=?',(token,)).fetchone()
                if not ticket or ticket['user_id']!=account['id'] or ticket['session_hash']!=hashlib.sha256(session['login'].encode()).hexdigest() or ticket['expires']<time.time():
                    return error('Upload authorization expired or belongs to another session. Start a new upload.',409)
                c.execute('DELETE FROM upload_tickets WHERE token=?',(token,))
            g.upload_ticket=dict(ticket)
            g.operation_id=ticket['operation_id']
            operations.update(g.operation_id,status='running',total=10000,completed=1000,message='Receiving file on the server.')
            return
        action=request.endpoint or request.method.lower()
        if request.method=='DELETE' and action=='api_file':
            action='delete_file'
        elif request.method=='GET':
            action='download_file'
        resource=request.path
        if request.view_args:
            sid=request.view_args.get('sid')
            if sid is not None:
                with db() as c:
                    space=c.execute('SELECT name FROM spaces WHERE id=?',(sid,)).fetchone()
                if space:
                    resource=f"Space #{sid}: {space['name']}"
        oid=operations.start(action,resource,actor)
        if oid is None:
            return error('This storage space already has a running deletion. Check Running Operations.',409)
        g.operation_id=oid

    @app.after_request
    def audit_finish(response):
        oid=getattr(g,'operation_id',None)
        if oid and not getattr(g,'operation_background',False):
            success=response.status_code<400
            payload=response.get_json(silent=True) if response.is_json else None
            message='Completed.' if success else (payload or {}).get('error','Request failed.')
            operations.finish(oid,success,message)
        return response

    def compression_mode():
        with db() as c:
            row=c.execute("SELECT value FROM settings WHERE name='compression'").fetchone()
        return row['value'] if row else 'off'

    @app.get('/api/admin/settings')
    @admin('admin')
    def get_settings():
        return jsonify(outhman=dict(base_url=app.config['OUTHMAN_BASE_URL'],app_id=app.config['OUTHMAN_APP_ID'],
            secret_configured=bool(app.config['OUTHMAN_APP_SECRET'])),compression=dict(mode=compression_mode()),execution=dict(max_concurrent=scheduler.limit))

    @app.patch('/api/admin/settings')
    @admin('admin')
    def save_settings():
        data=request.get_json() or {}
        if not isinstance(data,dict):
            raise ValueError('Invalid settings.')
        with lock,db() as c:
            c.execute('BEGIN IMMEDIATE')
            config=dict(base_url=app.config['OUTHMAN_BASE_URL'],app_id=app.config['OUTHMAN_APP_ID'],app_secret=app.config['OUTHMAN_APP_SECRET'])
            if 'outhman' in data:
                incoming=data['outhman']
                if not isinstance(incoming,dict):
                    raise ValueError('Invalid OuthMan settings.')
                for key in ('base_url','app_id'):
                    if key in incoming:
                        if not isinstance(incoming[key],str):
                            raise ValueError('Invalid OuthMan settings.')
                        config[key]=incoming[key].strip().rstrip('/') if key=='base_url' else incoming[key].strip()
                if incoming.get('app_secret'):
                    config['app_secret']=incoming['app_secret']
                parsed=urlparse(config['base_url'])
                if (parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password
                        or parsed.query or parsed.fragment or parsed.path not in ('','/')
                        or (parsed.scheme=='http' and parsed.hostname not in ('localhost','127.0.0.1','::1'))):
                    raise ValueError('Use an HTTPS OuthMan origin, or HTTP on localhost.')
                if not re.fullmatch('[a-fA-F0-9]{16}',config['app_id']) or not isinstance(config['app_secret'],str) or not re.fullmatch('[a-fA-F0-9]{32}',config['app_secret']):
                    raise ValueError('OuthMan requires a 16-character hex app ID and a 32-character hex app secret.')
                if c.execute('SELECT 1 FROM users LIMIT 1').fetchone() and (config['base_url']!=app.config['OUTHMAN_BASE_URL'] or config['app_id']!=app.config['OUTHMAN_APP_ID']):
                    return error('Existing users are linked to this OuthMan server and app ID. Keep that identity when rotating the secret; switching integrations requires user relinking.',409)
                c.execute("INSERT INTO settings VALUES('outhman',?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",(cipher.encrypt(json.dumps(config).encode()).decode(),))
            if 'compression' in data:
                compression=data['compression']
                if not isinstance(compression,dict) or compression.get('mode') not in MODES:
                    raise ValueError('Choose off, zip, bz2, xz or auto compression.')
                c.execute("INSERT INTO settings VALUES('compression',?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",(compression['mode'],))
            concurrency=scheduler.limit
            if 'execution' in data:
                execution=data['execution']
                if not isinstance(execution,dict) or type(execution.get('max_concurrent')) is not int or not 1<=execution['max_concurrent']<=4:
                    raise ValueError('Concurrent operations must be between 1 and 4.')
                concurrency=execution['max_concurrent']
                c.execute("INSERT INTO settings VALUES('max_concurrent',?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",(str(concurrency),))
            c.commit()
            scheduler.set_limit(concurrency)
            app.config.update(OUTHMAN_BASE_URL=config['base_url'],OUTHMAN_APP_ID=config['app_id'],OUTHMAN_APP_SECRET=config['app_secret'])
        return jsonify(ok=True)

    @app.post('/api/admin/operations/<int:oid>/pause')
    @admin('admin')
    def pause_operation(oid):
        if not scheduler.pause(oid):
            return error('This operation cannot be paused at this stage, or has finished.',409)
        return jsonify(ok=True)

    @app.post('/api/admin/operations/<int:oid>/resume')
    @admin('admin')
    def resume_operation(oid):
        if not scheduler.resume(oid):
            return error('This operation cannot be resumed, or has finished.',409)
        return jsonify(ok=True)

    @app.get('/api/admin/operations/<int:oid>')
    @admin('admin')
    def operation_detail(oid):
        try:
            after=max(0,int(request.args.get('after',0)))
            limit=min(500,max(1,int(request.args.get('limit',200))))
            if after>9223372036854775807:
                raise ValueError()
        except ValueError:
            raise ValueError('Invalid operation log cursor.')
        result=operations.detail(oid,after,limit)
        if result is None:
            return error('Operation not found.',404)
        return jsonify(result)

    @app.get('/api/admin/operations')
    @admin('admin')
    def list_operations():
        try:
            limit=min(100,max(1,int(request.args.get('limit',50))))
            offset=max(0,int(request.args.get('offset',0)))
        except ValueError:
            raise ValueError('Invalid operations page.')
        status=request.args.get('status','')
        if status and status not in ('queued','running','paused','completed','failed','interrupted'):
            raise ValueError('Invalid operation status.')
        return jsonify(operations.list(limit,offset,status=status,query=request.args.get('q','')[:200],action=request.args.get('action','')[:80]))

    @app.get('/')
    def index():
        if not user():
            return render_template('login.html',csrf=csrf())
        return render_template('index.html',csrf=csrf())
    @app.get('/auth/config')
    def auth_config():
        with db() as c:
            first = c.execute('SELECT count(*) FROM users').fetchone()[0]==0
        return jsonify(configured=bool(app.config['OUTHMAN_APP_ID'] and app.config['OUTHMAN_APP_SECRET']),first_user=first)
    @app.post('/auth/<action>')
    def auth(action):
        if not secrets.compare_digest(request.headers.get('X-CSRF-Token',''),csrf()):
            return error('Invalid CSRF token.',403)
        if action=='logout':
            with db() as c:
                c.execute('DELETE FROM sessions WHERE token=?',(session.get('login',''),))
            session.clear()
            return jsonify(ok=True)
        if action not in ('login','enroll'):
            return error('Unknown action.',404)
        data = request.get_json() or {}
        username,password,code = [data.get(k,'') for k in ('username','password','code')]
        if not all(isinstance(x,str) for x in (username,password,code)) or not username or len(username)>200 or len(password)>1024 or not re.fullmatch('[0-9]{6}',code):
            return error('Enter username, password and a six-digit code.')
        if not re.fullmatch('[a-fA-F0-9]{16}',app.config['OUTHMAN_APP_ID']) or not re.fullmatch('[a-fA-F0-9]{32}',app.config['OUTHMAN_APP_SECRET']):
            return error('Configure a valid OuthMan app ID (16 hex characters) and full app secret (32 hex characters) on the server first.',503)
        subject = (request.remote_addr or '')+':'+username.lower()
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            attempt = c.execute('SELECT * FROM attempts WHERE subject=?',(subject,)).fetchone()
            if attempt and attempt['expires']>time.time() and attempt['count']>=10:
                remaining=max(1,math.ceil(attempt['expires']-time.time()))
                response=jsonify(error=f'Too many attempts. Try again in {math.ceil(remaining/60)} minute(s).',retry_after=remaining)
                response.status_code=429
                response.headers['Retry-After']=str(remaining)
                return response
            c.execute('INSERT INTO attempts VALUES(?,1,?) ON CONFLICT(subject) DO UPDATE SET count=CASE WHEN expires<? THEN 1 ELSE count+1 END,expires=CASE WHEN expires<? THEN excluded.expires ELSE expires END',(subject,time.time()+900,time.time(),time.time()))
        with lock:
            if action=='enroll':
                with db() as c:
                    first = c.execute('SELECT count(*) FROM users').fetchone()[0]==0
                    token = data.get('invitation','')
                    invitation = c.execute('SELECT * FROM invitations WHERE token=? AND used=0 AND expires>?',(hashlib.sha256(token.encode()).hexdigest(),time.time())).fetchone()
                    if first:
                        if not secrets.compare_digest(token,app.config['BOOTSTRAP_TOKEN']):
                            return error('Enter the server bootstrap token to create the first administrator.',403)
                        role = 'admin'
                    elif invitation:
                        role = invitation['role']
                    else:
                        return error('A valid team invitation is required.',403)
                    if c.execute('SELECT id FROM users WHERE username=?',(username,)).fetchone():
                        return error('Account already linked. Sign in.',409)
                    linked = remote('enroll',{'username':username,'password':password,'code':code},201)
                    if linked.get('app_id')!=app.config['OUTHMAN_APP_ID'] or type(linked.get('user_id')) is not int or not isinstance(linked.get('username'),str) or not re.fullmatch('[a-fA-F0-9]{32}',linked.get('user_key','')):
                        raise ValueError('Invalid enrollment response. Administrator recovery may be required.')
                    uid = c.execute('INSERT INTO users(username,password,role,provider,remote_id,user_key) VALUES(?,?,?,?,?,?)',(linked['username'],generate_password_hash(password),role,app.config['OUTHMAN_BASE_URL'],linked['user_id'],cipher.encrypt(linked['user_key'].encode()).decode())).lastrowid
                    if invitation:
                        c.execute('UPDATE invitations SET used=1 WHERE token=?',(invitation['token'],))
                new_session(uid)
            else:
                with db() as c:
                    account = c.execute('SELECT * FROM users WHERE username=?',(username,)).fetchone()
                if not account or account['role']=='revoked' or account['provider']!=app.config['OUTHMAN_BASE_URL'] or not check_password_hash(account['password'],password):
                    return error('Invalid credentials.',401)
                rid,browser = secrets.token_urlsafe(32),secrets.token_urlsafe(32)
                session['pending_browser']=browser
                with db() as c:
                    c.execute('DELETE FROM pending WHERE expires<?',(time.time(),))
                    c.execute('INSERT INTO pending VALUES(?,?,?,?,0)',(rid,account['id'],browser,time.time()+30))
                verified = remote('verify',{'username':account['username'],'password':password,'code':code,'user_key':cipher.decrypt(account['user_key'].encode()).decode(),'request_id':rid})
                if verified.get('request_id')!=rid or not re.fullmatch('[a-fA-F0-9]{32}',verified.get('auth_pin','')) or not isinstance(verified.get('expires_at'),(int,float)) or verified['expires_at']<=time.time():
                    raise ValueError('Invalid verification response. Retry with a fresh code.')
                confirmed = remote('confirm',{'username':account['username'],'auth_pin':verified['auth_pin'],'request_id':rid})
                if confirmed.get('verified') is not True or confirmed.get('app_id')!=app.config['OUTHMAN_APP_ID'] or type(confirmed.get('user_id')) is not int or confirmed['user_id']!=account['remote_id'] or confirmed.get('request_id')!=rid:
                    raise ValueError('Authentication identity mismatch.')
                with db() as c:
                    changed = c.execute('UPDATE pending SET completed=1 WHERE id=? AND user_id=? AND browser=? AND expires>? AND completed=0',(rid,account['id'],session.get('pending_browser'),time.time())).rowcount
                    if changed!=1:
                        raise ValueError('Login expired. Retry with a fresh code.')
                    new_session(account['id'],c)
        with db() as c:
            c.execute('DELETE FROM attempts WHERE subject=?',(subject,))
        return jsonify(ok=True)
    def positive(data,name,allow_zero=False):
        value=data.get(name,0)
        if type(value) is not int or value < (0 if allow_zero else 1) or value>10**16:
            raise ValueError('Invalid '+name)
        return value
    def valid_name(value):
        if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 _-]{0,79}',value):
            raise ValueError('Use a name of 1–80 letters, numbers, spaces, underscores or hyphens.')
        return value
    @app.get('/api/admin/state')
    @admin()
    def state():
        account = user()
        with db() as c:
            month=datetime.now(timezone.utc).strftime('%Y-%m')
            c.execute('UPDATE spaces SET bandwidth_used=0,month=? WHERE month<>?',(month,month))
            return jsonify(server_upload_limit_mib=app.config['MAX_UPLOAD_FILE_BYTES']//(1024*1024),user={k:account[k] for k in ('username','role')},csrf=csrf(),clouds=[dict(r) for r in c.execute('SELECT id,name,provider,capacity,used FROM clouds')],spaces=[dict(r) for r in c.execute('SELECT id,name,quota,used,encrypted,bandwidth_limit,bandwidth_used,cloud_id,max_upload_mib,compression_mode FROM spaces')],files=[dict(r) for r in c.execute('SELECT id,space_id,path,size,stored_size,compression,created FROM files ORDER BY id DESC')],users=[dict(r) for r in c.execute('SELECT id,username,role FROM users')] if account['role']=='admin' else [])
    @app.post('/api/admin/clouds/test')
    @admin('admin')
    def test_cloud_credentials():
        data=request.get_json() or {}
        kind=data.get('provider')
        if kind not in ('google','dropbox','mega','b2','local'):
            raise ValueError('Unsupported provider.')
        credentials=data.get('credentials',{})
        if not isinstance(credentials,dict) or (kind not in ('local','mega','b2') and not credentials.get('access_token')):
            raise ValueError('Cloud access token required. For Google client JSON, authorize with Google first.')
        validate_credentials(kind,credentials)
        return jsonify(Provider({'id':0,'provider':kind},credentials,Path(app.config['DATA_DIR'])/'objects').check())
    @app.post('/api/admin/clouds/<int:cid>/test')
    @admin('admin')
    def test_saved_cloud(cid):
        with db() as c:
            cloud=c.execute('SELECT * FROM clouds WHERE id=?',(cid,)).fetchone()
        if not cloud:
            return error('Cloud account not found.',404)
        return jsonify(adapter(cloud).check())
    def detected_capacity(kind,credentials,used=0,cid=0):
        probe=Provider({'id':cid,'provider':kind},credentials,Path(app.config['DATA_DIR'])/'objects').check()
        available=probe.get('available')
        if type(available) is not int or available<0:
            raise ValueError('This provider does not report a fixed available capacity. Set a CSO storage budget manually.')
        if available+used<=0:
            raise ValueError('No available storage was reported by this provider.')
        return available+used

    @app.post('/api/admin/clouds/<int:cid>/capacity')
    @admin('admin')
    def refresh_cloud_capacity(cid):
        with lock,db() as c:
            c.execute('BEGIN IMMEDIATE')
            cloud=c.execute('SELECT * FROM clouds WHERE id=?',(cid,)).fetchone()
            if not cloud:
                return error('Cloud account not found.',404)
            credentials=json.loads(cipher.decrypt(cloud['credentials'].encode()))
            capacity=detected_capacity(cloud['provider'],credentials,cloud['used'],cid)
            c.execute('UPDATE clouds SET capacity=? WHERE id=?',(capacity,cid))
        return jsonify(ok=True,capacity=capacity)

    @app.post('/api/admin/clouds')
    @admin('admin')
    def add_cloud():
        d=request.get_json() or {}
        kind=d.get('provider')
        if kind not in ('local','google','dropbox','mega','b2'):
            raise ValueError('Unsupported provider.')
        credentials=d.get('credentials',{})
        if not isinstance(credentials,dict) or (kind not in ('local','mega','b2') and not credentials.get('access_token')):
            raise ValueError('Cloud access token required.')
        validate_credentials(kind,credentials)
        capacity=detected_capacity(kind,credentials) if d.get('auto_capacity') is True else positive(d,'capacity')
        with db() as c:
            cid=c.execute('INSERT INTO clouds(name,provider,capacity,credentials) VALUES(?,?,?,?)',(valid_name(d.get('name')),kind,capacity,cipher.encrypt(json.dumps(credentials).encode()).decode())).lastrowid
        return jsonify(id=cid),201
    @app.patch('/api/admin/clouds/<int:cid>')
    @admin('admin')
    def update_cloud(cid):
        d=request.get_json() or {}
        with lock,db() as c:
            cloud=c.execute('SELECT * FROM clouds WHERE id=?',(cid,)).fetchone()
            if not cloud:
                return error('Cloud account not found.',404)
            capacity=positive(d,'capacity')
            reserved=c.execute('SELECT COALESCE(sum(stored_size),0) FROM upload_reservations WHERE cloud_id=?',(cid,)).fetchone()[0]
            if capacity<cloud['used']+reserved:
                raise ValueError('Capacity cannot be lower than existing stored bytes and active reservations.')
            credentials=d.get('credentials')
            if credentials is None:
                encrypted=cloud['credentials']
            else:
                if not isinstance(credentials,dict) or (cloud['provider'] not in ('local','mega','b2') and not credentials.get('access_token')):
                    raise ValueError('Cloud access token required.')
                validate_credentials(cloud['provider'],credentials)
                if cloud['provider']=='b2':
                    saved=json.loads(cipher.decrypt(cloud['credentials'].encode()))
                    if any(saved.get(k)!=credentials.get(k) for k in ('bucket_id','bucket_name')):
                        raise ValueError('Keep the same Backblaze bucket when replacing credentials. Add a new cloud account for another bucket.')
                encrypted=cipher.encrypt(json.dumps(credentials).encode()).decode()
            c.execute('UPDATE clouds SET capacity=?,credentials=? WHERE id=?',(capacity,encrypted,cid))
        return jsonify(ok=True)
    @app.delete('/api/admin/clouds/<int:cid>')
    @admin('admin')
    def delete_cloud(cid):
        with lock,db() as c:
            c.execute('BEGIN IMMEDIATE')
            cloud=c.execute('SELECT * FROM clouds WHERE id=?',(cid,)).fetchone()
            if not cloud:
                return error('Cloud account not found.',404)
            has_files=c.execute('SELECT 1 FROM files JOIN spaces ON spaces.id=files.space_id WHERE spaces.cloud_id=? LIMIT 1',(cid,)).fetchone()
            has_usage=c.execute('SELECT 1 FROM spaces WHERE cloud_id=? AND used<>0 LIMIT 1',(cid,)).fetchone()
            if cloud['used'] or has_files or has_usage or c.execute('SELECT 1 FROM upload_reservations WHERE cloud_id=? LIMIT 1',(cid,)).fetchone():
                return error('This cloud account still holds files. Delete its files before removing the account.',409)
            c.execute('UPDATE spaces SET cloud_id=NULL WHERE cloud_id=?',(cid,))
            c.execute('DELETE FROM clouds WHERE id=?',(cid,))
        return jsonify(ok=True)

    def space_compression(data,default='inherit'):
        mode=data.get('compression_mode',default)
        if mode not in (*MODES,'inherit'):
            raise ValueError('Invalid space compression mode.')
        return mode

    def space_upload_limit(data,default=None):
        value=data.get('max_upload_mib',default if default is not None else app.config['MAX_UPLOAD_FILE_BYTES']//(1024*1024))
        if type(value) is not int or not 1<=value<=app.config['MAX_UPLOAD_FILE_BYTES']//(1024*1024):
            raise ValueError('Maximum file size must be a whole number of MiB within the server upload limit.')
        return value

    @app.post('/api/admin/spaces')
    @admin('admin')
    def add_space():
        d=request.get_json() or {}
        key=secrets.token_hex(16)
        with db() as c:
            sid=c.execute('INSERT INTO spaces(name,quota,encrypted,bandwidth_limit,month,key_hash,max_upload_mib,compression_mode) VALUES(?,?,?,?,?,?,?,?)',(valid_name(d.get('name')),positive(d,'quota'),int(bool(d.get('encrypted',False))),positive(d,'bandwidth_limit',True),datetime.now(timezone.utc).strftime('%Y-%m'),hashlib.sha256(key.encode()).hexdigest(),space_upload_limit(d),space_compression(d))).lastrowid
        return jsonify(key=key,space={'id':sid,'name':d['name']}),201
    @app.delete('/api/admin/spaces/<int:sid>')
    @admin('admin')
    def delete_space(sid):
        data=request.get_json(silent=True) or {}
        oid=getattr(g,'operation_id',None)
        # Validate confirmation before enqueueing a destructive job.
        with db() as c:
            space=c.execute('SELECT * FROM spaces WHERE id=?',(sid,)).fetchone()
            if not space:
                return error('Storage space not found.',404)
            if c.execute('SELECT 1 FROM upload_reservations WHERE space_id=? LIMIT 1',(sid,)).fetchone():
                return error('This space has an active or paused upload. Finish that operation before deleting the space.',409)
            nonempty=space['used'] or c.execute('SELECT 1 FROM files WHERE space_id=? LIMIT 1',(sid,)).fetchone()
            if nonempty and (data.get('delete_files') is not True or data.get('confirm_name')!=space['name']):
                return error('Confirm the space name and select Delete all files to remove a non-empty storage space.',409)
        if data.get('background') is True and oid:
            operations.update(oid,status='queued',message='Waiting to delete the space and its files.')
            def worker():
                with app.app_context():
                    try:
                        result=app.make_response(perform_space_deletion(sid,data,oid))
                        payload=result.get_json(silent=True) or {}
                        operations.finish(oid,result.status_code<400,payload.get('error','Space and cataloged cloud files deleted.'))
                    except Exception:
                        app.logger.exception('Background space deletion failed')
                        operations.finish(oid,False,'Deletion failed. Check remaining files and retry; completed deletions are retained.')
            scheduler.submit(oid,worker)
            g.operation_background=True
            return jsonify(operation_id=oid,status='queued'),202
        return scheduler.run(oid,lambda:perform_space_deletion(sid,data,oid))

    def perform_space_deletion(sid,data,oid=None):
        deleted=0
        marked=False
        scheduler.checkpoint()
        try:
            with lock,db() as c:
                c.execute('BEGIN IMMEDIATE')
                space=c.execute('SELECT * FROM spaces WHERE id=?',(sid,)).fetchone()
                if not space:
                    return error('Storage space not found.',404)
                if c.execute('SELECT 1 FROM upload_reservations WHERE space_id=? LIMIT 1',(sid,)).fetchone():
                    return error('Finish the active or paused upload before deleting this space.',409)
                files=c.execute('SELECT id FROM files WHERE space_id=? ORDER BY id',(sid,)).fetchall()
                if files or space['used']:
                    if data.get('delete_files') is not True or data.get('confirm_name')!=space['name']:
                        return error('Confirm the space name and select Delete all files to remove a non-empty storage space.',409)
                cloud=c.execute('SELECT * FROM clouds WHERE id=?',(space['cloud_id'],)).fetchone()
                if files and not cloud:
                    return error('The storage provider is unavailable. The space was kept.',409)
                c.execute('UPDATE spaces SET deleting=1 WHERE id=?',(sid,))
                marked=True
            if oid:
                operations.update(oid,total=len(files)+1,completed=0,message='Deleting cloud files.')
            provider=adapter(cloud) if cloud else None
            for entry in files:
                scheduler.checkpoint()
                if oid:
                    operations.update(oid,completed=deleted,message=f'Deleting cloud file {deleted+1} of {len(files)}.')
                with lock,db() as c:
                    c.execute('BEGIN IMMEDIATE')
                    f=c.execute('SELECT * FROM files WHERE id=?',(entry['id'],)).fetchone()
                    if f:
                        try:
                            provider.delete(f['object_id'])
                        except (ProviderError,OSError):
                            return error(f'Bulk deletion stopped after {deleted} files. The space and remaining files were kept. Check provider access or retention settings, then retry.',502)
                        c.execute('DELETE FROM files WHERE id=?',(f['id'],))
                        c.execute('UPDATE spaces SET used=used-? WHERE id=?',(f['size'],sid))
                        c.execute('UPDATE clouds SET used=used-? WHERE id=?',(f['stored_size'],cloud['id']))
                deleted+=1
                if oid:
                    operations.update(oid,completed=deleted,message=f'Deleted {deleted} of {len(files)} cloud files.')
            scheduler.checkpoint()
            if oid:
                operations.update(oid,message='Removing storage space and revoking its API key.')
            with lock,db() as c:
                c.execute('BEGIN IMMEDIATE')
                remaining=c.execute('SELECT used FROM spaces WHERE id=?',(sid,)).fetchone()
                if not remaining:
                    return error('Storage space not found.',404)
                if remaining['used'] or c.execute('SELECT 1 FROM files WHERE space_id=?',(sid,)).fetchone():
                    return error('Files or recorded usage remain. The space was kept for administrator recovery.',409)
                c.execute('DELETE FROM spaces WHERE id=?',(sid,))
                marked=False
            return jsonify(ok=True,deleted_files=deleted)
        finally:
            if marked:
                with lock,db() as c:
                    c.execute('UPDATE spaces SET deleting=0 WHERE id=?',(sid,))

    @app.patch('/api/admin/spaces/<int:sid>')
    @admin('admin')
    def update_space(sid):
        d=request.get_json() or {}
        quota=positive(d,'quota')
        bandwidth=positive(d,'bandwidth_limit',True)
        with lock,db() as c:
            space=c.execute('SELECT * FROM spaces WHERE id=?',(sid,)).fetchone()
            if not space:
                return error('Storage space not found.',404)
            pending=c.execute('SELECT COALESCE(sum(size),0) FROM upload_reservations WHERE space_id=?',(sid,)).fetchone()[0]
            if quota<space['used']+pending:
                raise ValueError('Quota cannot be lower than existing files.')
            c.execute('UPDATE spaces SET quota=?,bandwidth_limit=?,max_upload_mib=?,compression_mode=? WHERE id=?',(quota,bandwidth,space_upload_limit(d,min(space['max_upload_mib'],app.config['MAX_UPLOAD_FILE_BYTES']//(1024*1024))),space_compression(d,space['compression_mode']),sid))
        return jsonify(ok=True)
    @app.post('/api/admin/spaces/<int:sid>/rotate')
    @admin('admin')
    def rotate(sid):
        key=secrets.token_hex(16)
        with db() as c:
            if not c.execute('UPDATE spaces SET key_hash=? WHERE id=?',(hashlib.sha256(key.encode()).hexdigest(),sid)).rowcount:
                return error('Storage space not found.',404)
        return jsonify(key=key)
    @app.post('/api/admin/invitations')
    @admin('admin')
    def invite():
        role=(request.get_json() or {}).get('role')
        if role not in ('admin','manager'):
            raise ValueError('Invalid role.')
        token=secrets.token_urlsafe(32)
        with db() as c:
            c.execute('INSERT INTO invitations VALUES(?,?,?,0)',(hashlib.sha256(token.encode()).hexdigest(),role,time.time()+86400))
        return jsonify(token=token),201
    @app.route('/api/admin/users/<int:uid>',methods=['PATCH','DELETE'])
    @admin('admin')
    def manage_user(uid):
        if uid==user()['id']:
            return error('You cannot change your own access.',400)
        with lock,db() as c:
            target=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
            if not target:
                return error('User not found.',404)
            if target['role']=='admin' and c.execute("SELECT count(*) FROM users WHERE role='admin'").fetchone()[0]<=1:
                return error('Keep at least one administrator.',400)
            c.execute('DELETE FROM sessions WHERE user_id=?',(uid,))
            if request.method=='DELETE':
                # Retain the remote identity: OuthMan cannot re-enroll a deleted binding.
                c.execute("UPDATE users SET role='revoked' WHERE id=?",(uid,))
            else:
                role=(request.get_json() or {}).get('role')
                if role not in ('admin','manager'):
                    raise ValueError('Invalid role.')
                c.execute('UPDATE users SET role=? WHERE id=?',(role,uid))
        return jsonify(ok=True)
    def adapter(cloud):
        return Provider(cloud,json.loads(cipher.decrypt(cloud['credentials'].encode())),Path(app.config['DATA_DIR'])/'objects')
    def space_for_key(c):
        header=request.headers.get('Authorization','')
        key=header[7:] if header.startswith('Bearer ') else request.headers.get('X-API-Key','')
        return c.execute('SELECT * FROM spaces WHERE key_hash=?',(hashlib.sha256(key.encode()).hexdigest(),)).fetchone() if key else None
    def reset_bandwidth(c,space):
        month=datetime.now(timezone.utc).strftime('%Y-%m')
        if space['month']!=month:
            c.execute('UPDATE spaces SET bandwidth_used=0,month=? WHERE id=?',(month,space['id']))
            space=c.execute('SELECT * FROM spaces WHERE id=?',(space['id'],)).fetchone()
        return space
    def check_bandwidth(space,size):
        if space['bandwidth_limit'] and space['bandwidth_used']+size>space['bandwidth_limit']:
            raise ValueError('Monthly transfer allowance exceeded.')
    def valid_file_path(path):
        if not isinstance(path,str) or len(path)>1024 or path.startswith('/') or '\\' in path or any(p in ('','.','..') for p in path.split('/')) or any(ord(ch)<32 for ch in path):
            raise ValueError('Invalid relative file path.')
        return unicodedata.normalize('NFC',path)

    @app.post('/api/admin/uploads/start')
    @admin()
    def prepare_upload():
        data=request.get_json() or {}
        sid=data.get('space_id')
        size=data.get('size')
        if type(sid) is not int or type(size) is not int or size<0:
            raise ValueError('Invalid upload size or storage space.')
        path=valid_file_path(data.get('path'))
        with db() as c:
            space=c.execute('SELECT * FROM spaces WHERE id=?',(sid,)).fetchone()
            if not space:
                return error('Storage space not found.',404)
            if space['deleting']:
                return error('This storage space is being deleted.',409)
            if size>min(space['max_upload_mib']*1024*1024,app.config['MAX_UPLOAD_FILE_BYTES']):
                return error('File exceeds this storage space upload limit. Edit the space limits before retrying.',413)
            if space['used']+size>space['quota']:
                return error('Storage space quota exceeded.',413)
            check_bandwidth(reset_bandwidth(c,space),size)
            oid=operations.start('admin_upload',f"Space #{sid}: {space['name']} / {path}",user()['username'])
            token=secrets.token_urlsafe(32)
            c.execute('INSERT INTO upload_tickets VALUES(?,?,?,?,?,?,?,?)',(
                hashlib.sha256(token.encode()).hexdigest(),oid,user()['id'],hashlib.sha256(session['login'].encode()).hexdigest(),sid,path,size,time.time()+3600))
        operations.update(oid,status='running',total=10000,completed=500,message='Receiving file from browser. Keep the browser tab open until received.')
        return jsonify(upload_token=token,operation_id=oid)

    @app.post('/api/admin/uploads/failed')
    @admin()
    def fail_browser_upload():
        token=(request.get_json() or {}).get('upload_token','')
        if not isinstance(token,str):
            raise ValueError('Invalid upload token.')
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            ticket=c.execute('SELECT * FROM upload_tickets WHERE token=?',(hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
            if not ticket or ticket['user_id']!=user()['id'] or ticket['session_hash']!=hashlib.sha256(session['login'].encode()).hexdigest():
                return error('Upload is already received or unavailable. Check Running Operations.',409)
            c.execute('DELETE FROM upload_tickets WHERE token=?',(ticket['token'],))
        operations.finish(ticket['operation_id'],False,'Browser transfer did not complete. Start a new upload to retry.')
        return jsonify(ok=True)

    def upload(sid=None):
        uploaded=request.files.get('file')
        if not uploaded:
            raise ValueError('Multipart field "file" is required.')
        path=valid_file_path(request.form.get('path') or uploaded.filename)
        with db() as c:
            space=c.execute('SELECT * FROM spaces WHERE id=?',(sid,)).fetchone() if sid else space_for_key(c)
            if not space:
                return error('Storage space or API key not found.',404 if sid else 401)
            sid=space['id']
            limit=min(space['max_upload_mib']*1024*1024,app.config['MAX_UPLOAD_FILE_BYTES'])
        ticket=getattr(g,'upload_ticket',None)
        if ticket and (ticket['space_id']!=sid or ticket['path']!=path):
            return error('Upload does not match the selected file and storage space.',409)
        if request.form.get('background')=='true' and request.path.startswith('/api/admin/'):
            oid=getattr(g,'operation_id',None)
            staged=None
            try:
                with tempfile.NamedTemporaryFile(dir=upload_dir,suffix='.upload',delete=False) as target:
                    staged=Path(target.name)
                    size=0
                    while True:
                        chunk=uploaded.stream.read(1024*1024)
                        if not chunk:
                            break
                        size+=len(chunk)
                        if size>limit:
                            return error('File exceeds this storage space upload limit.',413)
                        target.write(chunk)
                if ticket and size!=ticket['size']:
                    return error('Received file size does not match the selected file.',409)
                operations.update(oid,status='queued',total=10000,completed=2000,message='File received. Waiting for cloud transfer.')
                def worker(staged_path):
                    with app.app_context():
                        try:
                            operations.update(oid,status='running',message='Preparing cloud upload.')
                            response=app.make_response(perform_upload(sid,path,staged_path,oid))
                            result=response.get_json(silent=True) or {}
                            operations.finish(oid,response.status_code<400,result.get('error','Upload completed.'))
                        except (ProviderError,ValueError) as exc:
                            operations.finish(oid,False,str(exc))
                        except Exception:
                            app.logger.exception('Background cloud upload failed')
                            operations.finish(oid,False,'Cloud upload failed. Check provider connection and available capacity; retry the file upload.')
                        finally:
                            staged_path.unlink(missing_ok=True)
                staged_path=staged
                scheduler.submit(oid,lambda:worker(staged_path))
                staged=None
                g.operation_background=True
                return jsonify(operation_id=oid,status='queued'),202
            finally:
                if staged is not None:
                    staged.unlink(missing_ok=True)
        staged=None
        try:
            with tempfile.NamedTemporaryFile(dir=upload_dir,suffix='.upload',delete=False) as target:
                staged=Path(target.name)
                size=0
                while True:
                    chunk=uploaded.stream.read(1024*1024)
                    if not chunk:
                        break
                    size+=len(chunk)
                    if size>limit:
                        return error('File exceeds this storage space upload limit.',413)
                    target.write(chunk)
            if ticket and size!=ticket['size']:
                return error('Received file size does not match the selected file.',409)
            return scheduler.run(getattr(g,'operation_id',None),lambda:perform_upload(sid,path,staged,getattr(g,'operation_id',None)))
        finally:
            if staged is not None:
                staged.unlink(missing_ok=True)

    def perform_upload(sid,path,content,operation_id=None):
        step(operation_id,2000,'Preparing received file.')
        with db() as c:
            space=c.execute('SELECT * FROM spaces WHERE id=?',(sid,)).fetchone()
        if not space:
            return error('Storage space not found.',404)
        if space['deleting']:
            return error('This storage space is being deleted.',409)
        if isinstance(content,Path):
            content=content.read_bytes()
        size=len(content)
        if size>min(space['max_upload_mib']*1024*1024,app.config['MAX_UPLOAD_FILE_BYTES']):
            return error('File exceeds this storage space upload limit.',413)
        mode=space['compression_mode']
        if mode=='inherit':
            mode=compression_mode()
        def compression_step(message):
            scheduler.checkpoint()
            if message and operation_id:
                operations.update(operation_id,message=message)
        compressed,compression=compress_content(content,mode,compression_step)
        step(operation_id,3000,'Compression stage complete. Encrypting file.' if space['encrypted'] else 'Compression stage complete. Preparing cloud object.')
        stored=encrypt_content(cipher,compressed) if space['encrypted'] else compressed
        step(operation_id,4000,'Encryption stage complete. Reserving cloud storage.' if space['encrypted'] else 'File prepared. Reserving cloud storage.')
        reservation_id=operation_id if operation_id is not None else -secrets.randbits(62)
        reserved=False
        cloud=None
        new_pin=False
        try:
            with lock,db() as c:
                c.execute('BEGIN IMMEDIATE')
                space=c.execute('SELECT * FROM spaces WHERE id=?',(sid,)).fetchone()
                if not space or space['deleting']:
                    return error('Storage space was removed or is being deleted.',409)
                if size>min(space['max_upload_mib']*1024*1024,app.config['MAX_UPLOAD_FILE_BYTES']):
                    return error('File exceeds this storage space upload limit.',413)
                space=reset_bandwidth(c,space)
                paths=[r['path'] for r in c.execute('SELECT path FROM files WHERE space_id=? UNION ALL SELECT path FROM upload_reservations WHERE space_id=?',(sid,sid))]
                if any(unicodedata.normalize('NFC',value).casefold()==path.casefold() for value in paths):
                    return error('A file already exists or is uploading at this path.',409)
                pending=c.execute('SELECT COALESCE(sum(size),0) FROM upload_reservations WHERE space_id=?',(sid,)).fetchone()[0]
                if space['used']+pending+size>space['quota']:
                    return error('Storage space quota exceeded, including queued transfers.',413)
                check_bandwidth(space,pending+size)
                if space['cloud_id']:
                    cloud=c.execute('SELECT * FROM clouds WHERE id=?',(space['cloud_id'],)).fetchone()
                    held=c.execute('SELECT COALESCE(sum(stored_size),0) FROM upload_reservations WHERE cloud_id=?',(cloud['id'],)).fetchone()[0]
                    if cloud['capacity']-cloud['used']-held<len(stored):
                        return error('The pinned cloud has insufficient capacity, including active transfers.',507)
                else:
                    cloud=c.execute('''SELECT clouds.* FROM clouds WHERE capacity-used-
                        COALESCE((SELECT sum(stored_size) FROM upload_reservations WHERE cloud_id=clouds.id),0)>=?
                        ORDER BY capacity-used-COALESCE((SELECT sum(stored_size) FROM upload_reservations WHERE cloud_id=clouds.id),0) DESC,id LIMIT 1''',(len(stored),)).fetchone()
                    if not cloud:
                        return error('No cloud account has sufficient unreserved capacity.',507)
                    c.execute('UPDATE spaces SET cloud_id=? WHERE id=?',(cloud['id'],sid))
                    new_pin=True
                c.execute('INSERT INTO upload_reservations VALUES(?,?,?,?,?,?)',(reservation_id,sid,path,cloud['id'],size,len(stored)))
                reserved=True
            provider_path=('CSO/objects/'+secrets.token_hex(32)) if space['encrypted'] else ('CSO/'+space['name']+'/'+path)
            provider=adapter(cloud)
            if operation_id:
                def progress(done,total,message):
                    step(operation_id,4000+int(5000*done/max(1,total)),message)
                provider.progress=progress
            step(operation_id,4000,'Transferring file to cloud storage.')
            object_id=provider.put(provider_path,stored)
            # Final catalog commit must not pause after a successful cloud write.
            if operation_id:
                operations.update(operation_id,completed=9500,message='Saving file catalog and storage counters.')
            created=datetime.now(timezone.utc).isoformat()
            with lock,db() as c:
                c.execute('BEGIN IMMEDIATE')
                fid=c.execute('INSERT INTO files(space_id,path,size,stored_size,object_id,created,compression) VALUES(?,?,?,?,?,?,?)',(sid,path,size,len(stored),object_id,created,compression)).lastrowid
                c.execute('UPDATE spaces SET used=used+?,bandwidth_used=bandwidth_used+? WHERE id=?',(size,size,sid))
                c.execute('UPDATE clouds SET used=used+? WHERE id=?',(len(stored),cloud['id']))
                c.execute('DELETE FROM upload_reservations WHERE operation_id=?',(reservation_id,))
                reserved=False
            return jsonify(id=fid,path=path,size=size,stored_size=len(stored),compression=compression,saved_bytes=max(0,size-len(stored)),created=created),201
        finally:
            if reserved:
                with lock,db() as c:
                    c.execute('DELETE FROM upload_reservations WHERE operation_id=?',(reservation_id,))
                    if new_pin and not c.execute('SELECT 1 FROM upload_reservations WHERE space_id=?',(sid,)).fetchone() and not c.execute('SELECT 1 FROM files WHERE space_id=?',(sid,)).fetchone():
                        c.execute('UPDATE spaces SET cloud_id=NULL WHERE id=? AND used=0',(sid,))

    def file_operation(fid,admin_access=False,delete=False):
        return scheduler.run(getattr(g,'operation_id',None),lambda:perform_file_operation(fid,admin_access,delete))

    def perform_file_operation(fid,admin_access=False,delete=False):
        with lock,db() as c:
            c.execute('BEGIN IMMEDIATE')
            f=c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone()
            space=c.execute('SELECT * FROM spaces WHERE id=?',(f['space_id'],)).fetchone() if f else None
            if not admin_access:
                authorized=space_for_key(c)
                if not authorized:
                    return error('Invalid API key.',401)
                if not space or authorized['id']!=space['id']:
                    return error('File not found.',404)
            if not f:
                return error('File not found.',404)
            cloud=c.execute('SELECT * FROM clouds WHERE id=?',(space['cloud_id'],)).fetchone()
            provider=adapter(cloud)
            if getattr(g,'operation_id',None):
                operations.update(g.operation_id,total=1,completed=0,resource=f"Space #{space['id']}: {space['name']} / {f['path']}",message='Deleting cloud file.' if delete else 'Downloading file.')
            if delete:
                provider.delete(f['object_id'])
                c.execute('DELETE FROM files WHERE id=?',(fid,))
                c.execute('UPDATE spaces SET used=used-? WHERE id=?',(f['size'],space['id']))
                c.execute('UPDATE clouds SET used=used-? WHERE id=?',(f['stored_size'],cloud['id']))
                return jsonify(ok=True)
            space=reset_bandwidth(c,space)
            check_bandwidth(space,f['size'])
            content=provider.get(f['object_id'])
            if space['encrypted']:
                content=decrypt_content(cipher,content)
            if f['compression']!='none':
                try:
                    content=decompress_content(content,f['compression'],f['size'])
                except (zipfile.BadZipFile,RuntimeError,ValueError,EOFError,OSError,lzma.LZMAError):
                    raise ProviderError('Stored compressed file is invalid. Download was stopped.') from None
            c.execute('UPDATE spaces SET bandwidth_used=bandwidth_used+? WHERE id=?',(f['size'],space['id']))
        return send_file(io.BytesIO(content),as_attachment=True,download_name=f['path'].split('/')[-1],mimetype='application/octet-stream')
    @app.post('/api/admin/files')
    @admin()
    def admin_upload():
        try:
            sid=int(request.form.get('space_id','0'))
        except ValueError:
            raise ValueError('Select a storage space.')
        if sid<=0:
            raise ValueError('Select a storage space.')
        return upload(sid)
    @app.get('/api/admin/files/<int:fid>/download')
    @admin()
    def admin_download(fid):
        return file_operation(fid,True)
    @app.delete('/api/admin/files/<int:fid>')
    @admin()
    def admin_delete(fid):
        return file_operation(fid,True,True)
    @app.route('/api/v1/files',methods=['GET','POST'])
    def api_files():
        if request.method=='POST':
            return upload()
        with db() as c:
            space=space_for_key(c)
            if not space:
                return error('Invalid API key.',401)
            return jsonify(files=[dict(r) for r in c.execute('SELECT id,path,size,created FROM files WHERE space_id=?',(space['id'],))])
    @app.route('/api/v1/files/<int:fid>',methods=['GET','DELETE'])
    def api_file(fid):
        return file_operation(fid,delete=request.method=='DELETE')
    @app.get('/api/v1/storage')
    def api_storage():
        with db() as c:
            space=space_for_key(c)
            if not space:
                return error('Invalid API key.',401)
            space=reset_bandwidth(c,space)
            return jsonify(**{k:space[k] for k in ('name','quota','used','encrypted','bandwidth_limit','bandwidth_used')})
    from config_backup import register_config_backup
    register_config_backup(app,admin=admin,db=db,lock=lock,encryption_key=encryption_key,scheduler=scheduler,operations=operations,perform_upload=perform_upload,file_operation=file_operation)
    register_google_routes(app,db,cipher,user,admin,positive,valid_name,lock)
    return app

if __name__=='__main__':
    app=create_app()
    host=os.environ.get('CSO_HOST','127.0.0.1')
    port=int(os.environ.get('PORT','10081'))
    print(f'CSO: http://{host}:{port} — first enrollment token is in data/bootstrap.key')
    from waitress import serve
    serve(app,host=host,port=port,threads=8)
