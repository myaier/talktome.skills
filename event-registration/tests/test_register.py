import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('register', Path(__file__).parents[1] / 'scripts/register.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
ID = '1234567890abcdef'
Q = [{'id':'intro','label':'介绍','type':'text','required':True}]
FORM = {'event':{'id':'uuid','title':'活动','status':'open'},'questions':Q,'formVersion':1}
class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.client=m.Client('http://localhost:8097',Path(self.tmp.name))
    def tearDown(self): self.tmp.cleanup()
    def test_first_save_requires_real_form_review(self):
        with patch.object(self.client,'call',return_value=FORM) as call:
            with self.assertRaises(m.ApiError) as ctx:self.client.save(ID,{'intro':'我'})
            self.assertEqual(ctx.exception.code,'FORM_REVIEW_REQUIRED')
            self.assertEqual(call.call_count,1)
    def test_changed_questions_do_not_silently_reuse_old_answers(self):
        kept,changed=m.reconcile(Q,[{**Q[0],'label':'不同的问题'}],{'intro':'旧答案'})
        self.assertEqual(kept,{})
        self.assertEqual(changed,['intro'])
    def test_new_optional_form_version_is_carried_automatically(self):
        m.write_private(self.client.context_path(ID),FORM)
        current={**FORM,'formVersion':2,'questions':Q+[{'id':'optional','label':'可选','type':'text','required':False}]}
        with patch.object(self.client,'call',side_effect=[current,{'result':{'expiresAt':'later'}}]) as call:
            self.assertTrue(self.client.save(ID,{'intro':'我'})['saved'])
            self.assertEqual(call.call_args_list[-1].args[1]['formVersion'],2)
            self.assertEqual(call.call_args_list[-1].args[0],'/api/events/draft')
    def test_lost_response_reuses_the_idempotency_key(self):
        m.write_private(self.client.context_path(ID),FORM)
        sent=[]
        def call(path,payload,**kwargs):
            if path.endswith('/get'):return FORM
            sent.append(payload['operationKey'])
            if len(sent)==1:raise OSError('response lost')
            return {'result':{'expiresAt':'later'}}
        with patch.object(self.client,'call',side_effect=call):
            with self.assertRaises(OSError):self.client.save(ID,{'intro':'我'})
            self.assertTrue(self.client.save(ID,{'intro':'我'})['saved'])
        self.assertEqual(sent[0],sent[1])
    def test_private_sessions_are_isolated_by_environment(self):
        other=m.Client('https://example.test',Path(self.tmp.name))
        self.client.save_session({'accessToken':'test','refreshToken':'test-refresh'})
        self.assertNotEqual(other.session_file,self.client.session_file)
        self.assertEqual(self.client.session_file.stat().st_mode & 0o777,0o600)
        self.assertEqual(other.session,{})
if __name__=='__main__':unittest.main()
