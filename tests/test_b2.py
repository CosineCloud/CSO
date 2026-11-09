import json
import unittest
from unittest.mock import Mock, patch
from providers import Provider, ProviderError, validate_credentials
from b2_provider import B2Provider

CREDS=dict(key_id='key',application_key='secret',bucket_id='bucket',bucket_name='cso-bucket')
AUTH={'authorizationToken':'token','accountId':'account','apiInfo':{'storageApi':{
    'apiUrl':'https://api001.backblazeb2.com','downloadUrl':'https://f001.backblazeb2.com',
    'allowed':{'buckets':[{'id':'bucket','name':'cso-bucket'}], 'namePrefix':None,
               'capabilities':['listBuckets','listFiles','readFiles','writeFiles','deleteFiles']}}}}

class B2Tests(unittest.TestCase):
    def response(self,data):
        r=Mock(status_code=200);r.json.return_value=data;return r

    def test_readonly_probe_and_basic_auth(self):
        responses=[self.response(AUTH),self.response({'buckets':[{'bucketId':'bucket','bucketName':'cso-bucket'}]}),self.response({'files':[]})]
        with patch('b2_provider.requests.request',side_effect=responses) as request:
            result=Provider({'provider':'b2','id':1},CREDS,'/tmp').check()
        self.assertTrue(result['ok'])
        self.assertNotIn('capacity',result)
        self.assertEqual(request.call_args_list[0].kwargs['auth'],('key','secret'))
        for call in request.call_args_list:
            self.assertFalse(call.kwargs['allow_redirects'])
        self.assertTrue(request.call_args.args[1].endswith('b2_list_file_names'))

    def test_upload_and_exact_version_operations(self):
        b=B2Provider(CREDS)
        responses=[self.response(AUTH),self.response({'uploadUrl':'https://pod.backblaze.com/upload','authorizationToken':'upload-token'}),self.response({'fileId':'version','fileName':'CSO/a b'})]
        with patch('b2_provider.requests.request',side_effect=responses) as request:
            identity=b.put('CSO/a b',b'contents')
        self.assertEqual(json.loads(identity)['file_id'],'version')
        self.assertEqual(request.call_args.kwargs['headers']['X-Bz-File-Name'],'CSO%2Fa%20b')
        self.assertEqual(request.call_args.kwargs['headers']['Authorization'],'upload-token')
        with patch('b2_provider.requests.request',return_value=Mock(status_code=200,content=b'contents')) as request:
            self.assertEqual(b.get(identity),b'contents')
            self.assertEqual(request.call_args.kwargs['params'],{'fileId':'version'})
        with patch('b2_provider.requests.request',side_effect=[self.response({'fileId':'version','fileName':'CSO/a b'}),Mock(status_code=404)]) as request:
            b.delete(identity)
            self.assertTrue(request.call_args_list[0].args[1].endswith('b2_delete_file_version'))
            self.assertTrue(request.call_args.args[1].endswith('b2_get_file_info'))
            self.assertEqual(request.call_args.kwargs['json'],{'fileId':'version'})

    def test_untrusted_urls_and_errors(self):
        b=B2Provider(CREDS)
        for url in ['http://api.backblazeb2.com','https://evil.test','https://backblazeb2.com.evil.test','https://user@api.backblazeb2.com']:
            with patch('b2_provider.requests.request') as request:
                with self.assertRaises(ProviderError): b.request('GET',url)
                request.assert_not_called()
        with patch('b2_provider.requests.request',return_value=Mock(status_code=401,text='secret')):
            with self.assertRaisesRegex(ProviderError,'rejected access') as err: b.authorize()
            self.assertNotIn('secret',str(err.exception))
        with self.assertRaises(ValueError): validate_credentials('b2',{'key_id':'key'})
        with self.assertRaises(ProviderError): b.identity('invalid')

    def test_wrong_bucket_and_capabilities_rejected(self):
        for allowed in [dict(buckets=[{'id':'other'}],capabilities=AUTH['apiInfo']['storageApi']['allowed']['capabilities']),dict(buckets=None,capabilities=['readFiles'])]:
            data=json.loads(json.dumps(AUTH));data['apiInfo']['storageApi']['allowed']=allowed
            with patch('b2_provider.requests.request',return_value=self.response(data)):
                with self.assertRaises(ProviderError): B2Provider(CREDS).authorize()

    def test_delete_retains_catalog_when_remote_version_remains(self):
        b=B2Provider(CREDS)
        b.auth=dict(api='https://api001.backblazeb2.com',token='token')
        identity=json.dumps({'file_id':'version','file_name':'file'})
        with patch('b2_provider.requests.request',side_effect=[self.response({'fileId':'version','fileName':'file'}),self.response({'fileId':'version'})]):
            with self.assertRaisesRegex(ProviderError,'still reports'):
                b.delete(identity)
        with patch('b2_provider.requests.request',side_effect=[Mock(status_code=400,json=lambda:{'code':'file_not_present'}),Mock(status_code=404)]):
            b.delete(identity)

    def test_multipart_parts_checksums_finish_and_progress(self):
        import hashlib
        progress=Mock()
        b=B2Provider(CREDS,progress=progress)
        b.LARGE_FILE_THRESHOLD=2;b.PART_SIZE=3
        def api(operation,body):
            if operation=='b2_start_large_file':return {'fileId':'large'}
            if operation=='b2_get_upload_part_url':return {'uploadUrl':'https://pod.backblaze.com/upload','authorizationToken':'part-token'}
            if operation=='b2_finish_large_file':return {'fileId':'large','fileName':'file'}
            return {}
        responses=[self.response({'partNumber':1,'contentSha1':hashlib.sha1(b'abc').hexdigest()}),self.response({'partNumber':2,'contentSha1':hashlib.sha1(b'def').hexdigest()})]
        with patch.object(b,'api',side_effect=api) as call,patch.object(b,'request',side_effect=responses) as request:
            result=json.loads(b.put('file',b'abcdef'))
        self.assertEqual(result['file_id'],'large')
        finish=next(c for c in call.call_args_list if c.args[0]=='b2_finish_large_file')
        self.assertEqual(finish.args[1]['partSha1Array'],[hashlib.sha1(x).hexdigest() for x in (b'abc',b'def')])
        self.assertEqual(request.call_args.kwargs['timeout'],(30,600))
        self.assertEqual(progress.call_args.args[:2],(2,3))

    def test_multipart_retries_parts_and_cancels_failed_upload(self):
        import hashlib
        from b2_provider import B2TransientError
        b=B2Provider(CREDS);b.LARGE_FILE_THRESHOLD=1;b.PART_SIZE=2
        def api(operation,body):
            if operation=='b2_start_large_file':return {'fileId':'large'}
            if operation=='b2_get_upload_part_url':return {'uploadUrl':'https://pod.backblaze.com/upload','authorizationToken':'part-token'}
            return {}
        with patch.object(b,'api',side_effect=api) as call,patch.object(b,'request',side_effect=B2TransientError('timeout')),patch('b2_provider.time.sleep'):
            with self.assertRaises(B2TransientError):b.put('file',b'abcd')
        self.assertEqual(sum(c.args[0]=='b2_get_upload_part_url' for c in call.call_args_list),3)
        call.assert_called_with('b2_cancel_large_file',{'fileId':'large'})

    def test_timeout_is_specific_and_preserves_no_secrets(self):
        import requests
        with patch('b2_provider.requests.request',side_effect=requests.Timeout('secret')):
            with self.assertRaisesRegex(ProviderError,'timed out') as exc:
                B2Provider(CREDS).authorize()
            self.assertNotIn('secret',str(exc.exception))
