"""Offline regression tests for card delivery and private session persistence."""
import base64
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import deploy

class Response:
    def __init__(self, value): self.value = value
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self): return json.dumps(self.value).encode()

class DeployTests(unittest.TestCase):
    def test_session_file_is_private_and_merges(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(deploy, 'ENV_PATH', Path(tmp)/'.env'):
            deploy.write_env({'TALKTOME_ACCESS_TOKEN':'test-access'})
            deploy.write_env({'TALKTOME_REFRESH_TOKEN':'test-refresh'})
            self.assertEqual(deploy.read_env()['TALKTOME_ACCESS_TOKEN'],'test-access')
            if os.name != 'nt': self.assertEqual(deploy.ENV_PATH.stat().st_mode & 0o777,0o600)

    def test_card_uses_owner_api_and_does_not_print_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            target=Path(tmp)/'card.png'
            png=b'\x89PNG\r\n\x1a\nfixture'
            def request(req, timeout):
                self.assertTrue(req.full_url.endswith('/api/agents/miniprogram-card'))
                self.assertEqual(json.loads(req.data),{'agentId':'fixture-agent','envVersion':'release'})
                self.assertEqual(req.get_header('Authorization'),'Bearer test-private-token')
                return Response({'imageBase64':base64.b64encode(png).decode(),'contentType':'image/png'})
            output=io.StringIO()
            with patch.object(sys,'argv',['deploy.py','--agent-id','fixture-agent','--card',str(target)]), patch.object(deploy,'read_env',return_value={'TALKTOME_ACCESS_TOKEN':'test-private-token'}),patch.object(deploy.urllib.request,'urlopen',side_effect=request),contextlib.redirect_stdout(output):
                deploy.main()
            self.assertEqual(target.read_bytes(),png)
            self.assertNotIn('test-private-token',output.getvalue())

    def test_card_rejects_non_image_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            target=Path(tmp)/'card.png'
            with patch.object(sys,'argv',['deploy.py','--agent-id','fixture-agent','--card',str(target)]),patch.object(deploy,'read_env',return_value={'TALKTOME_ACCESS_TOKEN':'fixture'}),patch.object(deploy.urllib.request,'urlopen',return_value=Response({'imageBase64':base64.b64encode(b'<html>').decode(),'contentType':'text/html'})):
                with self.assertRaises(SystemExit): deploy.main()
            self.assertFalse(target.exists())

if __name__ == '__main__': unittest.main()
