"""Open a local dashboard backed by the existing private GPU tunnel."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import time
import webbrowser

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.start_local import environment
from scripts.start_browser_collection import ensure_gpu

LOCAL_URL = "http://127.0.0.1:8767"


class DesktopError(RuntimeError):
    """Messages are authored locally and never contain tokens or HTTP bodies."""


def gateway_presence(client) -> str:
    """Public marker first: never disclose GPU credentials to an occupied port."""
    try:
        response = client.get(LOCAL_URL + "/api/health")
    except httpx.ConnectError:
        return "absent"
    except httpx.RequestError:
        return "foreign"
    try:
        body = response.json()
        owned = (response.status_code == 200 and isinstance(body, dict)
                 and body.get("service") == "cutflow-local-gateway"
                 and body.get("project_id") == "cutflow-studio")
    except (ValueError, TypeError):
        owned = False
    return "owned" if owned else "foreign"


def verify_gateway_session(client) -> bool:
    try:
        response = client.get(LOCAL_URL + "/local-session", headers={"X-CutFlow-Local": "1", "Origin": LOCAL_URL})
        body = response.json()
        if response.status_code != 200 or body.get("local_gateway") is not True or not isinstance(body.get("token"), str) or not body["token"]:
            raise DesktopError("本地工作台身份验证未完成，请检查当前版本。")
    except DesktopError:
        raise
    except Exception:
        raise DesktopError("本地工作台身份检查未完成，请稍后重试。") from None
    # A verified local gateway is useful for importing while the GPU is offline.
    # Only its own nonce goes to the loopback proxy; GPU secrets stay private.
    try:
        status = client.get(LOCAL_URL + "/api/status", headers={"Authorization": "Bearer " + body["token"]})
        return status.status_code == 200 and status.json().get("execution_target") == "remote_gpu"
    except Exception:
        return False


def spawn_gateway(env: dict, gpu_token: str):
    data = Path(env.get("CUTFLOW_DATA", str(ROOT / "data"))).resolve()
    data.mkdir(parents=True, exist_ok=True)
    child_env = dict(env)
    child_env.pop("CUTFLOW_TOKEN", None)
    child_env.pop("CUTFLOW_BROWSER_TOKEN", None)
    child_env["CUTFLOW_GPU_TOKEN"] = gpu_token
    child_env["PYTHONUTF8"] = "1"
    command = [sys.executable, "-m", "studio.local_gateway", "--host", "127.0.0.1", "--port", "8767"]
    with (data / "local-gateway.log").open("a", encoding="utf-8") as log:
        return subprocess.Popen(command, cwd=ROOT, env=child_env, stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
                                start_new_session=os.name != "nt")


def prepare_desktop(env: dict, *, startup_timeout: float = 20) -> dict:
    data = Path(env.get("CUTFLOW_DATA", str(ROOT / "data"))).resolve()
    token_path = data / "gpu-access-token.txt"
    if not token_path.is_file():
        raise DesktopError("缺少 GPU 连接配置，请先完成 GPU 专用连接设置。")
    gpu_token = token_path.read_text(encoding="utf-8").strip()
    if len(gpu_token) < 24:
        raise DesktopError("GPU 访问配置不完整，请重新检查连接设置。")
    with httpx.Client(timeout=8, trust_env=False, follow_redirects=False) as client:
        presence = gateway_presence(client)
        if presence == "foreign":
            raise DesktopError("8767 端口正由其他服务使用，未复用或修改该服务。")
        reused = presence == "owned"
        if not reused:
            process = spawn_gateway(env, gpu_token)
            deadline = time.monotonic() + startup_timeout
            while time.monotonic() < deadline:
                presence = gateway_presence(client)
                if presence == "owned":
                    break
                if presence == "foreign":
                    raise DesktopError("8767 端口已被其他服务占用，未复用该服务。")
                if process.poll() is not None:
                    raise DesktopError("本地工作台未启动，请检查 data/local-gateway.log。")
                time.sleep(0.25)
            else:
                raise DesktopError("本地工作台启动超时，请检查 data/local-gateway.log。")
        try:
            ensure_gpu(env, gpu_token, wait_seconds=6)
        except Exception:
            # Failure to start a GPU bridge must not prevent local intake.
            pass
        gpu_connected = verify_gateway_session(client)
    return {"url": LOCAL_URL + "/", "reused": reused, "gpu_connected": gpu_connected}


def main(argv=None):
    parser = argparse.ArgumentParser(description="打开本地工作台；网站在本机运行，剪辑仍由 GPU 完成。")
    parser.add_argument("--no-browser", action="store_true", help="启动服务并显示本地地址，不打开浏览器")
    args = parser.parse_args(argv)
    try:
        result = prepare_desktop(environment())
        if not args.no_browser:
            webbrowser.open(result["url"])
        print("本地工作台已就绪：" + result["url"])
        if result.get("gpu_connected") is False:
            print("GPU 暂未连接。可以先导入视频，文件会保存在本机，连接恢复后再同步处理。")
        print("连接信息会自动读取，无需手工填写；后台服务继续运行，可关闭此窗口。")
        return 0
    except DesktopError as error:
        print("暂未打开：" + str(error))
        return 1
    except KeyboardInterrupt:
        print("已取消打开工作台。")
        return 1
    except Exception:
        print("本地工作台启动未完成，请检查项目依赖和连接配置。访问令牌不会输出到日志。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
