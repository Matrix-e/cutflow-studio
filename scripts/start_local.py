"""Start the local workstation and stable-file watcher; originals are read-only."""
from __future__ import annotations
import argparse
import getpass
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time
import webbrowser

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

ROOT = Path(__file__).resolve().parents[1]

def environment():
    env = os.environ.copy()
    for path in [ROOT / 'config' / 'local.json', ROOT / 'config' / 'private.json']:
        if path.is_file():
            for key, value in json.loads(path.read_text(encoding='utf-8-sig')).items():
                if key.startswith('CUTFLOW_') and key not in os.environ:
                    env[key] = str(value)
    env.setdefault('CUTFLOW_DATA', str(ROOT / 'data'))
    env.setdefault('CUTFLOW_INBOX', str(ROOT / '素材投递箱'))
    env['PYTHONUTF8'] = '1'
    return env

def access_token(env):
    if env.get('CUTFLOW_TOKEN'):
        return env['CUTFLOW_TOKEN']
    data = Path(env['CUTFLOW_DATA'])
    data.mkdir(parents=True, exist_ok=True)
    path = data / 'access-token.txt'
    if not path.exists():
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                stream.write(secrets.token_urlsafe(32))
        except FileExistsError:
            pass
    token = path.read_text(encoding='utf-8').strip()
    if len(token) < 24:
        raise ValueError('工作台令牌长度不足，请检查 data/access-token.txt。')
    return token

def main(argv=None):
    parser = argparse.ArgumentParser(description='商剪工坊本地启动器')
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--no-watch', action='store_true')
    parser.add_argument('--configure-wecom', action='store_true', help='在本机隐藏输入机器人地址，仅保存在当前运行内存')
    args = parser.parse_args(argv)
    try:
        import httpx
        import uvicorn  # noqa: F401
        import fastapi  # noqa: F401
    except ImportError:
        print('依赖缺失，请先运行 python -m pip install -r requirements.txt。')
        return 1
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        print('需要 FFmpeg 与 ffprobe；请安装后再启动。')
        return 1
    env = environment()
    token = env['CUTFLOW_TOKEN'] = access_token(env)
    if args.configure_wecom:
        webhook = getpass.getpass('粘贴企业微信内部群机器人 Webhook（输入不会显示）：').strip()
        sys.path.insert(0, str(ROOT))
        from studio.delivery import _webhook_key
        _webhook_key(webhook)
        env['CUTFLOW_WECOM_WEBHOOK'] = webhook
        env['CUTFLOW_WECOM_MODE'] = 'webhook'
        env['CUTFLOW_PUBLISH_ENABLED'] = '1'
        print('机器人配置仅用于本次运行；不会写入源代码或上传到站点。')
    data = Path(env['CUTFLOW_DATA'])
    data.mkdir(parents=True, exist_ok=True)
    Path(env['CUTFLOW_INBOX']).mkdir(parents=True, exist_ok=True)
    base = 'http://127.0.0.1:8765'
    env['CUTFLOW_API_URL'] = base
    # Never expose the API over a public interface. The remote worker uses SSH forwarding.
    with httpx.Client(timeout=2, trust_env=False) as client:
        try:
            health = client.get(base + '/api/health')
        except httpx.RequestError:
            health = None
        if health is not None:
            try:
                owned = health.json().get('service') == 'cutflow-studio'
            except ValueError:
                owned = False
            if owned and client.get(base + '/api/status', headers={'Authorization':'Bearer '+token}).status_code == 200:
                config = client.get(base + '/api/config', headers={'Authorization':'Bearer '+token}).json()
                if Path(config.get('inbox', '')).resolve() != Path(env['CUTFLOW_INBOX']).resolve():
                    print('已有工作台的素材目录与当前设置不同。请先关闭原启动器，再启动使新设置生效。')
                    return 1
                if args.configure_wecom:
                    print('已有工作台运行。请先关闭原启动器，再用此选项重新启动，机器人配置才会生效。')
                    return 1
                if not args.no_browser:
                    webbrowser.open(base + '/#token=' + token)
                print('工作台已在运行，已复用现有服务。修改参数后请关闭原启动器再启动。')
                return 0
            print('8765 端口已被其他服务占用，未对它作任何修改。')
            return 1
    children, streams = [], []
    def launch(name, command):
        stream = (data / (name + '.log')).open('a', encoding='utf-8')
        streams.append(stream)
        child = subprocess.Popen(command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                 stdout=stream, stderr=subprocess.STDOUT,
                                 creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        children.append(child)
        return child
    try:
        server = launch('server', [sys.executable,'-m','uvicorn','studio.server:app','--host','127.0.0.1','--port','8765','--no-access-log'])
        with httpx.Client(timeout=2, trust_env=False) as client:
            for _ in range(40):
                if server.poll() is not None:
                    raise RuntimeError('剪辑服务启动失败，详情见 data/server.log。')
                try:
                    if client.get(base + '/api/health').status_code == 200:
                        break
                except httpx.RequestError:
                    pass
                time.sleep(.25)
            else:
                raise RuntimeError('剪辑服务启动超时。')
        if not args.no_watch:
            launch('watcher', [sys.executable,'-m','studio.watch','--settings',str(ROOT/'config'/'preset.json')])
        if env.get('CUTFLOW_SYNC_ENABLED') == '1':
            if all(env.get(k) for k in ['CUTFLOW_SSH_HOST','CUTFLOW_SSH_KEY','CUTFLOW_SSH_KNOWN_HOSTS']):
                launch('sync', [sys.executable,'-m','studio.sync','--source',env['CUTFLOW_INBOX']])
                print('GPU 同步进程已启动，连接结果以 data/sync.log 为准。', flush=True)
            else:
                print('GPU 同步暂未启用：还需配置 SSH 密钥与已核验的主机指纹文件。', flush=True)
        if not args.no_browser:
            webbrowser.open(base + '/#token=' + token)
        print('商剪工坊已启动：' + base, flush=True)
        print('素材目录：' + env['CUTFLOW_INBOX'], flush=True)
        print('当前使用本地剪辑服务；GPU 需另行部署与连接。', flush=True)
        print('保留此启动器运行。按 Ctrl+C 可停止本次启动的服务。', flush=True)
        while True:
            if server.poll() is not None:
                raise RuntimeError('剪辑服务意外退出，请查看 data/server.log。')
            for child in children[1:]:
                if child.poll() is not None:
                    raise RuntimeError('后台处理进程退出，请查看 data 中对应日志。')
            time.sleep(2)
    except KeyboardInterrupt:
        print('正在停止本次启动的服务。')
    finally:
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
        for child in reversed(children):
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
        for stream in streams:
            stream.close()
    return 0

if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print('启动未完成：' + str(exc))
        raise SystemExit(1)
