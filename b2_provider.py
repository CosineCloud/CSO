"""Backblaze B2 native API; version IDs preserve exact file identity."""
import hashlib
import json
import time
from urllib.parse import quote, urlparse
import requests
from providers import ProviderError


class B2TransientError(ProviderError):
    pass


class B2Provider:
    LARGE_FILE_THRESHOLD = 64*1024*1024
    PART_SIZE = 10*1024*1024
    def __init__(self, credentials, progress=None):
        self.credentials = credentials
        self.auth = None
        self.progress = progress

    def request(self, method, url, **kwargs):
        parsed = urlparse(url)
        if (parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None, 443)
                or not parsed.hostname or not any(parsed.hostname.endswith('.' + domain)
                    for domain in ('backblazeb2.com', 'backblaze.com'))):
            raise ProviderError('Invalid Backblaze API destination.')
        accepted = kwargs.pop('accepted', ())
        timeout = kwargs.pop('timeout', (15,60))
        try:
            response = requests.request(method, url, timeout=timeout, allow_redirects=False, **kwargs)
            if response.status_code in accepted:
                return response
            if response.status_code in (401, 403):
                raise ProviderError('Backblaze rejected access. Check the application key, bucket restriction and key capabilities.')
            if response.status_code == 429:
                raise B2TransientError('Backblaze is limiting requests. Wait before retrying.')
            if response.status_code>=500:
                raise B2TransientError('Backblaze is temporarily unavailable. Retry after the provider recovers.')
            if not 200 <= response.status_code < 300:
                raise ProviderError('Backblaze request failed. Check bucket status, file retention and account limits.')
            return response
        except requests.Timeout:
            raise B2TransientError('Backblaze transfer timed out. The server could not complete the transfer within its timeout; retry the upload.') from None
        except requests.ConnectionError:
            raise B2TransientError('Backblaze connection was interrupted. Check server internet access and retry the upload.') from None
        except requests.RequestException:
            raise ProviderError('Cannot reach Backblaze. Check the server connection and try again.') from None

    def authorize(self):
        if self.auth is None:
            try:
                data = self.request('GET', 'https://api.backblazeb2.com/b2api/v4/b2_authorize_account',
                    auth=(self.credentials['key_id'], self.credentials['application_key'])).json()
                storage = data['apiInfo']['storageApi']
                allowed = storage['allowed']
                if not {'listBuckets', 'listFiles', 'readFiles', 'writeFiles', 'deleteFiles'}.issubset(allowed['capabilities']):
                    raise ProviderError('Backblaze key needs listBuckets, listFiles, readFiles, writeFiles and deleteFiles capabilities.')
                buckets = allowed.get('buckets')
                if buckets is not None and not any(b['id'] == self.credentials['bucket_id'] for b in buckets):
                    raise ProviderError('Backblaze application key does not allow the selected bucket.')
                if allowed.get('namePrefix'):
                    raise ProviderError('Use a bucket-restricted Backblaze key without a filename prefix restriction.')
                self.auth = dict(token=data['authorizationToken'], api=storage['apiUrl'], download=storage['downloadUrl'], account=data['accountId'])
            except (ValueError, KeyError, TypeError):
                raise ProviderError('Backblaze returned an invalid authorization response.') from None
        return self.auth

    def api(self, operation, body):
        auth = self.authorize()
        try:
            return self.request('POST', auth['api'] + '/b2api/v4/' + operation,
                headers={'Authorization': auth['token']}, json=body).json()
        except ValueError:
            raise ProviderError('Backblaze returned an invalid API response.') from None

    def check(self):
        auth = self.authorize()
        try:
            buckets = self.api('b2_list_buckets', {'accountId':auth['account'], 'bucketId':self.credentials['bucket_id']})['buckets']
            if not any(b['bucketId'] == self.credentials['bucket_id'] and b['bucketName'] == self.credentials['bucket_name'] for b in buckets):
                raise ProviderError('Backblaze bucket ID and name do not match an accessible bucket.')
            self.api('b2_list_file_names', {'bucketId':self.credentials['bucket_id'], 'maxFileCount':1})
        except (KeyError, TypeError):
            raise ProviderError('Backblaze returned invalid bucket information.') from None
        return dict(ok=True, account=self.credentials['bucket_name'], message='Backblaze bucket access verified. No test file was written. Capacity is the budget configured in CSO; provider usage was not scanned.')

    def put(self, path, content):
        if len(content)>self.LARGE_FILE_THRESHOLD:
            return self.put_large(path,content)
        try:
            upload = self.api('b2_get_upload_url', {'bucketId':self.credentials['bucket_id']})
            result = self.request('POST', upload['uploadUrl'], headers={
                'Authorization':upload['authorizationToken'], 'X-Bz-File-Name':quote(path, safe=''),
                'Content-Type':'application/octet-stream', 'X-Bz-Content-Sha1':hashlib.sha1(content).hexdigest()}, data=content,timeout=(30,600)).json()
            return json.dumps({'file_id':result['fileId'], 'file_name':result['fileName']})
        except (ValueError, KeyError, TypeError):
            raise ProviderError('Backblaze returned an invalid upload response.') from None

    def put_large(self,path,content):
        file_id=None
        finishing=False
        try:
            started=self.api('b2_start_large_file',{'bucketId':self.credentials['bucket_id'],'fileName':path,
                'contentType':'application/octet-stream','fileInfo':{'large_file_sha1':hashlib.sha1(content).hexdigest()}})
            file_id=started['fileId']
            count=(len(content)+self.PART_SIZE-1)//self.PART_SIZE
            hashes=[]
            if self.progress:
                self.progress(0,count+1,f'Uploading {count} Backblaze parts.')
            for index in range(count):
                if self.progress:
                    self.progress(index,count+1,f'Uploading Backblaze part {index+1} of {count}.')
                part=content[index*self.PART_SIZE:(index+1)*self.PART_SIZE]
                digest=hashlib.sha1(part).hexdigest()
                for attempt in range(3):
                    try:
                        upload=self.api('b2_get_upload_part_url',{'fileId':file_id})
                        result=self.request('POST',upload['uploadUrl'],headers={
                            'Authorization':upload['authorizationToken'],'X-Bz-Part-Number':str(index+1),
                            'X-Bz-Content-Sha1':digest,'Content-Type':'application/octet-stream'},
                            data=part,timeout=(30,600)).json()
                        if result['contentSha1']!=digest or result['partNumber']!=index+1:
                            raise ProviderError('Backblaze part checksum confirmation did not match.')
                        break
                    except B2TransientError:
                        if attempt==2:
                            raise
                        if self.progress:
                            self.progress(index,count+1,f'Retrying Backblaze part {index+1} of {count}.')
                        time.sleep(0.5*(attempt+1))
                hashes.append(digest)
                if self.progress:
                    self.progress(index+1,count+1,f'Uploaded Backblaze part {index+1} of {count}; finalizing after all parts.')
            if self.progress:
                self.progress(count,count+1,'Finalizing Backblaze multipart upload.')
            finishing=True
            result=self.api('b2_finish_large_file',{'fileId':file_id,'partSha1Array':hashes})
            if result['fileId']!=file_id or result['fileName']!=path:
                raise ProviderError('Backblaze did not confirm the completed file identity.')
            return json.dumps({'file_id':file_id,'file_name':path})
        except (ValueError,KeyError,TypeError):
            raise ProviderError('Backblaze returned an invalid multipart response.') from None
        finally:
            if file_id and not finishing:
                # Cancel incomplete parts on failure. Never delete a possibly finalized version.
                try:
                    self.api('b2_cancel_large_file',{'fileId':file_id})
                except ProviderError:
                    pass

    def identity(self, object_id):
        try:
            identity = json.loads(object_id)
            if not all(isinstance(identity[k], str) and identity[k] for k in ('file_id','file_name')):
                raise ValueError()
            return identity
        except (ValueError, KeyError, TypeError):
            raise ProviderError('Invalid Backblaze file identity.') from None

    def get(self, object_id):
        identity = self.identity(object_id)
        auth = self.authorize()
        return self.request('GET', auth['download'] + '/b2api/v4/b2_download_file_by_id',
            headers={'Authorization':auth['token']}, params={'fileId':identity['file_id']}).content

    def delete(self, object_id):
        identity = self.identity(object_id)
        auth = self.authorize()
        payload = {'fileId':identity['file_id'], 'fileName':identity['file_name']}
        deleted = self.request('POST', auth['api'] + '/b2api/v4/b2_delete_file_version',
            headers={'Authorization':auth['token']}, json=payload, accepted=(400,404))
        if deleted.status_code == 400:
            try:
                missing = deleted.json().get('code') == 'file_not_present'
            except (ValueError, AttributeError):
                missing = False
            if not missing:
                raise ProviderError('Backblaze refused file deletion. Check file retention and application key permissions.')
        if deleted.status_code not in (400,404):
            try:
                result = deleted.json()
                if result['fileId'] != identity['file_id'] or result['fileName'] != identity['file_name']:
                    raise ValueError()
            except (ValueError, KeyError, TypeError):
                raise ProviderError('Backblaze did not confirm deletion of the expected file version.') from None
        # Keep the catalog entry unless the exact cloud version is confirmed absent.
        probe = self.request('POST', auth['api'] + '/b2api/v4/b2_get_file_info',
            headers={'Authorization':auth['token']}, json={'fileId':identity['file_id']}, accepted=(404,))
        if probe.status_code != 404:
            raise ProviderError('Backblaze still reports this file version. Its catalog entry was kept; check retention and retry.')
