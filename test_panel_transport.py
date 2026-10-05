"""Offline regression tests for cookie-gated panel API and disconnected requests."""
import http.client
import importlib.util
import io
import os
from pathlib import Path
from unittest.mock import Mock, patch
import unittest
import base64
import json
import re
import shutil
import subprocess
import sys
from email.message import Message

os.environ.update(PANEL_URL='https://panel.example.com/?gate=exampleSecret', PANEL_TOKEN='exampleToken',
    NODE_DOMAIN='node.example.com', NODE_NAME='Node', PANEL_IP='203.0.113.2')
os.environ.pop('PANEL_ACCESS_COOKIE', None)
spec = importlib.util.spec_from_file_location('panel_transport', Path(__file__).with_name('panel_setup.py'))
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class TransportTests(unittest.TestCase):
    def test_node_name_paste_normalization_in_bash(self):
        bash = shutil.which('bash')
        if not bash and os.name == 'nt':
            candidate = Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'Git/bin/bash.exe'
            bash = str(candidate) if candidate.exists() else None
        if not bash:
            self.skipTest('Bash is required for the input regression test')
        script = Path(__file__).with_name('bootstrap.sh').read_text(encoding='utf-8')
        function = re.search(r'(?ms)^normalize_node_name\(\) \{\n.*?^\}', script)
        self.assertIsNotNone(function)
        for entered, expected in [
                ('NODE-FI-02', 'NODE-FI-02'),
                (' \tNODE-FI-02 \r\n', 'NODE-FI-02'),
                ('\ufeff\u00a0NODE-FI-02\u202f\u200b', 'NODE-FI-02'),
                ('NODE FI 02', 'NODE FI 02'),
                (' \r\n', '')]:
            with self.subTest(entered=repr(entered)):
                result = subprocess.check_output([bash, '-c',
                    function.group(0) + '\nnormalize_node_name "$1"', 'test', entered],
                    text=True, encoding='utf-8', timeout=10)
                self.assertEqual(result, expected)

    def test_token_diagnostics_hides_claim_identifiers(self):
        claims = base64.urlsafe_b64encode(json.dumps({'role': 'API', 'uuid': 'private-identifier'}).encode()).decode().rstrip('=')
        with patch.object(helper, 'TOKEN', 'header.' + claims + '.signature'):
            detail = helper.token_diagnostics()
        self.assertIn('role=API', detail)
        self.assertIn('signature NOT verified', detail)
        self.assertNotIn('private-identifier', detail)

    def test_401_details_hide_secrets_and_do_not_retry(self):
        headers = Message()
        headers['Content-Type'] = 'application/json'
        error = helper.urllib.error.HTTPError('https://panel.example.com/api/nodes', 401,
            'Unauthorized', headers, self.response(json.dumps({'message':
                'Unauthorized exampleToken exampleSecret'}).encode()))
        opener = Mock()
        opener.open.side_effect = error
        with patch.object(helper.urllib.request, 'build_opener', return_value=opener):
            with self.assertRaises(RuntimeError) as result:
                helper.api('GET', 'nodes')
        self.assertEqual(opener.open.call_count, 1)
        self.assertIn('JSON auth response', str(result.exception))
        self.assertNotIn('exampleToken', str(result.exception))
        self.assertNotIn('exampleSecret', str(result.exception))

    def test_401_html_basic_challenge(self):
        headers = Message()
        headers['WWW-Authenticate'] = 'Basic realm="private"'
        error = helper.urllib.error.HTTPError('https://panel.example.com/api/nodes', 401,
            'Unauthorized', headers, self.response(b'<html>login</html>'))
        detail = helper.auth_error_details(error)
        self.assertIn('Non-JSON', detail)
        self.assertIn('challenge=basic', detail)
        self.assertNotIn('private', detail)

    def test_opaque_token_and_optional_bearer_prefix(self):
        self.assertEqual(helper.normalize_token('  abc+/def==  '), 'abc+/def==')
        self.assertEqual(helper.normalize_token('Bearer abc+/def=='), 'abc+/def==')
        self.assertEqual(helper.normalize_token('  bearer\tabc+/def==\r\n'), 'abc+/def==')
        for value in ('', '   ', 'Bearer ', 'abc\r\ndef', 'abc def', 'abc\x00def'):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                helper.normalize_token(value)
        for pasted in ('eyJheader.payload.signature\\', '"eyJheader.payload.signature"',
                "'eyJheader.payload.signature'", 'Bearer "eyJheader.payload.signature"',
                '\ufeff\u00a0eyJheader.payload.signature\u200b'):
            with self.subTest(pasted=pasted):
                self.assertEqual(helper.normalize_token(pasted), 'eyJheader.payload.signature')
        for malformed in ('eyJheader.payload', 'eyJheader.payload.', 'eyJheader.payload.signature!'):
            with self.subTest(malformed=malformed), self.assertRaisesRegex(RuntimeError, 'JWT'):
                helper.normalize_token(malformed)

    def test_connection_validation_is_independent_of_token(self):
        environment = dict(os.environ, PANEL_TOKEN='eyJheader.incomplete')
        result = subprocess.run([sys.executable, '-B', str(Path(__file__).with_name('panel_setup.py')), 'connection'],
            env=environment, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ['https://panel.example.com', 'gate=exampleSecret'])
        self.assertNotIn('eyJheader', result.stdout + result.stderr)

    def response(self, body=b'{"response": []}'):
        return io.BytesIO(body)

    def test_cookie_sent_without_query_or_disk(self):
        opener = Mock()
        opener.open.return_value = self.response()
        with patch.object(helper.urllib.request, 'build_opener', return_value=opener):
            self.assertEqual(helper.api('GET', 'nodes'), [])
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, 'https://panel.example.com/api/nodes')
        self.assertEqual(request.get_header('Cookie'), 'gate=exampleSecret')
        self.assertEqual(request.get_header('Authorization'), 'Bearer exampleToken')

    def test_get_disconnect_retried(self):
        opener = Mock()
        opener.open.side_effect = [http.client.RemoteDisconnected(), http.client.RemoteDisconnected(), self.response()]
        with patch.object(helper.urllib.request, 'build_opener', return_value=opener), patch.object(helper.time, 'sleep'):
            self.assertEqual(helper.api('GET', 'nodes'), [])
        self.assertEqual(opener.open.call_count, 3)

    def test_post_not_retried_and_secrets_not_reported(self):
        opener = Mock()
        opener.open.side_effect = http.client.RemoteDisconnected()
        with patch.object(helper.urllib.request, 'build_opener', return_value=opener):
            with self.assertRaises(RuntimeError) as result:
                helper.api('POST', 'nodes', {'name': 'Node'})
        self.assertEqual(opener.open.call_count, 1)
        self.assertNotIn('exampleSecret', str(result.exception))
        self.assertNotIn('exampleToken', str(result.exception))
        self.assertIn('POST nodes', str(result.exception))

    def test_html_login_response_diagnosed(self):
        opener = Mock()
        opener.open.return_value = self.response(b'<html>Login</html>')
        with patch.object(helper.urllib.request, 'build_opener', return_value=opener):
            with self.assertRaisesRegex(RuntimeError, 'expected JSON'):
                helper.api('GET', 'nodes')

    def test_url_validation(self):
        self.assertEqual(helper.panel_connection('https://panel.example.com/auth/login?gate=secret'),
            ('https://panel.example.com', 'gate=secret'))
        self.assertEqual(helper.panel_connection('https://panel.example.com/'),
            ('https://panel.example.com', ''))
        for url in ('http://panel.example.com/', 'https://user:password@panel.example.com/',
                'https://panel.example.com/?gate=', 'https://panel.example.com/?a=b&c=d'):
            with self.subTest(url=url), self.assertRaises(RuntimeError):
                helper.panel_connection(url)


if __name__ == '__main__':
    unittest.main()
