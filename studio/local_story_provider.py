"""Optional authenticated loopback-only story generation; standard library only."""
from __future__ import annotations
import ipaddress
import http.client
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat
import urllib.error
import urllib.parse
import urllib.request

MAX_RESPONSE_BYTES = 1024 * 1024
TIMEOUT_SECONDS = 120


class LocalStoryProviderError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _failure(message):
    return LocalStoryProviderError(message)


def _endpoint(value):
    try:
        if not isinstance(value,str) or not value or any(c.isspace() or ord(c)<32 for c in value):raise ValueError()
        url=urllib.parse.urlsplit(value)
        if url.scheme!='http' or url.username is not None or url.password is not None or url.query or url.fragment or '?' in value or '#' in value:raise ValueError()
        host=url.hostname
        if not host:raise ValueError()
        if host.lower()!='localhost':
            if '%' in host or not ipaddress.ip_address(host).is_loopback:raise ValueError()
        if url.port is not None and not 1<=url.port<=65535:raise ValueError()
        if '\\' in value or '%' in url.path or any(p in ('.','..') for p in url.path.split('/')):raise ValueError()
        return urllib.parse.urlunsplit(('http',url.netloc,url.path.rstrip('/')+'/chat/completions','',''))
    except (ValueError,TypeError):
        raise _failure('本地故事服务地址无效，只能使用本机HTTP服务。') from None


def _read_key(root, name):
    try:
        if not isinstance(name,str) or not name or '\x00' in name or ':' in name:raise ValueError()
        relative=PurePosixPath(name.replace('\\','/'))
        if relative.is_absolute() or PureWindowsPath(name).drive or any(p in ('.','..') for p in relative.parts):raise ValueError()
        path=root.joinpath(*relative.parts)
        if not path.resolve().is_relative_to(root):raise ValueError()
        cursor=path
        while cursor!=root:
            if cursor.is_symlink():raise ValueError()
            cursor=cursor.parent
        if not stat.S_ISREG(path.stat().st_mode) or path.stat().st_size>4096:raise ValueError()
        with path.open('rb') as file:raw=file.read(4097)
        if len(raw)>4096:raise ValueError()
        key=raw.decode('utf-8').strip()
        if not key or any(c.isspace() or ord(c)<32 or ord(c)==127 for c in key):raise ValueError()
        return key
    except (OSError,ValueError,UnicodeError):
        raise _failure('本地故事服务凭据文件无效，请检查数据目录内的配置。') from None


def get_provider(data=None, *, reasoning_tokens=0):
    """Return callable(messages, max_tokens), or None only when unconfigured."""
    if type(reasoning_tokens) is not int or not 0 <= reasoning_tokens <= 2048:
        raise _failure('内容推理预算必须在允许范围内。')
    root=Path(data or os.environ.get('CUTFLOW_DATA') or Path(__file__).resolve().parents[1]/'data').resolve()
    path=root/'story-provider.json'
    if not path.exists() and not path.is_symlink():return None
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size>16384:raise ValueError()
        config=json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(config,dict) or set(config)!={'base_url','model','api_key_file'}:raise ValueError()
        model=config['model']
        if not isinstance(model,str) or not 1<=len(model.strip())<=200 or any(ord(c)<32 for c in model):raise ValueError()
    except (OSError,ValueError,UnicodeError):
        raise _failure('本地故事服务配置无效，请检查数据目录中的配置文件。') from None
    endpoint=_endpoint(config['base_url'])
    key=_read_key(root,config['api_key_file'])
    # Never use environment proxies or redirect a request bearing credentials.
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),_NoRedirect())

    def generate(messages,max_tokens, *, response_schema=None):
        try:
            if not isinstance(messages,list) or not messages or type(max_tokens) is not int or max_tokens<1:raise ValueError()
            payload={'model':model.strip(),'messages':messages,'max_tokens':max_tokens,'temperature':0,
                     'response_format':{'type':'json_object'},'chat_template_kwargs':{'enable_thinking':False}}
            if response_schema is not None:
                if not isinstance(response_schema,dict):raise ValueError()
                payload['response_format']={'type':'json_schema','json_schema':{'name':'cutflow_result','strict':True,'schema':response_schema}}
            if reasoning_tokens:
                # Supported by the verified local llama.cpp server. Reasoning is
                # bounded and its separate response field is never returned/logged.
                payload.update(max_tokens=max_tokens+reasoning_tokens,temperature=.6,top_p=.95,top_k=20,min_p=0,
                               reasoning_budget_tokens=reasoning_tokens,reasoning_format='deepseek',
                               chat_template_kwargs={'enable_thinking':True})
            raw=json.dumps(payload,ensure_ascii=False,allow_nan=False).encode('utf-8')
        except (TypeError,ValueError):
            raise _failure('故事生成请求格式无效。') from None
        request=urllib.request.Request(endpoint,data=raw,headers={'Content-Type':'application/json','Authorization':'Bearer '+key},method='POST')
        try:
            with opener.open(request,timeout=TIMEOUT_SECONDS) as response:
                if response.status!=200:raise ValueError()
                length=response.headers.get('Content-Length')
                if length is not None and int(length)>MAX_RESPONSE_BYTES:raise ValueError()
                body=response.read(MAX_RESPONSE_BYTES+1)
                if len(body)>MAX_RESPONSE_BYTES:raise ValueError()
            value=json.loads(body.decode('utf-8'))
            choice=value['choices'][0]
            content=choice['message']['content']
            if choice.get('finish_reason')!='stop' or not isinstance(content,str) or not content.strip():raise ValueError()
            return content
        except urllib.error.HTTPError as exc:
            exc.close()
            raise _failure('本地故事服务请求失败，请检查服务连接与访问配置。') from None
        except (urllib.error.URLError,http.client.HTTPException,OSError,ValueError,KeyError,IndexError,TypeError,UnicodeError):
            raise _failure('本地故事服务未返回完整有效的结果，请稍后重试。') from None
    generate.supports_response_schema = True
    return generate
