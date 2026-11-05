import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from studio import local_story_provider as provider


class Response:
    def __init__(self,body,headers=None,status=200):self.body=body;self.headers=headers or {};self.status=status
    def read(self,size):return self.body[:size]
    def __enter__(self):return self
    def __exit__(self,*args):pass


class LocalStoryProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        (self.root/'story-api-key.txt').write_text('fixture-key-only',encoding='utf-8')
        self.config={'base_url':'http://127.0.0.1:6008/v1','model':'Qwen3.5-9B','api_key_file':'story-api-key.txt'}
    def tearDown(self):self.temp.cleanup()
    def save(self,**changes):
        (self.root/'story-provider.json').write_text(json.dumps({**self.config,**changes}),encoding='utf-8')
    def body(self,**choice):
        return json.dumps({'choices':[{'finish_reason':'stop','message':{'content':'{"scenes":[]}'},**choice}]}).encode()

    def test_absent_configuration_returns_none_but_invalid_configuration_fails(self):
        self.assertIsNone(provider.get_provider(self.root))
        (self.root/'story-provider.json').write_text('{bad',encoding='utf-8')
        with self.assertRaises(provider.LocalStoryProviderError):provider.get_provider(self.root)

    def test_external_urls_credentials_query_and_fragment_are_rejected(self):
        for url in ('https://127.0.0.1/v1','http://example.com/v1','http://192.168.1.4/v1','http://127.0.0.1.evil.test/v1',
                    'http://user:secret@127.0.0.1/v1','http://localhost/v1?x=1','http://localhost/v1#x','http://localhost/v1?',
                    'http://127.0.0.1:99999/v1','http://localhost/a/../v1'):
            self.save(base_url=url)
            with self.subTest(url=url),self.assertRaises(provider.LocalStoryProviderError):provider.get_provider(self.root)
        for url in ('http://localhost:6008/v1','http://[::1]:6008/v1','http://127.1.2.3:6008/v1'):
            self.assertTrue(provider._endpoint(url).endswith('/chat/completions'))

    def test_key_paths_and_bad_key_files_are_rejected_without_disclosure(self):
        for name in ('../key.txt','..\\key.txt','C:\\key.txt','/tmp/key.txt','key.txt:secret','missing.txt'):
            self.save(api_key_file=name)
            with self.subTest(name=name),self.assertRaises(provider.LocalStoryProviderError):provider.get_provider(self.root)
        for secret in ('x'*4097,'fixture\nkey',''):
            (self.root/'story-api-key.txt').write_text(secret,encoding='utf-8');self.save()
            with self.assertRaises(provider.LocalStoryProviderError) as error:provider.get_provider(self.root)
            self.assertNotIn('fixture',str(error.exception))

    def test_normal_request_is_authenticated_bounded_and_non_thinking(self):
        self.save()
        with patch.object(provider.urllib.request,'build_opener') as builder:
            builder.return_value.open.return_value=Response(self.body())
            generate=provider.get_provider(self.root)
            messages=[{'role':'user','content':'写一个故事'}]
            self.assertEqual(generate(messages,2000),'{"scenes":[]}')
            args,kwargs=builder.return_value.open.call_args;request=args[0]
            self.assertEqual(request.full_url,'http://127.0.0.1:6008/v1/chat/completions')
            self.assertEqual(request.get_method(),'POST');self.assertEqual(request.get_header('Authorization'),'Bearer fixture-key-only')
            payload=json.loads(request.data)
            self.assertEqual(payload['messages'],messages);self.assertEqual(payload['max_tokens'],2000)
            self.assertEqual(payload['temperature'],0);self.assertEqual(payload['model'],'Qwen3.5-9B')
            self.assertEqual(payload['response_format'],{'type':'json_object'})
            self.assertEqual(payload['chat_template_kwargs'],{'enable_thinking':False})
            self.assertEqual(kwargs['timeout'],120)
            self.assertTrue(any(isinstance(h,provider._NoRedirect) for h in builder.call_args.args))

    def test_invalid_truncated_empty_and_oversized_results_fail(self):
        cases=[Response(self.body(finish_reason='length')),Response(self.body(finish_reason=None)),Response(b'not-json'),
               Response(self.body(message={'content':''})),Response(self.body(message={'content':None})),
               Response(b'x'*(provider.MAX_RESPONSE_BYTES+1)),Response(b'{}',{'Content-Length':str(provider.MAX_RESPONSE_BYTES+1)}),
               Response(self.body(),status=302)]
        self.save()
        with patch.object(provider.urllib.request,'build_opener') as builder:
            generate=provider.get_provider(self.root)
            for response in cases:
                builder.return_value.open.return_value=response
                with self.subTest(response=response),self.assertRaises(provider.LocalStoryProviderError):generate([{'role':'user','content':'测试'}],100)

    def test_bounded_planning_reasoning_returns_only_final_content(self):
        self.save()
        with patch.object(provider.urllib.request,'build_opener') as builder:
            builder.return_value.open.return_value=Response(self.body(message={'content':'{"supported":false}','reasoning_content':'internal deliberation'}))
            generate=provider.get_provider(self.root,reasoning_tokens=768)
            self.assertEqual(generate([{'role':'user','content':'核查依据'}],300),'{"supported":false}')
            payload=json.loads(builder.return_value.open.call_args.args[0].data)
            self.assertEqual(payload['max_tokens'],1068)
            self.assertEqual(payload['reasoning_budget_tokens'],768)
            self.assertTrue(payload['chat_template_kwargs']['enable_thinking'])
        for budget in (-1,2049,True,1.5):
            with self.assertRaises(provider.LocalStoryProviderError):provider.get_provider(self.root,reasoning_tokens=budget)

    def test_structured_review_preserves_schema_and_bounded_reasoning(self):
        self.save()
        schema={'type':'object','required':['passed'],'properties':{'passed':{'type':'boolean'}},'additionalProperties':False}
        with patch.object(provider.urllib.request,'build_opener') as builder:
            builder.return_value.open.return_value=Response(self.body(message={'content':'{"passed":false}'}))
            generate=provider.get_provider(self.root,reasoning_tokens=768)
            self.assertEqual(generate([{'role':'user','content':'核查'}],300,response_schema=schema),'{"passed":false}')
            payload=json.loads(builder.return_value.open.call_args.args[0].data)
            self.assertEqual(payload['response_format']['json_schema']['schema'],schema)
            self.assertEqual(payload['reasoning_budget_tokens'],768)
            with self.assertRaises(provider.LocalStoryProviderError):generate([{'role':'user','content':'核查'}],300,response_schema='invalid')

    def test_http_errors_timeouts_and_redirects_do_not_leak_body_or_key(self):
        self.save()
        with patch.object(provider.urllib.request,'build_opener') as builder:
            generate=provider.get_provider(self.root)
            errors=[urllib.error.HTTPError('http://localhost',302,'fixture-key-only',{},io.BytesIO(b'private-response')),
                    urllib.error.HTTPError('http://localhost',401,'fixture-key-only',{},io.BytesIO(b'private-response')),
                    urllib.error.URLError('fixture-key-only'),TimeoutError('fixture-key-only'),
                    provider.http.client.IncompleteRead(b'private-response',100)]
            for error in errors:
                builder.return_value.open.side_effect=error
                with self.assertRaises(provider.LocalStoryProviderError) as caught:generate([{'role':'user','content':'测试'}],100)
                self.assertNotIn('fixture-key-only',str(caught.exception));self.assertNotIn('private-response',str(caught.exception))
        self.assertIsNone(provider._NoRedirect().redirect_request(None,None,302,'',{},'http://example.com'))


if __name__=='__main__':unittest.main()
