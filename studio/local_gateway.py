"""Loopback desktop website; GPU credentials stay in this process.

The browser gets a short-lived gateway token through a same-origin bootstrap.
GPU API traffic goes through the existing pinned SSH tunnel, never a public port.
"""
from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
import hmac
import logging
import os
from pathlib import Path
import re
import secrets

import anyio
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from . import __version__

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = 'http://127.0.0.1:8766'
PORT = 8767
MAX_REQUEST_BYTES = 65536
RESPONSE_HEADERS = {'content-type', 'content-length', 'content-disposition', 'accept-ranges', 'content-range'}
LOGGER = logging.getLogger('cutflow.local_gateway')
API_TIMEOUT = httpx.Timeout(8, connect=3, pool=3)
VIDEO_TIMEOUT = httpx.Timeout(300, connect=5, pool=5)
PUBLISH_TIMEOUT = httpx.Timeout(290, connect=5, pool=5)
ATTEMPT_DEADLINE_SECONDS = 8
PUBLISH_DEADLINE_SECONDS = 290
RETRYABLE_READ_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadError,
                         httpx.ReadTimeout, httpx.RemoteProtocolError)


def _route_label(path: str) -> str:
    # Log route shape only: arbitrary identifiers, query values and credentials
    # never enter the diagnostic log, including attacker-chosen unknown paths.
    fixed = {'status', 'jobs', 'assets', 'config', 'approve', 'publish', 'retry', 'video',
             'collections', 'tasks', 'items', 'runs', 'import', 'browser', 'claim',
             'complete', 'fail', 'heartbeat'}
    return '/api/' + '/'.join(part if part in fixed else '{id}' for part in path.split('/'))


def _transport_failure(exc: httpx.HTTPError, method: str):
    if isinstance(exc, httpx.TimeoutException):
        code, status, detail = 'upstream_timeout', 504, 'GPU 响应超时，请稍后重新读取。'
    elif isinstance(exc, httpx.ProtocolError):
        code, status, detail = 'upstream_protocol_error', 502, '与 GPU 的通信中断，请重新读取当前任务。'
    else:
        code, status, detail = 'upstream_unavailable', 503, '暂时无法完成与 GPU 的通信，请稍后重新读取。'
    if method != 'GET':
        detail = '本次操作的结果尚未确认，请先查看任务记录；系统没有自动重复提交。'
    return code, status, detail


def create_app(*, gpu_token: str, local_token: str | None = None,
               upstream_transport=None, web_root: Path | None = None) -> FastAPI:
    if not isinstance(gpu_token, str) or not 24 <= len(gpu_token) <= 4096 or any(ord(c) < 33 for c in gpu_token):
        raise ValueError('GPU 访问配置无效，请重新连接 GPU 工作台。')
    desktop_token = local_token or secrets.token_urlsafe(32)
    client = httpx.AsyncClient(base_url=UPSTREAM, transport=upstream_transport, trust_env=False,
                              follow_redirects=False, timeout=API_TIMEOUT,
                              headers={'Accept-Encoding': 'identity'})

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            await client.aclose()

    app = FastAPI(title='商剪工坊 · 本地入口', docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.middleware('http')
    async def local_only(request: Request, call_next):
        host = request.headers.get('host', '').lower()
        origin = request.headers.get('origin')
        fetch_site = request.headers.get('sec-fetch-site')
        if (len(request.headers.getlist('host')) != 1
                or len(request.headers.getlist('origin')) > 1
                or len(request.headers.getlist('sec-fetch-site')) > 1
                or host not in {'127.0.0.1:8767', 'localhost:8767'}
                or origin is not None and origin != 'http://' + host
                or fetch_site is not None and fetch_site not in {'same-origin', 'none'}):
            response = JSONResponse({'detail': '仅允许从本机工作台访问。'}, status_code=403)
        elif request.method not in {'GET', 'HEAD', 'POST', 'PATCH'}:
            response = JSONResponse({'detail': '不支持此请求方式。'}, status_code=405)
        else:
            response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Frame-Options'] = 'DENY'
        return response

    @app.get('/api/health')
    def health():
        return {'ok': True, 'service': 'cutflow-local-gateway', 'project_id': 'cutflow-studio',
                'version': __version__, 'upstream': 'gpu_tunnel'}

    @app.get('/local-session')
    def session(request: Request):
        # Cross-origin JS cannot set this header without a denied CORS preflight.
        if request.headers.get('x-cutflow-local') != '1':
            return JSONResponse({'detail': '请从本地工作台页面打开。'}, status_code=403)
        return {'token': desktop_token, 'local_gateway': True}

    @app.api_route('/api/{path:path}', methods=['GET', 'POST', 'PATCH'])
    async def proxy(path: str, request: Request):
        supplied = request.headers.get('authorization', '')
        expected = 'Bearer ' + desktop_token
        if not hmac.compare_digest(supplied.encode(), expected.encode()):
            return JSONResponse({'detail': '本地连接已失效，请刷新页面自动重新连接。'}, status_code=401)
        if not re.fullmatch(r'[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*', path):
            return JSONResponse({'detail': '请求路径无效。'}, status_code=400)
        content = bytearray()
        async for chunk in request.stream():
            if len(content) + len(chunk) > MAX_REQUEST_BYTES:
                return JSONResponse({'detail': '请求内容过大。'}, status_code=413)
            content.extend(chunk)
        headers = {'Authorization': 'Bearer ' + gpu_token, 'Accept-Encoding': 'identity'}
        for name in ('content-type', 'range', 'if-range', 'idempotency-key'):
            if name in request.headers:
                headers[name] = request.headers[name]
        query = [(k, v) for k, v in request.query_params.multi_items() if k.lower() != 'token']
        url = httpx.URL(UPSTREAM).copy_with(path='/api/' + path, query=str(httpx.QueryParams(query)).encode())
        is_video = request.method == 'GET' and path.endswith('/video')
        is_publish = request.method == 'POST' and re.fullmatch(r'jobs/[A-Za-z0-9_-]+/publish', path) is not None
        timeout = PUBLISH_TIMEOUT if is_publish else VIDEO_TIMEOUT if is_video else API_TIMEOUT
        deadline = PUBLISH_DEADLINE_SECONDS if is_publish else ATTEMPT_DEADLINE_SECONDS
        attempts = 2 if request.method == 'GET' else 1
        upstream = None

        async def exchange():
            nonlocal upstream
            outgoing = client.build_request(request.method, url, headers=headers,
                                            content=bytes(content), timeout=timeout)
            upstream = await client.send(outgoing, stream=True)
            if not is_video and not (300 <= upstream.status_code < 400 or upstream.status_code >= 500):
                return await upstream.aread()
            return None

        for attempt in range(1, attempts + 1):
            upstream = None
            try:
                # The complete metadata exchange has one deadline, including
                # connect/pool/read; two safe attempts fit below the UI's 20s.
                # For video this deadline applies to response headers only;
                # publication retains one long attempt for media upload/send.
                with anyio.fail_after(deadline):
                    body = await exchange()
                if 300 <= upstream.status_code < 400:
                    await upstream.aclose()
                    return JSONResponse({'detail': 'GPU 返回了意外跳转，已停止此次请求。',
                                         'code': 'upstream_redirect', 'retryable': False}, status_code=502)
                if upstream.status_code >= 500:
                    LOGGER.warning('gateway_upstream_error code=upstream_server_error error_class=HTTP%d method=%s route=%s attempt=%d retrying=False',
                                   upstream.status_code, request.method, _route_label(path), attempt)
                    await upstream.aclose()
                    return JSONResponse({'detail': 'GPU 服务暂时无法完成请求，请查看任务状态后再操作。',
                                         'code': 'upstream_server_error', 'retryable': request.method == 'GET'}, status_code=502)
                # Buffer ordinary API replies before sending any browser headers.
                # A dropped JSON response can then be retried safely for GET only.
                # Video stays streaming and can never be replayed after bytes start.
                if not is_video:
                    headers_out = {k: v for k, v in upstream.headers.items() if k.lower() in RESPONSE_HEADERS}
                    # aread() decodes content encodings; let Response calculate the
                    # actual byte length even if an upstream ignored identity.
                    headers_out.pop('content-length', None)
                    await upstream.aclose()
                    return Response(body, status_code=upstream.status_code, headers=headers_out)
                break
            except (httpx.HTTPError, TimeoutError) as error:
                exc = error if isinstance(error, httpx.HTTPError) else httpx.ReadTimeout('Gateway request deadline exceeded')
                if upstream is not None:
                    await upstream.aclose()
                retrying = request.method == 'GET' and attempt < attempts and isinstance(exc, RETRYABLE_READ_ERRORS)
                code, status, detail = _transport_failure(exc, request.method)
                LOGGER.warning('gateway_upstream_error code=%s error_class=%s method=%s route=%s attempt=%d retrying=%s',
                               code, type(error).__name__, request.method, _route_label(path), attempt, retrying)
                if retrying:
                    continue
                return JSONResponse({'detail': detail, 'code': code, 'retryable': request.method == 'GET'}, status_code=status)
        # The backend never sets credentials in response headers; strip everything
        # except explicit media/JSON response metadata as a second boundary.
        headers_out = {k: v for k, v in upstream.headers.items() if k.lower() in RESPONSE_HEADERS}
        async def stream_video():
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            except httpx.HTTPError as exc:
                code, _, _ = _transport_failure(exc, request.method)
                LOGGER.warning('gateway_upstream_error code=%s error_class=%s method=GET route=%s stage=video_stream retrying=False',
                               code, type(exc).__name__, _route_label(path))
                # The browser may already have received bytes: do not restart a
                # partial response or falsely report successful completion.
                raise RuntimeError('GPU video stream interrupted') from None
            finally:
                await upstream.aclose()
        return StreamingResponse(stream_video(), status_code=upstream.status_code,
                                 headers=headers_out, background=BackgroundTask(upstream.aclose))

    static = web_root if web_root is not None else ROOT / 'web'
    app.mount('/', StaticFiles(directory=static, html=True), name='desktop')
    return app


def main():
    parser = argparse.ArgumentParser(description='本地网站入口，剪辑仍由远端 GPU 完成')
    parser.add_argument('--host', choices=['127.0.0.1'], default='127.0.0.1')
    parser.add_argument('--port', choices=[PORT], type=int, default=PORT)
    args = parser.parse_args()
    token = os.getenv('CUTFLOW_GPU_TOKEN', '').strip()
    if not token:
        data = Path(os.getenv('CUTFLOW_DATA', str(ROOT / 'data')))
        token = (data / 'gpu-access-token.txt').read_text(encoding='utf-8').strip()
    import uvicorn
    uvicorn.run(create_app(gpu_token=token), host=args.host, port=args.port, access_log=False, proxy_headers=False)


if __name__ == '__main__':
    main()
