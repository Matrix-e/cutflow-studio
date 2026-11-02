"""Explicit, durable WeCom publication. Secrets stay in runtime configuration.

WeCom does not offer an idempotency key for robot messages. A durable local
ledger deliberately stops after an uncertain send instead of sending twice.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

API = "https://qyapi.weixin.qq.com/cgi-bin/"
FILE_SETTINGS = frozenset({"CUTFLOW_WECOM_MODE", "CUTFLOW_WECOM_WEBHOOK", "CUTFLOW_PUBLISH_ENABLED"})


class DeliveryError(RuntimeError):
    def __init__(self, code: str, message: str, *, uncertain: bool = False):
        super().__init__(message)
        self.code, self.uncertain = code, uncertain


def _setting(name: str) -> str:
    # Explicit process settings always win. The narrowly scoped file can be
    # atomically replaced over SFTP while the GPU service remains running.
    if name in os.environ:
        return os.environ[name].strip()
    if name not in FILE_SETTINGS:
        return ""
    root = Path(os.environ.get("CUTFLOW_DATA", "") or Path(__file__).resolve().parents[1] / "data")
    path = root / "wecom-config.json"
    try:
        if path.stat().st_size > 8192:
            return ""
        config = json.loads(path.read_text(encoding="utf-8"))
        value = config.get(name) if isinstance(config, dict) else None
        return value.strip() if isinstance(value, str) else ""
    except (OSError, ValueError):
        return ""


def configuration_status() -> dict:
    mode = _setting("CUTFLOW_WECOM_MODE") or "webhook"
    enabled = _setting("CUTFLOW_PUBLISH_ENABLED") == "1"
    required = (["CUTFLOW_WECOM_WEBHOOK"] if mode == "webhook" else
                ["CUTFLOW_WECOM_CORP_ID", "CUTFLOW_WECOM_SECRET", "CUTFLOW_WECOM_AGENT_ID", "CUTFLOW_WECOM_TO_USER"])
    missing = [key for key in required if not _setting(key)]
    return {"mode": mode, "enabled": enabled, "configured": not missing and mode in {"webhook", "app"}, "missing": missing}


def _webhook_key(value: str) -> str:
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        raise DeliveryError("invalid_destination", "企业微信地址格式无效。") from None
    query = parse_qs(parsed.query)
    keys = query.get("key", [])
    if (parsed.scheme != "https" or parsed.hostname != "qyapi.weixin.qq.com"
            or port not in (None, 443) or parsed.username or parsed.password
            or parsed.path != "/cgi-bin/webhook/send" or parsed.fragment
            or set(query) != {"key"} or len(keys) != 1 or not re.fullmatch(r"[A-Za-z0-9_-]{8,256}", keys[0])):
        raise DeliveryError("invalid_destination", "企业微信地址必须为官方 HTTPS 消息推送地址。")
    return keys[0]


def _db() -> sqlite3.Connection:
    root = Path(_setting("CUTFLOW_DATA") or Path(__file__).resolve().parents[1] / "data")
    root.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(root / "delivery.sqlite3", timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""CREATE TABLE IF NOT EXISTS deliveries (
        job_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, status TEXT NOT NULL,
        result TEXT, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    con.execute("CREATE TABLE IF NOT EXISTS send_attempts(target_hash TEXT, attempted_at REAL)")
    return con


def _file_hash(source: Path) -> str:
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _begin_send(con: sqlite3.Connection, job_id: str, destination: str) -> None:
    # This is local-process-group coordination; other senders may share the same bot.
    target_hash = hashlib.sha256(destination.encode()).hexdigest()
    now = time.time()
    con.execute("BEGIN IMMEDIATE")
    count = con.execute("SELECT COUNT(*) FROM send_attempts WHERE target_hash=? AND attempted_at>?", (target_hash, now - 60)).fetchone()[0]
    if count >= 20:
        con.rollback()
        raise DeliveryError("rate_limited", "该群一分钟内已提交 20 次发送，请稍后重试。")
    con.execute("INSERT INTO send_attempts VALUES(?,?)", (target_hash, now))
    con.execute("UPDATE deliveries SET status='sending', updated_at=CURRENT_TIMESTAMP WHERE job_id=?", (job_id,))
    con.commit()


def _set(con: sqlite3.Connection, job_id: str, status: str, result: dict | None = None) -> None:
    con.execute("UPDATE deliveries SET status=?, result=?, updated_at=CURRENT_TIMESTAMP WHERE job_id=?",
                (status, json.dumps(result or {}, ensure_ascii=False), job_id))
    con.commit()


def _response(response: httpx.Response, *, sending: bool = False) -> dict:
    # Never include response URLs or provider error strings: either could echo a secret.
    if response.status_code != 200:
        raise DeliveryError("http_error", f"企业微信返回 HTTP {response.status_code}。", uncertain=sending)
    try:
        body = response.json()
    except (ValueError, TypeError):
        raise DeliveryError("invalid_response", "企业微信返回了无法解析的响应。", uncertain=sending) from None
    if not isinstance(body, dict) or not isinstance(body.get("errcode"), int):
        raise DeliveryError("invalid_response", "企业微信响应缺少结果码。", uncertain=sending)
    if body["errcode"] != 0:
        raise DeliveryError("wecom_rejected", f"企业微信拒绝了请求，错误码 {body['errcode']}。")
    return body


def publish_video(path: str | Path, title: str, job_id: str, *, approved: bool = False) -> dict:
    """Publish one reviewed MP4; call again safely only for confirmed successes.

    Caller must pass approved=True after its durable review gate. This function
    does not bypass that gate, and never enables publication by itself.
    """
    if not approved:
        raise DeliveryError("review_required", "成片尚未审核通过。")
    if not job_id or len(job_id) > 200:
        raise DeliveryError("invalid_job", "缺少有效任务编号。")
    config = configuration_status()
    if not config["enabled"]:
        raise DeliveryError("disabled", "企业微信发布尚未开启。")
    if not config["configured"]:
        raise DeliveryError("not_configured", "企业微信发布配置不完整。")
    source = Path(path).resolve(strict=True)
    if not source.is_file() or source.suffix.lower() != ".mp4":
        raise DeliveryError("invalid_file", "只支持发布 MP4 成片。")
    mode = config["mode"]
    maximum = 20_000_000 if mode == "webhook" else 10_000_000
    size = source.stat().st_size
    if size <= 5 or size > maximum:
        raise DeliveryError("file_size", f"成片须大于 5 字节且不超过 {maximum // 1_000_000} MB；请导出发布版。")
    if mode == "webhook":
        key = _webhook_key(_setting("CUTFLOW_WECOM_WEBHOOK"))
        destination = f"webhook:{key}"
    else:
        if not _setting("CUTFLOW_WECOM_AGENT_ID").isdigit():
            raise DeliveryError("invalid_agent", "企业微信应用编号应为整数。")
        if _setting("CUTFLOW_WECOM_TO_USER") == "@all":
            raise DeliveryError("invalid_recipient", "请配置明确的员工 UserID，不支持默认全员群发。")
        destination = "app:" + ":".join(_setting(k) for k in ["CUTFLOW_WECOM_CORP_ID", "CUTFLOW_WECOM_AGENT_ID", "CUTFLOW_WECOM_TO_USER"])
    file_hash = _file_hash(source)
    fingerprint = hashlib.sha256((file_hash + destination).encode()).hexdigest()
    con = _db()
    try:
        con.execute("BEGIN IMMEDIATE")
        previous = con.execute("SELECT * FROM deliveries WHERE job_id=?", (job_id,)).fetchone()
        if previous:
            if previous["fingerprint"] != fingerprint:
                raise DeliveryError("job_changed", "任务文件或发布目标已改变，请创建新的审核任务。")
            if previous["status"] == "published":
                con.rollback()
                return {**json.loads(previous["result"]), "idempotent": True}
            if previous["status"] in {"preparing", "sending", "unknown"}:
                raise DeliveryError("uncertain_previous", "上次发布未确认结果，请在企业微信核对后处理，系统不会自动重发。", uncertain=True)
            con.execute("UPDATE deliveries SET status='preparing' WHERE job_id=?", (job_id,))
        else:
            con.execute("INSERT INTO deliveries(job_id,fingerprint,status) VALUES(?,?,'preparing')", (job_id, fingerprint))
        con.commit()
        sending = False
        try:
            with httpx.Client(timeout=httpx.Timeout(60, connect=10), follow_redirects=False) as client:
                if mode == "webhook":
                    upload_url, upload_params = API + "webhook/upload_media", {"key": key, "type": "file"}
                    send_url, send_params = API + "webhook/send", {"key": key}
                else:
                    token_body = _response(client.get(API + "gettoken", params={"corpid": _setting("CUTFLOW_WECOM_CORP_ID"), "corpsecret": _setting("CUTFLOW_WECOM_SECRET")}))
                    access_token = token_body.get("access_token")
                    if not access_token:
                        raise DeliveryError("invalid_response", "企业微信未返回访问凭证。")
                    upload_url, upload_params = API + "media/upload", {"access_token": access_token, "type": "video"}
                    send_url, send_params = API + "message/send", {"access_token": access_token}
                name = re.sub(r'[\x00-\x1f"\\/]', "_", title).strip()[:80] or source.stem
                filename = name if name.lower().endswith(".mp4") else name + ".mp4"
                with source.open("rb") as stream:
                    uploaded = _response(client.post(upload_url, params=upload_params, files={"media": (filename, stream, "video/mp4", {"Content-Disposition": f'form-data; name="media"; filename="{filename}"; filelength={size}'})}))
                media_id = uploaded.get("media_id")
                if not media_id:
                    raise DeliveryError("invalid_response", "企业微信未返回素材编号。")
                if _file_hash(source) != file_hash:
                    raise DeliveryError("file_changed", "成片在上传期间发生变化，消息尚未发送。")
                if mode == "webhook":
                    payload = {"msgtype": "file", "file": {"media_id": media_id}}
                else:
                    payload = {"touser": _setting("CUTFLOW_WECOM_TO_USER"), "agentid": int(_setting("CUTFLOW_WECOM_AGENT_ID")),
                               "msgtype": "video", "video": {"media_id": media_id, "title": title[:128], "description": "审核通过的成片"},
                               "enable_duplicate_check": 1, "duplicate_check_interval": 1800}
                _begin_send(con, job_id, destination)
                sending = True
                sent = _response(client.post(send_url, params=send_params, json=payload), sending=True)
                partial = any(sent.get(k) for k in ("invaliduser", "invalidparty", "invalidtag", "unlicenseduser"))
                if partial:
                    raise DeliveryError("partial_delivery", "企业微信返回部分接收人无效；请核对接收人，系统不会自动重发。", uncertain=True)
                result = {"status": "published", "mode": mode, "job_id": job_id, "file_sha256": file_hash,
                          "bytes": size, "message_id": sent.get("msgid"), "idempotent": False}
                _set(con, job_id, "published", result)
                return result
        except DeliveryError as exc:
            _set(con, job_id, "unknown" if exc.uncertain else "failed", {"code": exc.code})
            raise
        except Exception:
            _set(con, job_id, "unknown" if sending else "failed", {"code": "network_or_local_error"})
            raise DeliveryError("network_or_local_error", "发布过程发生异常。" + ("结果不确定，请核对企业微信；系统不会自动重发。" if sending else "尚未发送消息，可检查网络和配置后重试。"), uncertain=sending) from None
    finally:
        con.close()
