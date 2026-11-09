import unittest
from unittest.mock import Mock, patch
from providers import Provider, ProviderError

class ProviderTests(unittest.TestCase):
    def test_dropbox_creates_parent_folders_then_uploads(self):
        provider=Provider({'id':1,'provider':'dropbox'},{'access_token':'token'},'/tmp')
        with patch.object(provider,'request') as call:
            result=provider.put('CSO/App/docs/report.pdf',b'file')
        self.assertEqual(result,'/CSO/App/docs/report.pdf')
        self.assertEqual(call.call_count,4)
        self.assertEqual(call.call_args_list[0].kwargs['json']['path'],'/CSO')
        self.assertEqual(call.call_args_list[2].kwargs['json']['path'],'/CSO/App/docs')
        self.assertEqual(call.call_args.kwargs['data'],b'file')

    def test_google_tracks_uploaded_id_and_resumable_body(self):
        provider=Provider({'id':1,'provider':'google'},{'access_token':'token'},'/tmp')
        folder=Mock();folder.json.return_value={'files':[{'id':'folder'}]}
        init=Mock(headers={'Location':'https://www.googleapis.com/upload/drive/v3/files?upload_id=123'})
        final=Mock();final.json.return_value={'id':'object'}
        with patch.object(provider,'request',side_effect=[folder,folder,init,final]) as call:
            result=provider.put('CSO/App/report.pdf',b'file')
        self.assertEqual(result,'object')
        self.assertEqual(call.call_args.args[0],'PUT')
        self.assertEqual(call.call_args.kwargs['data'],b'file')

    def test_google_rejects_foreign_upload_destination(self):
        provider=Provider({'id':1,'provider':'google'},{'access_token':'token'},'/tmp')
        folder=Mock();folder.json.return_value={'files':[{'id':'folder'}]}
        init=Mock(headers={'Location':'https://untrusted.example/upload'})
        with patch.object(provider,'request',side_effect=[folder,folder,init]):
            with self.assertRaises(ProviderError):
                provider.put('CSO/App/report.pdf',b'file')

    def test_requests_disable_redirect_and_timeout(self):
        provider=Provider({'id':1,'provider':'dropbox'},{'access_token':'token'},'/tmp')
        response=Mock(status_code=200)
        with patch('providers.requests.request',return_value=response) as call:
            provider.request('GET','https://example.test')
        self.assertFalse(call.call_args.kwargs['allow_redirects'])
        self.assertEqual(call.call_args.kwargs['timeout'],60)
        self.assertEqual(call.call_args.kwargs['headers']['Authorization'],'Bearer token')

    def test_google_probe_reads_account_quota_without_writes(self):
        provider=Provider({'id':1,'provider':'google'},{'access_token':'token'},'/tmp')
        response=Mock();response.json.return_value={'user':{'emailAddress':'person@example.com'},'storageQuota':{'limit':'15000000000','usage':'2000000000'}}
        with patch.object(provider,'request',return_value=response) as call:
            result=provider.check()
        self.assertEqual(result['available'],13000000000)
        self.assertEqual(call.call_count,1)
        self.assertEqual(call.call_args.args[0],'GET')
        self.assertEqual(call.call_args.args[1],'https://www.googleapis.com/drive/v3/about')

    def test_refresh_token_is_used_without_redirecting_secrets(self):
        provider=Provider({'id':1,'provider':'google'},{'access_token':'expired','refresh_token':'refresh','client_id':'id','client_secret':'secret'},'/tmp')
        unauthorized=Mock(status_code=401)
        success=Mock(status_code=200)
        token=Mock(status_code=200);token.json.return_value={'access_token':'fresh'}
        with patch('providers.requests.request',side_effect=[unauthorized,success]), patch('providers.requests.post',return_value=token) as exchange:
            provider.request('GET','https://www.googleapis.com/drive/v3/about')
        self.assertFalse(exchange.call_args.kwargs['allow_redirects'])
        self.assertEqual(provider.credentials['access_token'],'fresh')

    def test_google_probe_handles_missing_quota_limit(self):
        provider=Provider({'id':1,'provider':'google'},{'access_token':'token'},'/tmp')
        response=Mock();response.json.return_value={'user':{'displayName':'User'},'storageQuota':{'usage':'2'}}
        with patch.object(provider,'request',return_value=response):
            result=provider.check()
        self.assertTrue(result['ok'])
        self.assertNotIn('capacity',result)

    def test_google_probe_sanitizes_credential_failure(self):
        provider=Provider({'id':1,'provider':'google'},{'access_token':'token'},'/tmp')
        response=Mock(status_code=401)
        with patch('providers.requests.request',return_value=response):
            with self.assertRaisesRegex(ProviderError,'invalid or expired'):
                provider.check()
