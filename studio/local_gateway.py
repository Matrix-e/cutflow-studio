"""Loopback desktop website; GPU credentials stay in this process.

The browser gets a short-lived gateway token through a same-origin bootstrap.
GPU API traffic goes through the existing pinned SSH tunnel, never a public port.
"""
from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
import hmac
import os
from pathlib import Path
import re
import secrets

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from . import __version__

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = 'http://127.0.0.1:8766'
PORT = 8767
MAX_REQUEST_BYTES = 65536
RESPONSE_HEADERS = {'content-type', 'content-length', 'content-disposition', 'accept-ranges', 'content-range'}


def create_app(*, gpu_token: str, local_token: str | None = None,
               upstream_transport=None, web_root: Path | None = None) -> FastAPI:
    if not isinstance(gpu_token, str) or not 24 <= len(gpu_token) <= 4096 or any(ord(c) < 33 for c in gpu_token):
        raise ValueError('GPU 访问配置无效，请重新连接 GPU 工作台。')
    desktop_token = local_token or secrets.token_urlsafe(32)
    client = httpx.AsyncClient(base_url=UPSTREAM, transport=upstream_transport, trust_env=False,
                              follow_redirects=False, timeout=httpx.Timeout(300, connect=5),
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
        try:
            outgoing = client.build_request(request.method, url, headers=headers, content=bytes(content))
            upstream = await client.send(outgoing, stream=True)
        except httpx.HTTPError:
            return JSONResponse({'detail': '本地页面已启动，GPU 暂未连接。请打开“打开本地工作台.cmd”重新连接。'}, status_code=503)
        if 300 <= upstream.status_code < 400:
            await upstream.aclose()
            return JSONResponse({'detail': 'GPU 返回了意外跳转，已停止此次请求。'}, status_code=502)
        if upstream.status_code >= 500:
            await upstream.aclose()
            return JSONResponse({'detail': 'GPU 服务暂时无法完成请求，请稍后重试；原有任务状态已保留。'}, status_code=502)
        # The backend never sets credentials in response headers; strip everything
        # except explicit media/JSON response metadata as a second boundary.
        headers_out = {k: v for k, v in upstream.headers.items() if k.lower() in RESPONSE_HEADERS}
        return StreamingResponse(upstream.aiter_raw(), status_code=upstream.status_code,
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
