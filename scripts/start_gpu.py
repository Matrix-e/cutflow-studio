"""Local GPU bridge: verified SSH tunnel + stable SFTP ingestion, no local render."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.start_local import environment


def acquire_lock(path):
    """Inspect size without reading another Windows process's locked byte."""
    stream = None
    try:
        stream = path.open('a+b')
        if os.fstat(stream.fileno()).st_size == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return stream
    except OSError:
        if stream is not None:
            stream.close()
        return None


def authenticated_gpu(client, base, headers, *, timeout=3):
    """Only a successful authenticated GPU response permits starting sync."""
    try:
        response = client.get(base + '/api/status', headers=headers, timeout=timeout)
        return response.status_code == 200 and response.json().get('execution_target') == 'remote_gpu'
    except Exception:
        return False


def wait_or_stop(stop_file, seconds):
    """Bounded sleeps keep the stop marker responsive during reconnect backoff."""
    deadline = time.monotonic() + seconds
    while not stop_file.exists():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.5, remaining))
    return True


def wait_for_gpu(client, base, headers, process, stop_file, *, timeout=30):
    deadline = time.monotonic() + timeout
    while not stop_file.exists():
        if process.poll() is not None:
            return 'tunnel_exited'
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return 'timeout'
        if authenticated_gpu(client, base, headers, timeout=min(3, remaining)):
            return 'ready'
        if wait_or_stop(stop_file, min(1, max(0, deadline - time.monotonic()))):
            break
    return 'stopped'


def stop_process(process):
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)

def main(argv=None):
    parser=argparse.ArgumentParser(description='连接远程 GPU 工作台并自动同步素材')
    parser.add_argument('--no-browser',action='store_true')
    args=parser.parse_args(argv)
    import httpx
    env=environment()
    data=Path(env['CUTFLOW_DATA'])
    key=Path(env.get('CUTFLOW_SSH_KEY',data/'gpu_sync_ed25519'))
    known=Path(env.get('CUTFLOW_SSH_KNOWN_HOSTS',data/'gpu_known_hosts'))
    token_path=data/'gpu-access-token.txt'
    if not all(p.is_file() for p in (key,known,token_path)):
        print('GPU 接入配置尚未完成：需要专用密钥、主机指纹与远端访问令牌。')
        return 1
    ssh=shutil.which('ssh')
    if not ssh:
        print('需要 Windows OpenSSH 客户端。')
        return 1
    token=token_path.read_text().strip()
    host=env.get('CUTFLOW_SSH_HOST','connect.westb.seetacloud.com')
    port=env.get('CUTFLOW_SSH_PORT','10893')
    user=env.get('CUTFLOW_SSH_USER','root')
    env['CUTFLOW_SSH_KEY']=str(key)
    env['CUTFLOW_SSH_KNOWN_HOSTS']=str(known)
    env['CUTFLOW_SSH_HOST']=host
    env['CUTFLOW_SSH_PORT']=port
    env['CUTFLOW_SSH_USER']=user
    base='http://127.0.0.1:8766'
    headers={'Authorization':'Bearer '+token}
    data.mkdir(parents=True,exist_ok=True)
    lock=acquire_lock(data/'gpu-client.lock')
    if lock is None:
        with httpx.Client(timeout=8,trust_env=False,follow_redirects=False) as client:
            ready=authenticated_gpu(client,base,headers,timeout=8)
        if ready and not args.no_browser:
            webbrowser.open(base+'/#token='+token)
        print('GPU 连接器已经在运行，已复用。' if ready else
              'GPU 连接器已经在后台等待连接，未重复启动同步。')
        return 0
    commands={
        'tunnel':[ssh,'-N','-T','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',
                  '-o','UserKnownHostsFile='+str(known),'-o','ExitOnForwardFailure=yes',
                  '-o','ConnectTimeout=10',
                  '-o','ServerAliveInterval=30','-o','ServerAliveCountMax=3','-i',str(key),
                  '-p',port,'-L','127.0.0.1:8766:127.0.0.1:8765',user+'@'+host],
        'sync':[sys.executable,'-m','studio.sync','--source',env['CUTFLOW_INBOX']],
    }
    stop_file=data/'gpu-client.stop'
    stop_file.unlink(missing_ok=True)
    children,streams={},{}
    def launch(name):
        if name in streams:
            streams[name].close()
        streams[name]=(data/('gpu-'+name+'.log')).open('a',encoding='utf-8')
        children[name]=subprocess.Popen(commands[name],cwd=ROOT,env=env,stdin=subprocess.DEVNULL,
            stdout=streams[name],stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    connected, opened = False, False
    try:
        with httpx.Client(timeout=3,trust_env=False,follow_redirects=False) as client:
            while not stop_file.exists():
                (data/'gpu-client.json').write_text(json.dumps({'supervisor':os.getpid(),
                    **{name:p.pid for name,p in children.items()},'local_url':base,
                    'status':'connected' if connected else 'waiting_connection'}))
                tunnel = children.get('tunnel')
                if tunnel is None or tunnel.poll() is not None:
                    connected = False
                    try:
                        launch('tunnel')
                    except OSError:
                        print('SSH 暂未启动，连接器保留运行并稍后重试。',flush=True)
                        if wait_or_stop(stop_file,10):
                            break
                        continue
                    tunnel = children['tunnel']
                if not connected:
                    outcome = wait_for_gpu(client,base,headers,tunnel,stop_file)
                    if outcome == 'stopped' or stop_file.exists():
                        break
                    if outcome != 'ready':
                        stop_process(tunnel)
                        print('GPU 暂未连接，后台将继续重试；本机素材保持等待同步。',flush=True)
                        if wait_or_stop(stop_file,10):
                            break
                        continue
                    connected = True
                sync = children.get('sync')
                if sync is None or sync.poll() is not None:
                    # A stopped sync worker is never restarted against an
                    # unverified service, even when the tunnel process survives.
                    if not authenticated_gpu(client,base,headers):
                        connected = False
                        continue
                    if stop_file.exists():
                        break
                    try:
                        launch('sync')
                    except OSError:
                        print('同步组件暂未启动，后台将稍后重试。',flush=True)
                        if wait_or_stop(stop_file,10):
                            break
                        continue
                if not opened:
                    if not args.no_browser:
                        webbrowser.open(base+'/#token='+token)
                    print('GPU 工作台已连接：'+base,flush=True)
                    print('自动同步目录：'+env['CUTFLOW_INBOX'],flush=True)
                    print('此连接器只同步与转发；素材由远端 GPU 工作进程处理。',flush=True)
                    print('Ctrl+C 关闭本机连接。远端服务继续运行，不会关闭租用的 GPU 实例。',flush=True)
                    opened = True
                if wait_or_stop(stop_file,3):
                    break
        if stop_file.exists():
            stop_file.unlink(missing_ok=True)
            print('已收到停止请求，正在关闭本机同步与转发。',flush=True)
    except KeyboardInterrupt:
        print('正在停止本机同步与转发。')
    finally:
        for child in children.values():
            stop_process(child)
        for stream in streams.values():stream.close()
        lock.close()
    return 0

if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as exc:
        print('GPU 连接未完成：'+str(exc))
        raise SystemExit(1)
