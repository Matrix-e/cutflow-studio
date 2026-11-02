"""Configure official Douyin search credentials without performing searches.

Default destination is the GPU. --local explicitly chooses this workstation.
Official scope: aweme.dy.video_search. Normal account cookies are not accepted
as an alternative authentication mechanism by the collection adapter.
"""
from __future__ import annotations

import argparse
import csv
import getpass
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import uuid
import warnings

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.start_local import environment
from studio.sync import SFTPTransport

REMOTE_DATA = "/root/autodl-tmp/cutflow-studio/data"
KEYS = frozenset({"CUTFLOW_DOUYIN_CLIENT_KEY", "CUTFLOW_DOUYIN_CLIENT_SECRET", "CUTFLOW_DOUYIN_DEVICE_ID"})
MAX_INT64 = 9223372036854775807


def validate_config(client_key: str, client_secret: str, device_id: str) -> dict[str, str]:
    values = []
    for label, raw in (("Client Key", client_key), ("Client Secret", client_secret)):
        value = raw.strip() if isinstance(raw, str) else ""
        if not value or len(value) > 512 or not re.fullmatch(r"[!-~]+", value):
            raise ValueError(f"{label} 格式无效，请填写开放平台的应用凭据。")
        values.append(value)
    device = device_id.strip() if isinstance(device_id, str) else ""
    if not re.fullmatch(r"[0-9]{1,19}", device) or not 1 <= int(device) <= MAX_INT64:
        raise ValueError("Device ID 必须是 1 至 9223372036854775807 的十进制整数。")
    return {"CUTFLOW_DOUYIN_CLIENT_KEY": values[0], "CUTFLOW_DOUYIN_CLIENT_SECRET": values[1],
            "CUTFLOW_DOUYIN_DEVICE_ID": str(int(device))}


def _payload(config: dict[str, str]) -> bytes:
    if set(config) != KEYS:
        raise ValueError("只允许配置三个抖音采集字段。")
    checked = validate_config(config["CUTFLOW_DOUYIN_CLIENT_KEY"], config["CUTFLOW_DOUYIN_CLIENT_SECRET"], config["CUTFLOW_DOUYIN_DEVICE_ID"])
    return json.dumps(checked, ensure_ascii=True).encode("utf-8")


def upload_config(sftp, config: dict[str, str], remote_data: str = REMOTE_DATA) -> None:
    payload = _payload(config)
    folder = PurePosixPath(remote_data)
    if (not folder.is_absolute() or ".." in folder.parts or "\\" in remote_data
            or any(ord(char) < 32 for char in remote_data)):
        raise ValueError("GPU 配置目录无效。")
    target = str(folder / "douyin-config.json")
    temporary = target + "." + uuid.uuid4().hex + ".part"
    try:
        with sftp.open(temporary, "wx") as stream:
            sftp.chmod(temporary, 0o600)
            stream.write(payload)
            stream.flush()
        with sftp.open(temporary, "rb") as stream:
            returned = stream.read(len(payload) + 1)
        if hashlib.sha256(returned).digest() != hashlib.sha256(payload).digest():
            raise RuntimeError("GPU 配置传输校验未通过。")
        sftp.posix_rename(temporary, target)
        if sftp.stat(target).st_mode & 0o777 != 0o600:
            raise RuntimeError("GPU 配置文件权限校验未通过。")
    finally:
        try:
            sftp.remove(temporary)
        except Exception:
            pass


def _restrict_local_file(path: Path) -> None:
    if os.name != "nt":
        os.chmod(path, 0o600)
        return
    # os.open(mode=0600) does not restrict Windows ACLs. Protect this new empty
    # file before writing secret bytes; never change permissions on its parent.
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    identity = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True,
                              text=True, check=True, creationflags=flags, timeout=10)
    rows = list(csv.reader(io.StringIO(identity.stdout.strip())))
    sid = rows[0][-1] if len(rows) == 1 and rows[0] else ""
    if not re.fullmatch(r"S-[0-9]+(?:-[0-9]+)+", sid):
        raise RuntimeError("无法核实本机配置文件所有者。")
    subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"*{sid}:(F)"],
                   capture_output=True, check=True, creationflags=flags, timeout=10)


def save_local(config: dict[str, str], data_dir: Path) -> Path:
    payload = _payload(config)
    folder = data_dir.resolve()
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "douyin-config.json"
    temporary = folder / (".douyin-config." + uuid.uuid4().hex + ".part")
    descriptor = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        _restrict_local_file(temporary)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if hashlib.sha256(temporary.read_bytes()).digest() != hashlib.sha256(payload).digest():
            raise RuntimeError("本机配置写入校验未通过。")
        os.replace(temporary, target)
        return target
    finally:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="配置抖音开放平台视频搜索；默认写入 GPU，不执行搜索。")
    parser.add_argument("--local", action="store_true", help="只配置本机 data/douyin-config.json")
    args = parser.parse_args(argv)
    transport = None
    try:
        env = environment()
        data = Path(env.get("CUTFLOW_DATA", str(ROOT / "data")))
        if not args.local:
            key = env.get("CUTFLOW_SSH_KEY") or str(data / "gpu_sync_ed25519")
            known = env.get("CUTFLOW_SSH_KNOWN_HOSTS") or str(data / "gpu_known_hosts")
            if not Path(key).is_file() or not Path(known).is_file():
                print("未找到 GPU 专用密钥或已核验的主机文件，请先完成 GPU 连接配置。")
                return 1
        print("需要抖音开放平台应用，并已开通 aweme.dy.video_search 视频搜索权限。")
        print("请填写应用 Client Key、Client Secret 和设备 ID，不要填写普通抖音账号 Cookie。")
        print("本工具仅保存配置，不会发起搜索、采集、下载或发布。")
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            config = validate_config(getpass.getpass("Client Key（输入隐藏）："),
                                     getpass.getpass("Client Secret（输入隐藏）："),
                                     getpass.getpass("Device ID（正整数，输入隐藏）："))
        if args.local:
            save_local(config, data)
            print("本机抖音采集配置已保存。未连接抖音或 GPU。")
        else:
            transport = SFTPTransport(env.get("CUTFLOW_SSH_HOST", "connect.westb.seetacloud.com"),
                                      env.get("CUTFLOW_SSH_USER", "root"), key, known,
                                      int(env.get("CUTFLOW_SSH_PORT", "10893")))
            upload_config(transport.sftp, config)
            print("GPU 抖音采集配置已安全保存，刷新工作台后查看接入状态。")
            print("凭据只写入 GPU 运行目录；没有本地副本，也没有执行任何搜索。")
        return 0
    except (KeyboardInterrupt, EOFError):
        print("已取消配置，未执行任何搜索。")
        return 1
    except getpass.GetPassWarning:
        print("当前环境无法隐藏输入；请双击“配置抖音采集.cmd”在本机终端操作。")
        return 1
    except ValueError:
        print("配置格式无效。请核对应用凭据，以及 1 至 9223372036854775807 的设备 ID。")
        return 1
    except Exception:
        print("配置未完成。请检查 SSH 连接、已核验主机文件和配置目录权限；凭据未输出到日志。")
        return 1
    finally:
        if transport:
            try:
                transport.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
