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
from start_local import environment

ROOT = Path(__file__).resolve().parents[1]

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
    lock=(data/'gpu-client.lock').open('a+b')
    lock.seek(0)
    if not lock.read(1):
        lock.write(b'0');lock.flush()
    lock.seek(0)
    try:
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:
        lock.close()
        with httpx.Client(timeout=4,trust_env=False) as client:
            response=client.get(base+'/api/status',headers=headers)
            response.raise_for_status()
        if not args.no_browser:
            webbrowser.open(base+'/#token='+token)
        print('GPU 连接器已经在运行，已复用。')
        return 0
    commands={
        'tunnel':[ssh,'-N','-T','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',
                  '-o','UserKnownHostsFile='+str(known),'-o','ExitOnForwardFailure=yes',
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
    try:
        launch('tunnel')
        with httpx.Client(timeout=3,trust_env=False) as client:
            for _ in range(30):
                if children['tunnel'].poll() is not None:
                    raise RuntimeError('SSH 转发未建立，请查看 data/gpu-tunnel.log。')
                try:
                    response=client.get(base+'/api/status',headers=headers)
                    if response.status_code==200:
                        break
                except httpx.RequestError:
                    pass
                time.sleep(1)
            else:
                raise RuntimeError('远端剪辑服务尚未响应，请检查 GPU 实例和服务。')
        launch('sync')
        if not args.no_browser:
            webbrowser.open(base+'/#token='+token)
        print('GPU 工作台已连接：'+base,flush=True)
        print('自动同步目录：'+env['CUTFLOW_INBOX'],flush=True)
        print('此连接器只同步与转发；素材由远端 GPU 工作进程处理。',flush=True)
        print('Ctrl+C 关闭本机连接。远端服务继续运行，不会关闭租用的 GPU 实例。',flush=True)
        while True:
            if stop_file.exists():
                stop_file.unlink(missing_ok=True)
                print('已收到停止请求，正在关闭本机同步与转发。',flush=True)
                break
            (data/'gpu-client.json').write_text(json.dumps({'supervisor':os.getpid(),
                **{name:p.pid for name,p in children.items()},'local_url':base}))
            for name,child in list(children.items()):
                if child.poll() is not None:
                    print(name+' 连接中断，将重新连接。',flush=True)
                    time.sleep(10)
                    launch(name)
            time.sleep(3)
    except KeyboardInterrupt:
        print('正在停止本机同步与转发。')
    finally:
        for child in children.values():
            if child.poll() is None:child.terminate()
        for child in children.values():
            try:child.wait(timeout=10)
            except subprocess.TimeoutExpired:child.kill()
        for stream in streams.values():stream.close()
        lock.close()
    return 0

if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as exc:
        print('GPU 连接未完成：'+str(exc))
        raise SystemExit(1)
