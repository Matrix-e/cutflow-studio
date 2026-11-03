"""Adversarial local gateway checks. All upstream traffic uses a mock transport."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import anyio
import httpx
from fastapi.testclient import TestClient

from studio.local_gateway import create_app


class MemoryStream(httpx.AsyncByteStream):
    def __init__(self,payload):
        self.payload,self.closed = payload,False
    async def __aiter__(self):
        midpoint = max(1,len(self.payload)//2)
        yield self.payload[:midpoint]
        yield self.payload[midpoint:]
    async def aclose(self):
        self.closed = True


class BrokenStream(MemoryStream):
    async def __aiter__(self):
        yield self.payload[:2]
        raise httpx.ReadError('sensitive upstream transport details must not escape')


class LocalGatewayTests(unittest.TestCase):
    GPU_TOKEN = "gpu-private-token-for-test"
    LOCAL_TOKEN = "local-browser-token-for-test"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.web = self.root / "web"
        self.web.mkdir()
        (self.web / "index.html").write_text("<html>Local studio</html>",encoding="utf-8")
        (self.root / "outside.txt").write_text("OUTSIDE-PRIVATE-SENTINEL",encoding="utf-8")
        self.calls = []
        self.streams = []
        self.response_mode = "normal"
        self.failures = []

        def response(status,payload,headers=None,broken=False):
            stream = BrokenStream(payload) if broken else MemoryStream(payload)
            self.streams.append(stream)
            return httpx.Response(status,headers={"Content-Length":str(len(payload)),**(headers or {})},stream=stream)

        async def upstream(request):
            body = await request.aread()
            self.calls.append({"method":request.method,"url":str(request.url),"headers":dict(request.headers),"body":body,
                               "timeout":request.extensions.get('timeout')})
            if self.failures:
                raise self.failures.pop(0)
            if self.response_mode=="stall":
                await anyio.sleep(60)
            if self.response_mode=="brief_stall":
                await anyio.sleep(0.03)
            if self.response_mode=="redirect":
                return response(302,b"redirect",{"Location":"https://evil.example/steal","Set-Cookie":"remote=secret"})
            if self.response_mode=="timeout":
                raise httpx.ReadTimeout("upstream detail with "+self.GPU_TOKEN)
            if self.response_mode=="server_error":
                return response(500,("private traceback "+self.GPU_TOKEN).encode(),{"Content-Type":"text/plain"})
            if self.response_mode=="range":
                return response(206,b"clip",{"Content-Type":"video/mp4","Content-Range":"bytes 0-3/10","Accept-Ranges":"bytes"})
            if self.response_mode=="video_body_error":
                return response(206,b"clip",{"Content-Type":"video/mp4","Content-Range":"bytes 0-3/10"},broken=True)
            if self.response_mode in {"body_error","body_error_once"} and (self.response_mode=="body_error" or len(self.calls)==1):
                return response(200,b'{"ok":true}',{"Content-Type":"application/json"},broken=True)
            return response(200,b'{"ok":true}',{"Content-Type":"application/json","Access-Control-Allow-Origin":"*","Set-Cookie":"remote=secret","X-Upstream-Private":"private","Location":"https://evil.example/ignored"})

        self.app = create_app(gpu_token=self.GPU_TOKEN,local_token=self.LOCAL_TOKEN,
                             upstream_transport=httpx.MockTransport(upstream),web_root=self.web)
        self.context = TestClient(self.app,base_url="http://127.0.0.1:8767",follow_redirects=False)
        self.client = self.context.__enter__()
        self.auth = {"Authorization":"Bearer "+self.LOCAL_TOKEN,"Sec-Fetch-Site":"same-origin"}
        self.session_headers = {"X-CutFlow-Local":"1","Sec-Fetch-Site":"same-origin"}

    def tearDown(self):
        self.context.__exit__(None,None,None)
        self.temp.cleanup()

    def assert_rejected_without_upstream(self,method,path,headers=None,**kwargs):
        previous = len(self.calls)
        response = self.client.request(method,path,headers=headers,**kwargs)
        self.assertIn(response.status_code,(400,401,403,404,405,413,422),response.text)
        self.assertEqual(len(self.calls),previous)
        self.assertNotIn(self.GPU_TOKEN,response.text)
        return response

    def test_session_returns_only_gateway_token_with_no_cache(self):
        response = self.client.get("/local-session",headers=self.session_headers)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()["token"],self.LOCAL_TOKEN)
        self.assertTrue(response.json()["local_gateway"])
        self.assertNotIn(self.GPU_TOKEN,response.text)
        self.assertIn("no-store",response.headers.get("cache-control",""))
        self.assertNotIn("access-control-allow-origin",response.headers)
        self.assertEqual(self.calls,[])

    def test_session_requires_non_simple_custom_header(self):
        self.assert_rejected_without_upstream("GET","/local-session")
        self.assert_rejected_without_upstream("GET","/local-session",headers={"X-CutFlow-Local":"0"})
        self.assert_rejected_without_upstream("POST","/local-session",headers=self.session_headers)

    def test_host_rebinding_aliases_and_wrong_ports_are_rejected(self):
        hosts = ["evil.example","127.0.0.1.evil.example:8767","localhost.evil.example:8767", "127.0.0.1:9999",
                 "user@127.0.0.1:8767","127.0.0.1:8767,evil.example","127.0.0.1","127.0.0.1:8767."]
        for host in hosts:
            with self.subTest(host=host):
                self.assert_rejected_without_upstream("GET","/local-session",headers={**self.session_headers,"Host":host})
                self.assert_rejected_without_upstream("POST","/api/jobs",headers={**self.auth,"Host":host},json={"source":"x.mp4"})

    def test_duplicate_host_and_origin_headers_are_rejected(self):
        self.assert_rejected_without_upstream("GET","/local-session",headers=[
            ("Host","127.0.0.1:8767"),("Host","evil.example"),("X-CutFlow-Local","1")])
        self.assert_rejected_without_upstream("GET","/local-session",headers=[
            ("X-CutFlow-Local","1"),("Origin","http://127.0.0.1:8767"),("Origin","https://evil.example")])

    def test_foreign_null_or_mismatched_origins_are_rejected(self):
        origins = ["null","https://evil.example","http://localhost:1234","http://127.0.0.1:8767.evil.example",
                   "http://127.0.0.1:8767/","https://127.0.0.1:8767","http://localhost:8767"]
        for origin in origins:
            with self.subTest(origin=origin):
                self.assert_rejected_without_upstream("GET","/local-session",headers={**self.session_headers,"Origin":origin})
                self.assert_rejected_without_upstream("PATCH","/api/jobs/one",headers={**self.auth,"Origin":origin},json={})

    def test_cross_site_and_same_site_requests_are_rejected(self):
        for site in ("cross-site","same-site","unexpected"):
            with self.subTest(site=site):
                self.assert_rejected_without_upstream("GET","/local-session",headers={**self.session_headers,"Sec-Fetch-Site":site})
                self.assert_rejected_without_upstream("GET","/api/jobs",headers={**self.auth,"Sec-Fetch-Site":site})

    def test_matching_localhost_origin_is_allowed(self):
        response = self.client.get("http://localhost:8767/local-session",headers={**self.session_headers,"Origin":"http://localhost:8767"})
        self.assertEqual(response.status_code,200,response.text)

    def test_cors_preflight_never_enables_cross_origin_access(self):
        response = self.assert_rejected_without_upstream("OPTIONS","/api/jobs",headers={"Origin":"https://evil.example",
            "Access-Control-Request-Method":"POST","Access-Control-Request-Headers":"authorization,x-cutflow-local"})
        self.assertNotIn("access-control-allow-origin",response.headers)
        self.assertNotIn("access-control-allow-headers",response.headers)

    def test_proxy_requires_gateway_bearer_and_rejects_query_token(self):
        for headers in ({},{"Authorization":"Bearer wrong"},{"Authorization":"Bearer "+self.GPU_TOKEN}):
            self.assert_rejected_without_upstream("GET","/api/jobs",headers=headers)
        self.assert_rejected_without_upstream("GET","/api/jobs/job1/video?token="+self.LOCAL_TOKEN)

    def test_valid_bearer_does_not_forward_query_tokens(self):
        response = self.client.get("/api/jobs?token="+self.LOCAL_TOKEN+"&TOKEN=another-secret&limit=3",headers=self.auth)
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.calls[0]["url"],"http://127.0.0.1:8766/api/jobs?limit=3")

    def test_proxy_substitutes_gpu_bearer_and_drops_untrusted_headers(self):
        response = self.client.post("/api/jobs?limit=3",headers={**self.auth,"Cookie":"local=private",
            "Proxy-Authorization":"Basic neverforward","X-Forwarded-Host":"evil.example","X-Forwarded-For":"8.8.8.8"},
            json={"source":"sample.mp4"})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(len(self.calls),1)
        call = self.calls[0]
        self.assertEqual(call["url"],"http://127.0.0.1:8766/api/jobs?limit=3")
        self.assertEqual(call["headers"]["authorization"],"Bearer "+self.GPU_TOKEN)
        self.assertNotIn(self.LOCAL_TOKEN,json.dumps(call["headers"]))
        for header in ("cookie","proxy-authorization","x-forwarded-host","x-forwarded-for","origin"):
            self.assertNotIn(header,call["headers"])
        self.assertEqual(json.loads(call["body"]),{"source":"sample.mp4"})
        for header in ("access-control-allow-origin","set-cookie","x-upstream-private","location"):
            self.assertNotIn(header,response.headers)

    def test_proxy_rejects_absolute_dot_encoded_and_backslash_paths(self):
        for path in ("/api//evil.example/path","/api/https://evil.example/path","/api/jobs/%2e%2e/status",
                     "/api/jobs/%252e%252e/status","/api/jobs/%5cstatus","/api/jobs/%00status"):
            with self.subTest(path=path):
                self.assert_rejected_without_upstream("GET",path,headers=self.auth)

    def test_upstream_redirect_is_not_followed_or_returned_as_open_redirect(self):
        self.response_mode = "redirect"
        response = self.client.get("/api/jobs",headers=self.auth)
        self.assertEqual(response.status_code,502,response.text)
        self.assertEqual(len(self.calls),1)
        self.assertNotIn("location",response.headers)
        self.assertNotIn("set-cookie",response.headers)
        self.assertNotIn("evil.example",response.text)

    def test_upstream_errors_do_not_reveal_gpu_token(self):
        self.response_mode = "timeout"
        response = self.client.get("/api/jobs",headers=self.auth)
        self.assertIn(response.status_code,(502,503,504))
        self.assertNotIn(self.GPU_TOKEN,response.text)
        self.assertNotIn(self.LOCAL_TOKEN,response.text)

    def test_upstream_server_error_body_is_not_exposed(self):
        self.response_mode = "server_error"
        response = self.client.get("/api/jobs",headers=self.auth)
        self.assertIn(response.status_code,(502,503))
        self.assertNotIn(self.GPU_TOKEN,response.text)
        self.assertNotIn("private traceback",response.text)
        self.assertTrue(self.streams[-1].closed)
        self.assertEqual(len(self.calls),1)

    def test_unsupported_method_and_oversized_body_do_not_reach_gpu(self):
        self.assert_rejected_without_upstream("DELETE","/api/jobs",headers=self.auth)
        self.assert_rejected_without_upstream("POST","/api/jobs",headers=self.auth,content=b"x"*65537)

    def test_video_range_response_is_preserved(self):
        self.response_mode = "range"
        response = self.client.get("/api/jobs/job1/video",headers={**self.auth,"Range":"bytes=0-3"})
        self.assertEqual(response.status_code,206,response.text)
        self.assertEqual(response.content,b"clip")
        self.assertEqual(response.headers["content-range"],"bytes 0-3/10")
        self.assertEqual(self.calls[0]["headers"]["range"],"bytes=0-3")
        self.assertTrue(self.streams[-1].closed)

    def test_safe_get_recovers_once_from_each_transient_transport_class(self):
        for kind in (httpx.ConnectError,httpx.ConnectTimeout,httpx.ReadError,httpx.ReadTimeout,httpx.RemoteProtocolError):
            with self.subTest(error=kind.__name__):
                self.calls.clear()
                self.failures = [kind("must not leak "+self.GPU_TOKEN)]
                with self.assertLogs('cutflow.local_gateway',level='WARNING') as logs:
                    response = self.client.get('/api/jobs/job1',headers=self.auth)
                self.assertEqual(response.status_code,200,response.text)
                self.assertEqual(len(self.calls),2)
                self.assertIn('retrying=True',' '.join(logs.output))
                self.assertNotIn(self.GPU_TOKEN,' '.join(logs.output))

    def test_get_retry_stops_after_two_attempts_with_specific_error_code(self):
        for kind,code,status in ((httpx.ConnectError,'upstream_unavailable',503),
                                 (httpx.ReadTimeout,'upstream_timeout',504),
                                 (httpx.RemoteProtocolError,'upstream_protocol_error',502)):
            with self.subTest(error=kind.__name__):
                self.calls.clear()
                self.failures = [kind('private') for _ in range(3)]
                with self.assertLogs('cutflow.local_gateway',level='WARNING'):
                    response = self.client.get('/api/jobs/job1',headers=self.auth)
                self.assertEqual(response.status_code,status)
                self.assertEqual(response.json()['code'],code)
                self.assertTrue(response.json()['retryable'])
                self.assertEqual(len(self.calls),2)
                self.assertEqual(len(self.failures),1)
                self.assertNotIn('GPU 暂未连接',response.text)

    def test_publish_and_patch_are_never_retried(self):
        for method,path in (('POST','/api/jobs/job1/publish'),('PATCH','/api/collections/tasks/task1')):
            for kind in (httpx.ConnectError,httpx.ReadTimeout,httpx.RemoteProtocolError):
                with self.subTest(method=method,error=kind.__name__):
                    self.calls.clear()
                    self.failures = [kind('private'),kind('must remain unconsumed')]
                    with self.assertLogs('cutflow.local_gateway',level='WARNING'):
                        response = self.client.request(method,path,headers=self.auth,json={})
                    self.assertIn(response.status_code,(502,503,504))
                    self.assertFalse(response.json()['retryable'])
                    self.assertEqual(len(self.calls),1)
                    self.assertEqual(len(self.failures),1)
                    self.assertIn('结果尚未确认',response.json()['detail'])

    def test_interrupted_json_get_body_retries_before_reply_and_closes_failed_stream(self):
        self.response_mode = 'body_error_once'
        with self.assertLogs('cutflow.local_gateway',level='WARNING'):
            response = self.client.get('/api/jobs/job1',headers=self.auth)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json(),{'ok':True})
        self.assertEqual(len(self.calls),2)
        self.assertTrue(all(stream.closed for stream in self.streams))

    def test_interrupted_write_body_is_not_replayed(self):
        self.response_mode = 'body_error'
        with self.assertLogs('cutflow.local_gateway',level='WARNING'):
            response = self.client.post('/api/jobs/job1/publish',headers=self.auth)
        self.assertEqual(response.status_code,503)
        self.assertFalse(response.json()['retryable'])
        self.assertEqual(len(self.calls),1)
        self.assertTrue(self.streams[0].closed)

    def test_safe_logs_include_route_shape_not_query_id_or_exception_text(self):
        self.failures = [httpx.RemoteProtocolError('private '+self.GPU_TOKEN)]*2
        with self.assertLogs('cutflow.local_gateway',level='WARNING') as logs:
            self.client.get('/api/jobs/sensitive-identifier?secret=query-private',headers=self.auth)
        text = ' '.join(logs.output)
        self.assertIn('error_class=RemoteProtocolError',text)
        self.assertIn('method=GET route=/api/jobs/{id}',text)
        for secret in (self.GPU_TOKEN,self.LOCAL_TOKEN,'sensitive-identifier','query-private','private '):
            self.assertNotIn(secret,text)

    def test_api_video_and_publish_have_distinct_read_budgets(self):
        self.client.get('/api/status',headers=self.auth)
        self.assertEqual(self.calls[-1]['timeout']['read'],8)
        self.response_mode='range'
        self.client.get('/api/jobs/job1/video',headers=self.auth)
        self.assertEqual(self.calls[-1]['timeout']['read'],300)
        self.response_mode='normal'
        previous=len(self.calls)
        self.client.post('/api/jobs/job1/publish',headers=self.auth)
        self.assertEqual(len(self.calls)-previous,1)
        self.assertEqual(self.calls[-1]['timeout']['read'],290)

    def test_metadata_attempt_deadline_bounds_connect_and_full_body_time(self):
        self.response_mode='stall'
        started=time.monotonic()
        with patch('studio.local_gateway.ATTEMPT_DEADLINE_SECONDS',0.01),self.assertLogs('cutflow.local_gateway',level='WARNING'):
            response=self.client.get('/api/jobs/job1',headers=self.auth)
        self.assertEqual(response.status_code,504)
        self.assertEqual(response.json()['code'],'upstream_timeout')
        self.assertEqual(len(self.calls),2)
        self.assertLess(time.monotonic()-started,1)

    def test_publish_keeps_its_long_single_attempt_deadline(self):
        self.response_mode='brief_stall'
        with patch('studio.local_gateway.ATTEMPT_DEADLINE_SECONDS',0.01):
            response=self.client.post('/api/jobs/job1/publish',headers=self.auth)
        self.assertEqual(response.status_code,200)
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.calls[0]['timeout']['read'],290)

    def test_partial_video_stream_is_never_replayed_and_raw_error_is_hidden(self):
        self.response_mode='video_body_error'
        with self.assertLogs('cutflow.local_gateway',level='WARNING') as logs:
            with self.assertRaisesRegex(RuntimeError,'GPU video stream interrupted') as raised:
                self.client.get('/api/jobs/job1/video',headers=self.auth)
        self.assertEqual(len(self.calls),1)
        self.assertTrue(self.streams[0].closed)
        self.assertNotIn('sensitive upstream',str(raised.exception))
        self.assertIn('stage=video_stream retrying=False',' '.join(logs.output))

    def test_only_portrait_import_has_eight_mib_body_limit_and_long_single_attempt(self):
        payload = b'image-bytes' * 7000
        self.assert_rejected_without_upstream('POST', '/api/ideas', headers=self.auth, content=payload)
        response = self.client.post('/api/presenters/import?filename=person.png', headers={**self.auth,
            'Content-Type': 'image/png'}, content=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.calls[-1]['body'], payload)
        self.assertEqual(self.calls[-1]['timeout']['read'], 60)
        self.assert_rejected_without_upstream('POST', '/api/presenters/import', headers=self.auth,
                                             content=b'x' * (8 * 1024 * 1024 + 1))
        self.calls.clear()
        self.failures = [httpx.ReadError('portrait result unknown')]
        with self.assertLogs('cutflow.local_gateway', level='WARNING'):
            response = self.client.post('/api/presenters/import', headers=self.auth, content=b'image')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(response.json()['retryable'])

    def test_media_cookie_allows_only_exact_portrait_image_get(self):
        self.client.get('/local-session', headers=self.session_headers)
        response = self.client.get('/api/presenters/1234/image', headers={'Sec-Fetch-Site': 'same-origin'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.calls[-1]['headers']['authorization'], 'Bearer ' + self.GPU_TOKEN)
        for method, path in [('GET', '/api/presenters'), ('POST', '/api/presenters/import'),
                             ('POST', '/api/presenters/1234/validate'), ('GET', '/api/presenters/1234/image/extra')]:
            self.assert_rejected_without_upstream(method, path, headers={'Sec-Fetch-Site': 'same-origin'})
        self.assert_rejected_without_upstream('GET', '/api/presenters/1234/image',
                                             headers={'Sec-Fetch-Site': 'cross-site'})

    def test_portrait_validation_keeps_single_write_attempt(self):
        self.failures = [httpx.ReadTimeout('unconfirmed validation')]
        with self.assertLogs('cutflow.local_gateway', level='WARNING'):
            response = self.client.post('/api/presenters/1234/validate', headers=self.auth)
        self.assertEqual(response.status_code, 504)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]['timeout']['read'], 60)

    def test_static_site_and_health_never_contact_gpu_or_expose_files(self):
        self.assertIn("Local studio",self.client.get("/").text)
        health = self.client.get("/api/health")
        self.assertEqual(health.status_code,200)
        self.assertNotIn(self.GPU_TOKEN,health.text)
        self.assertEqual(self.calls,[])
        for path in ("/%2e%2e/outside.txt","/%252e%252e/outside.txt"):
            response = self.client.get(path)
            self.assertNotIn("OUTSIDE-PRIVATE-SENTINEL",response.text)


if __name__ == "__main__":
    unittest.main()
