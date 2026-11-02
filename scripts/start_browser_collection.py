"""User-invoked, visible Chrome collector with isolated profile and GPU bridge."""
from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.start_local import environment

API_URL = "http://127.0.0.1:8766"


def browser_launch(env: dict, *, once=False, login_only=False):
    data = Path(env.get("CUTFLOW_DATA", str(ROOT / "data"))).resolve()
    profile, output = data / "douyin-browser", data / "collected" / "douyin"
    inbox_value = env.get("CUTFLOW_INBOX")
    if inbox_value:
        inbox = Path(inbox_value).resolve()
        if output.is_relative_to(inbox) or profile.is_relative_to(inbox):
            raise ValueError("采集与登录目录不能位于自动剪辑素材投递箱内，请调整素材目录。")
    token_file = data / "gpu-access-token.txt"
    if not token_file.is_file():
        raise ValueError("缺少 GPU 访问配置，请先运行“连接GPU工作台.cmd”。")
    token = token_file.read_text(encoding="utf-8").strip()
    if len(token) < 24:
        raise ValueError("GPU 访问配置不完整，请先完成 GPU 连接配置。")
    command = [sys.executable, "-m", "studio.browser_collector", "--api-url", API_URL,
               "--profile-dir", str(profile), "--output-dir", str(output)]
    if once:
        command.append("--once")
    if login_only:
        command.append("--login-only")
    child_env = dict(env)
    child_env["CUTFLOW_BROWSER_API_URL"] = API_URL
    child_env["CUTFLOW_BROWSER_TOKEN"] = token
    child_env["PYTHONUTF8"] = "1"
    return command, child_env, profile, output


def gpu_ready(client, token: str) -> bool:
    try:
        response = client.get(API_URL + "/api/status", headers={"Authorization": "Bearer " + token})
        if response.status_code != 200:
            return False
        return response.json().get("execution_target") == "remote_gpu"
    except Exception:
        return False


def ensure_gpu(env: dict, token: str, *, wait_seconds: float = 45) -> bool:
    import httpx
    data = Path(env.get("CUTFLOW_DATA", str(ROOT / "data")))
    with httpx.Client(timeout=3, trust_env=False, follow_redirects=False) as client:
        if gpu_ready(client, token):
            return True
        data.mkdir(parents=True, exist_ok=True)
        # start_gpu.py owns a process lock; a competing launch reuses its bridge.
        with (data / "gpu-browser-connector.log").open("a", encoding="utf-8") as log:
            process = subprocess.Popen([sys.executable, str(ROOT / "scripts" / "start_gpu.py"), "--no-browser"],
                                       cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            if gpu_ready(client, token):
                return True
            if process.poll() is not None:
                return False
            time.sleep(1)
    return False


def _lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    stream.seek(0)
    if not stream.read(1):
        stream.write(b"0")
        stream.flush()
    stream.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return stream
    except OSError:
        stream.close()
        return None


def main(argv=None):
    parser = argparse.ArgumentParser(description="打开独立 Chrome 登录并采集抖音；不自动剪辑或发布。")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--once", action="store_true", help="处理一轮队列后退出")
    group.add_argument("--login-only", action="store_true", help="只打开独立浏览器供手动登录")
    args = parser.parse_args(argv)
    lock = None
    try:
        if importlib.util.find_spec("playwright") is None:
            print("缺少浏览器采集组件。请在本项目目录运行：python -m pip install -r requirements-browser.txt")
            print("安装后重新双击“启动抖音采集.cmd”；采集使用已安装的 Google Chrome。")
            return 1
        if importlib.util.find_spec("studio.browser_collector") is None:
            print("当前版本缺少浏览器采集模块，请先更新完整项目。")
            return 1
        env = environment()
        command, child_env, profile, output = browser_launch(env, once=args.once, login_only=args.login_only)
        data = Path(env.get("CUTFLOW_DATA", str(ROOT / "data"))).resolve()
        lock = _lock(data / "douyin-browser-client.lock")
        if lock is None:
            print("抖音采集窗口已在运行，请使用现有窗口，避免重复登录同一浏览器目录。")
            return 0
        if not ensure_gpu(env, child_env["CUTFLOW_BROWSER_TOKEN"]):
            print("GPU 工作台暂未就绪。连接器可能仍在启动，请查看 data/gpu-browser-connector.log 后重试。")
            return 1
        profile.mkdir(parents=True, exist_ok=True)
        output.mkdir(parents=True, exist_ok=True)
        print("即将打开独立的 Google Chrome；登录、短信验证或验证码请由你本人完成。")
        print("采集文件目录：" + str(output))
        print("该目录与自动剪辑投递箱分开；采集内容不会自动进入剪辑或发布。")
        print("保留本窗口运行。Ctrl+C 停止采集，GPU 同步连接器继续运行。")
        result = subprocess.run(command, cwd=ROOT, env=child_env, check=False)
        if result.returncode:
            print("采集进程已停止，请查看上方提示；未自动发送或发布内容。")
        return result.returncode
    except KeyboardInterrupt:
        print("已停止采集。GPU 同步连接器继续运行。")
        return 0
    except ValueError as error:
        # Only local configuration validations above produce ValueError messages.
        print(str(error))
        return 1
    except Exception:
        print("采集未启动。请检查 GPU 连接、Chrome 与浏览器组件；访问令牌不会显示在日志中。")
        return 1
    finally:
        if lock:
            lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
