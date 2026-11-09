import json
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
from providers import Provider, ProviderError, validate_credentials


class MegaTests(unittest.TestCase):
    def setUp(self):
        self.provider = Provider({'id': 1, 'provider': 'mega'},
                                 {'email': 'user@example.com', 'password': 'secret'}, '/tmp')

    def test_credentials_required(self):
        for creds in ({}, {'email': 'a'}, {'email': 'a', 'password': ''}):
            with self.assertRaises(ValueError):
                validate_credentials('mega', creds)

    def test_commands_keep_secrets_out_of_argv_and_errors(self):
        with patch('providers.subprocess.run', side_effect=[Mock(stdout=b'obscured\n'), Mock(stdout=b'file')]) as run:
            self.assertEqual(self.provider.get('CSO/app/a.txt'), b'file')
        self.assertEqual(run.call_args.args[0][-2:], ['cat', 'cso:CSO/app/a.txt'])
        self.assertNotIn('secret', ' '.join(run.call_args.args[0]))
        self.assertEqual(run.call_args.kwargs['env']['RCLONE_CONFIG_CSO_PASS'], 'obscured')
        self.assertEqual(run.call_args_list[0].kwargs['input'], b'secret')
        with patch('providers.subprocess.run', side_effect=subprocess.CalledProcessError(1, ['rclone'], stderr=b'secret')):
            with self.assertRaises(ProviderError) as error:
                self.provider.get('file')
        self.assertNotIn('secret', str(error.exception))

    def test_upload_download_delete_and_readonly_check(self):
        with patch.object(self.provider, 'mega_command', return_value=b'') as run:
            self.assertEqual(self.provider.put('CSO/app/file', b'payload'), 'CSO/app/file')
            self.assertEqual(run.call_args.args[0], 'copyto')
            self.assertEqual(run.call_args.args[2], 'cso:CSO/app/file')
            self.provider.delete('CSO/app/file')
            run.assert_called_with('--mega-hard-delete', 'deletefile', 'cso:CSO/app/file')
        with patch.object(self.provider, 'mega_command', return_value=json.dumps({'total': 100, 'used': 20}).encode()) as run:
            self.assertEqual(self.provider.check()['available'], 80)
            run.assert_called_once_with('about', 'cso:', '--json')

    def test_invalid_paths_and_missing_dependency(self):
        for path in ('../file', '/file', 'a/../b', 'a//b', ''):
            with self.assertRaises(ProviderError):
                self.provider.get(path)
        with patch('providers.subprocess.run', side_effect=FileNotFoundError):
            with self.assertRaisesRegex(ProviderError, 'Install rclone'):
                self.provider.check()

    def test_failures_are_classified_without_exposing_provider_output(self):
        cases = [(b'lookup g.api.mega.co.nz: no such host secret', 'cannot reach'),
                 (b'could not login: invalid password secret', 'rejected the login'),
                 (b'2FA required secret', 'two-factor'),
                 (b'quota exceeded secret', 'quota is exhausted'),
                 (b'too many requests secret', 'limiting requests')]
        for stderr, expected in cases:
            with self.subTest(expected=expected), patch('providers.subprocess.run', side_effect=subprocess.CalledProcessError(1, ['rclone'], stderr=stderr)):
                with self.assertRaisesRegex(ProviderError, expected) as error:
                    self.provider.check()
                self.assertNotIn('secret', str(error.exception))
