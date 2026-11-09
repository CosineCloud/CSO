"""Cloud adapters. Provider object IDs are stored separately from logical paths."""
import json
import shutil
import os
import subprocess
import tempfile
from urllib.parse import urlparse
from pathlib import Path
import requests

class ProviderError(Exception):
    pass

def validate_credentials(kind, credentials):
    if not isinstance(credentials,dict):
        raise ValueError('Invalid cloud credentials.')
    if kind=='local':
        return
    if kind=='b2':
        if not all(isinstance(credentials.get(k),str) and credentials[k].strip() for k in ('key_id','application_key','bucket_id','bucket_name')):
            raise ValueError('Backblaze key ID, application key, bucket ID and bucket name are required.')
        return
    if kind=='mega':
        if not all(isinstance(credentials.get(k),str) and credentials[k].strip() for k in ('email','password')):
            raise ValueError('MEGA email and password are required.')
        if any(c in credentials['password'] for c in ('\n', '\r', '\x00')):
            raise ValueError('MEGA password cannot contain line breaks or NUL.')
        return
    token=credentials.get('access_token')
    if not isinstance(token,str) or not token.strip():
        raise ValueError('Cloud access token required. For Google JSON, use Google sign-in or Reauthorize.')
    if kind=='google' and (token.endswith('.apps.googleusercontent.com') or token.startswith('GOCSPX-') or token in [credentials.get('client_id'),credentials.get('client_secret')]):
        raise ValueError('An OAuth client ID or client secret was entered as the Google access token. Use Google sign-in (JSON) or Reauthorize, select gd_api.txt, and complete Google consent.')

class Provider:
    def __init__(self, cloud, credentials, root):
        self.cloud, self.credentials, self.root = cloud, credentials, Path(root)
        self.progress = None

    def b2(self):
        from b2_provider import B2Provider
        validate_credentials('b2', self.credentials)
        return B2Provider(self.credentials,progress=self.progress)

    def mega_command(self, *args, content=None):
        """No shell or credentials in argv; never expose provider stderr to clients."""
        validate_credentials('mega', self.credentials)
        env = {k:v for k,v in os.environ.items() if not k.startswith('RCLONE_')}
        try:
            obscured = subprocess.run(['rclone', 'obscure', '-'], input=self.credentials['password'].encode(),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, check=True, env=env).stdout.decode().strip()
            env.update(RCLONE_CONFIG_CSO_TYPE='mega', RCLONE_CONFIG_CSO_USER=self.credentials['email'],
                       RCLONE_CONFIG_CSO_PASS=obscured, RCLONE_CONFIG_CSO_USE_HTTPS='true')
            return subprocess.run(['rclone', '--config', os.devnull, '--retries', '1', '--low-level-retries', '1',
                                   *args], input=content, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  timeout=300, check=True, env=env).stdout
        except FileNotFoundError:
            raise ProviderError('Install rclone on the CSO server and add it to PATH to use MEGA.') from None
        except subprocess.TimeoutExpired:
            raise ProviderError('MEGA request timed out. Wait before retrying; repeated logins can trigger MEGA throttling.') from None
        except subprocess.CalledProcessError as exc:
            # Classify locally; provider output may contain credentials and must never be returned.
            detail = (exc.stderr or b'')
            if isinstance(detail, bytes):
                detail = detail.decode('utf-8', errors='replace')
            detail = detail.lower()
            message = 'MEGA operation failed. Verify your login at mega.nz first, then check credentials and account status. Accounts requiring 2FA are not supported by this adapter.'
            if any(term in detail for term in ('lookup ', 'no such host', 'network is unreachable', 'connection refused', 'tls handshake', 'certificate', 'proxyconnect')):
                message = 'CSO cannot reach MEGA. Check the server internet connection, DNS, proxy and TLS certificates.'
            elif any(term in detail for term in ('too many', 'rate limit', 'temporarily blocked', 'try again', 'eagain')):
                message = 'MEGA is temporarily limiting requests. Wait before retrying; avoid repeated Test drive clicks.'
            elif any(term in detail for term in ('two-factor', 'two factor', '2fa', 'multi-factor', 'mfa')):
                message = 'This MEGA account requires two-factor authentication, which this adapter does not support.'
            elif any(term in detail for term in ('quota', 'overquota', 'storage full', 'not enough space')):
                message = 'MEGA storage or transfer quota is exhausted. Check your MEGA dashboard before retrying.'
            elif any(term in detail for term in ("couldn't login", 'could not login', 'login failed', 'invalid password', 'invalid credentials', 'object not found', 'enoent')):
                message = 'MEGA rejected the login. Verify the email and password at mega.nz and complete any account confirmation. This adapter cannot sign in to accounts requiring 2FA.'
            raise ProviderError(message) from None
        except (subprocess.SubprocessError, OSError, UnicodeError):
            raise ProviderError('Could not run the MEGA adapter. Check the rclone installation and server permissions.') from None

    def mega_path(self, path):
        if not isinstance(path, str) or not path or path.startswith('/') or any(p in ('', '.', '..') for p in path.split('/')) or '\x00' in path:
            raise ProviderError('Invalid MEGA object path.')
        return 'cso:' + path

    def request(self, method, url, **kwargs):
        validate_credentials(self.cloud['provider'],self.credentials)
        accepted = kwargs.pop('accepted', ())
        headers = kwargs.pop('headers', {})
        headers['Authorization'] = 'Bearer ' + self.credentials.get('access_token', '')
        try:
            r = requests.request(method, url, headers=headers, timeout=60, allow_redirects=False, **kwargs)
            if r.status_code == 401 and self.cloud['provider'] == 'google' and self.credentials.get('refresh_token'):
                t = requests.post('https://oauth2.googleapis.com/token', data={k:self.credentials[k] for k in ('refresh_token','client_id','client_secret')} | {'grant_type':'refresh_token'}, timeout=15, allow_redirects=False)
                if t.status_code!=200:
                    raise ProviderError('Google token refresh failed. Reauthorize the account or replace its refresh credentials.')
                self.credentials['access_token'] = t.json()['access_token']
                headers['Authorization'] = 'Bearer ' + self.credentials['access_token']
                r = requests.request(method, url, headers=headers, timeout=60, allow_redirects=False, **kwargs)
            if r.status_code not in accepted:
                if r.status_code==401:
                    raise ProviderError('Cloud credentials are invalid or expired. Authorize the account or replace its access token.')
                if r.status_code==403 and self.cloud['provider']=='google':
                    raise ProviderError('Google Drive access denied. Enable the Drive API in the Google project and grant CSO file access, then reconnect.')
                if r.status_code==429:
                    raise ProviderError('The cloud provider is rate limiting requests. Wait and try again.')
                r.raise_for_status()
            if 300 <= r.status_code < 400:
                raise ProviderError('Unexpected cloud redirect')
            return r
        except (requests.RequestException, ValueError, KeyError) as e:
            raise ProviderError('Cloud request failed; check the account credentials and available capacity.') from e

    def check(self):
        """Read-only access/quota check; does not claim file-write permission."""
        kind = self.cloud['provider']
        validate_credentials(kind,self.credentials)
        try:
            if kind == 'local':
                root = self.root.resolve()
                while not root.exists():
                    root = root.parent
                disk = shutil.disk_usage(root)
                return dict(ok=True, message='Local disk is reachable. No test file was written.',
                            capacity=disk.total, usage=disk.used, available=disk.free)
            if kind == 'b2':
                return self.b2().check()
            if kind == 'mega':
                quota = json.loads(self.mega_command('about', 'cso:', '--json'))
                total, used = int(quota['total']), int(quota['used'])
                return dict(ok=True, message='MEGA account access verified. No test file was written.',
                            account=self.credentials['email'], capacity=total, usage=used, available=max(0,total-used))
            if kind == 'google':
                data = self.request('GET','https://www.googleapis.com/drive/v3/about',
                    params={'fields':'user(displayName,emailAddress,permissionId),storageQuota(limit,usage)'}).json()
                account = data.get('user', {})
                quota = data.get('storageQuota', {})
                result = dict(ok=True, message='Google Drive account access verified. No test file was written.',
                              account=account.get('emailAddress') or account.get('displayName') or 'Google Drive')
                if account.get('permissionId'):
                    result['account_id'] = account['permissionId']
                if 'usage' in quota:
                    result['usage'] = int(quota['usage'])
                if 'limit' in quota:
                    result['capacity'] = int(quota['limit'])
                    result['available'] = max(0,result['capacity']-int(quota.get('usage',0)))
                return result
            if kind == 'dropbox':
                account = self.request('POST','https://api.dropboxapi.com/2/users/get_current_account',
                                       headers={'Content-Type':'application/json'},data='null').json()
                quota = self.request('POST','https://api.dropboxapi.com/2/users/get_space_usage',
                                     headers={'Content-Type':'application/json'},data='null').json()
                used, total = int(quota['used']), int(quota['allocation']['allocated'])
                return dict(ok=True, message='Dropbox account access verified. No test file was written.',
                            account=account.get('email','Dropbox'),capacity=total,usage=used,available=max(0,total-used))
            raise ProviderError('Unsupported provider.')
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ProviderError('The cloud returned an invalid account or quota response.') from exc

    def put(self, path, content):
        kind = self.cloud['provider']
        if kind == 'local':
            target = self.root / str(self.cloud['id']) / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            return path
        if kind == 'b2':
            return self.b2().put(path, content)
        if kind == 'mega':
            remote = self.mega_path(path)
            with tempfile.TemporaryDirectory(prefix='cso-mega-') as tmp:
                source = Path(tmp) / 'upload'
                source.write_bytes(content)
                self.mega_command('copyto', str(source), remote)
            return path
        if kind == 'dropbox':
            parts = path.split('/')[:-1]
            for i in range(1, len(parts)+1):
                self.request('POST','https://api.dropboxapi.com/2/files/create_folder_v2',json={'path':'/'+'/'.join(parts[:i]),'autorename':False},accepted=(409,))
            self.request('POST', 'https://content.dropboxapi.com/2/files/upload', headers={'Content-Type':'application/octet-stream','Dropbox-API-Arg':json.dumps({'path':'/'+path,'mode':'overwrite','autorename':False})}, data=content)
            return '/' + path
        parent = None
        for folder in path.split('/')[:-1]:
            q = "trashed = false and mimeType = 'application/vnd.google-apps.folder' and name = '" + folder.replace("'", "\\'") + "' and '" + (parent or 'root') + "' in parents"
            found = self.request('GET','https://www.googleapis.com/drive/v3/files', params={'q':q,'fields':'files(id)'}).json()['files']
            if found:
                parent = found[0]['id']
            else:
                meta = {'name':folder,'mimeType':'application/vnd.google-apps.folder','parents':[parent or 'root']}
                parent = self.request('POST','https://www.googleapis.com/drive/v3/files',json=meta).json()['id']
        metadata = {'name':path.split('/')[-1], 'parents':[parent or 'root']}
        # Resumable upload handles files larger than Google's multipart recommendation.
        initiated = self.request('POST','https://www.googleapis.com/upload/drive/v3/files',params={'uploadType':'resumable','fields':'id'},headers={'X-Upload-Content-Type':'application/octet-stream','X-Upload-Content-Length':str(len(content))},json=metadata)
        location = initiated.headers.get('Location','')
        parsed = urlparse(location)
        if parsed.scheme != 'https' or parsed.hostname not in ('www.googleapis.com','upload.googleapis.com') or parsed.username or parsed.port not in (None,443):
            raise ProviderError('Invalid cloud upload location.')
        return self.request('PUT',location,headers={'Content-Type':'application/octet-stream'},data=content).json()['id']

    def get(self, object_id):
        if self.cloud['provider'] == 'b2':
            return self.b2().get(object_id)
        if self.cloud['provider'] == 'mega':
            return self.mega_command('cat', self.mega_path(object_id))
        if self.cloud['provider'] == 'local':
            return (self.root / str(self.cloud['id']) / object_id).read_bytes()
        if self.cloud['provider'] == 'dropbox':
            return self.request('POST','https://content.dropboxapi.com/2/files/download',headers={'Dropbox-API-Arg':json.dumps({'path':object_id})}).content
        return self.request('GET','https://www.googleapis.com/drive/v3/files/'+object_id,params={'alt':'media'}).content

    def delete(self, object_id):
        if self.cloud['provider'] == 'b2':
            return self.b2().delete(object_id)
        if self.cloud['provider'] == 'mega':
            self.mega_command('--mega-hard-delete', 'deletefile', self.mega_path(object_id))
        elif self.cloud['provider'] == 'local':
            (self.root / str(self.cloud['id']) / object_id).unlink(missing_ok=True)
        elif self.cloud['provider'] == 'dropbox':
            self.request('POST','https://api.dropboxapi.com/2/files/delete_v2',json={'path':object_id})
        else:
            self.request('DELETE','https://www.googleapis.com/drive/v3/files/'+object_id,accepted=(404,))
