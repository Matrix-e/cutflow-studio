"""Hidden local Webhook input -> verified SFTP runtime config; no test sends."""
from __future__ import annotations

import getpass
import hashlib
import json
from pathlib import Path, PurePosixPath
import sys
import uuid
import warnings

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.start_local import environment
from studio.delivery import _webhook_key
from studio.sync import SFTPTransport

REMOTE_DATA = "/root/autodl-tmp/cutflow-studio/data"


def upload_config(sftp, webhook: str, remote_data: str = REMOTE_DATA) -> None:
    """Write only three allowed settings, protected before the first secret byte."""
    _webhook_key(webhook)
    folder = PurePosixPath(remote_data)
    if not folder.is_absolute() or ".." in folder.parts or "\\" in remote_data:
        raise ValueError("GPU 配置目录无效。")
    target = str(folder / "wecom-config.json")
    temporary = target + "." + uuid.uuid4().hex + ".part"
    payload = json.dumps({"CUTFLOW_WECOM_MODE": "webhook", "CUTFLOW_WECOM_WEBHOOK": webhook,
                          "CUTFLOW_PUBLISH_ENABLED": "1"}, ensure_ascii=False).encode("utf-8")
    try:
        with sftp.open(temporary, "wx") as stream:
            sftp.chmod(temporary, 0o600)
            stream.write(payload)
            stream.flush()
        with sftp.open(temporary, "rb") as stream:
            returned = stream.read(len(payload) + 1)
        if hashlib.sha256(returned).digest() != hashlib.sha256(payload).digest():
            raise RuntimeError("GPU 配置传输校验未通过。")
        # OpenSSH's atomic rename replaces an existing config without a gap.
        sftp.posix_rename(temporary, target)
        if sftp.stat(target).st_mode & 0o777 != 0o600:
            raise RuntimeError("GPU 配置文件权限校验未通过。")
    finally:
        try:
            sftp.remove(temporary)
        except Exception:
            pass


def main():
    transport = None
    try:
        env = environment()
        key_path = env.get("CUTFLOW_SSH_KEY") or str(ROOT / "data" / "gpu_sync_ed25519")
        known_hosts = env.get("CUTFLOW_SSH_KNOWN_HOSTS") or str(ROOT / "data" / "gpu_known_hosts")
        if not Path(key_path).is_file() or not Path(known_hosts).is_file():
            print("还未找到 GPU 专用密钥或已核验的主机文件，请先完成 GPU 连接配置。")
            return 1
        print("此配置只写入你的 GPU 运行目录；不会发送测试消息，也不会开启自动发布。")
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            webhook = getpass.getpass("粘贴企业微信内部群机器人 Webhook（输入不显示）：").strip()
        _webhook_key(webhook)
        transport = SFTPTransport(env.get("CUTFLOW_SSH_HOST", "connect.westb.seetacloud.com"),
                                  env.get("CUTFLOW_SSH_USER", "root"), key_path, known_hosts,
                                  int(env.get("CUTFLOW_SSH_PORT", "10893")))
        upload_config(transport.sftp, webhook)
        print("GPU 企业微信配置已安全保存，刷新 GPU 工作台即可读取。")
        print("请先试看并审核成片，再点击推送；尚未向群里发送任何消息。")
        return 0
    except (KeyboardInterrupt, EOFError):
        print("已取消，未发送任何消息。")
        return 1
    except getpass.GetPassWarning:
        print("当前窗口不支持隐藏输入；请双击“配置GPU企业微信.cmd”在本机终端中操作。")
        return 1
    except Exception:
        # Never print an exception that might contain a URL, key, or typed value.
        print("GPU 配置未完成。请检查官方机器人地址、SSH 连接及专用密钥配置；不会自动重发消息。")
        return 1
    finally:
        if transport:
            try:
                transport.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
