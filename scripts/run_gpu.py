"""GPU-side supervisor. Does not expose ports or persist delivery credentials."""
from __future__ import annotations
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]

def main():
    base = Path(os.getenv('CUTFLOW_GPU_ROOT', '/root/autodl-tmp/cutflow-studio'))
    data = base / 'data'
    data.mkdir(parents=True, exist_ok=True)
    token = (data / 'access-token.txt').read_text().strip()
    if len(token) < 24:
        raise ValueError('GPU access token missing or too short')
    import fcntl
    lock = (data / 'supervisor.lock').open('a+')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    env = os.environ.copy()
    env.update({'CUTFLOW_TOKEN':token,'CUTFLOW_INBOX':str(base/'inbox'),
                'CUTFLOW_DATA':str(data),'CUTFLOW_API_URL':'http://127.0.0.1:8765',
                'CUTFLOW_EXECUTION_TARGET':'remote_gpu','PYTHONUTF8':'1'})
    preset = json.loads((ROOT/'config'/'preset.json').read_text())
    preset['encoder'] = 'nvenc'
    env['CUTFLOW_SETTINGS'] = json.dumps(preset)
    env.setdefault('CUTFLOW_AUTO_PUBLISH','0')
    idea_python = base/'idea-runtime/bin/python'
    idea_model = base/'models/Qwen3-4B-Instruct-2507'
    if idea_python.is_file() and idea_model.is_dir():
        env.setdefault('CUTFLOW_IDEA_PYTHON', str(idea_python))
        env.setdefault('CUTFLOW_IDEA_MODEL_DIR', str(idea_model))
    commands = {
        'server':[sys.executable,'-m','uvicorn','studio.server:app','--host','127.0.0.1','--port','8765','--no-access-log'],
        'watcher':[sys.executable,'-m','studio.watch'],
    }
    children, logs = {}, {}
    stopped = False
    def stop(signum, frame):
        nonlocal stopped
        stopped = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while not stopped:
            for name, command in commands.items():
                child = children.get(name)
                if child is None or child.poll() is not None:
                    if name in logs:
                        logs[name].close()
                    logs[name] = (data/(name+'.log')).open('a',encoding='utf-8')
                    children[name] = subprocess.Popen(command,cwd=ROOT,env=env,
                        stdin=subprocess.DEVNULL,stdout=logs[name],stderr=subprocess.STDOUT)
                    print(name+' started',flush=True)
            (data/'processes.json').write_text(json.dumps({'supervisor':os.getpid(),
                **{name:p.pid for name,p in children.items()},'release':str(ROOT)}))
            time.sleep(5)
    finally:
        for child in children.values():
            if child.poll() is None:
                child.terminate()
        for child in children.values():
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                child.kill()
        for stream in logs.values():
            stream.close()
        lock.close()

if __name__ == '__main__':
    main()
