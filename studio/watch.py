"""Stable folder ingestion and explicitly enabled automatic publication.

No original files are modified. Only watcher-owned jobs may be auto-published.
An ambiguous publish is terminal locally; a later scan never sends it again.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from urllib.parse import urlparse

import httpx

from .sync import candidate_files, stable_hash

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mts"}


def validated_url(value: str) -> str:
    parsed = urlparse(value)
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("API 地址不能包含登录信息、查询参数或片段")
    try:
        loopback = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = parsed.hostname.lower() == "localhost"
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise ValueError("API 只允许本机 HTTP 或明确配置的远程 HTTPS 地址")
    return value.rstrip("/")


class Watcher:
    def __init__(self, inbox: str | Path, data_dir: str | Path, api_url: str, token: str,
                 settings: dict | None = None, stable_seconds: float = 15,
                 auto_publish: bool = False, client=None):
        self.inbox = Path(inbox).resolve()
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.data = Path(data_dir).resolve()
        self.data.mkdir(parents=True, exist_ok=True)
        self.path = self.data / "watch.sqlite3"
        self.api_url = validated_url(api_url)
        if not token:
            raise ValueError("需要先配置本机访问口令 CUTFLOW_TOKEN")
        if stable_seconds < 15:
            raise ValueError("文件稳定等待时间不能少于 15 秒")
        self.stable_seconds = stable_seconds
        self.settings = dict(settings or {})
        self.settings_json = json.dumps(self.settings, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        self.auto_publish = bool(auto_publish)
        self.client = client or httpx.Client(timeout=httpx.Timeout(20, connect=5), follow_redirects=False)
        self.owns_client = client is None
        self.headers = {"Authorization": f"Bearer {token}"}
        self.primed: set[str] = set()
        self.hash_cache: dict[str, tuple] = {}
        with self.connection() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS observations (
                    source TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
                    stable_since REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS submissions (
                    request_key TEXT PRIMARY KEY, source TEXT NOT NULL, sha256 TEXT NOT NULL,
                    settings TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'waiting', job_id TEXT,
                    next_retry REAL NOT NULL DEFAULT 0, error TEXT,
                    publish_state TEXT NOT NULL DEFAULT 'none', updated_at REAL NOT NULL,
                    owned_by_request INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS diagnostic_sources (
                    api_url TEXT NOT NULL,source TEXT NOT NULL,registered_at REAL NOT NULL,
                    PRIMARY KEY(api_url,source)
                );
            """)
            columns = {row["name"] for row in con.execute("PRAGMA table_info(submissions)")}
            if "owned_by_request" not in columns:
                # Old rows have no ownership proof and must never auto-publish.
                con.execute("ALTER TABLE submissions ADD COLUMN owned_by_request INTEGER NOT NULL DEFAULT 0")

    @contextmanager
    def connection(self):
        con = sqlite3.connect(self.path, timeout=15)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def close(self):
        if self.owns_client:
            self.client.close()

    def _request(self, method, route, **kwargs):
        return self.client.request(method, self.api_url + route, headers=self.headers, **kwargs)

    def _mark(self, key, state, error=None):
        with self.connection() as con:
            con.execute("UPDATE submissions SET publish_state=?,error=?,updated_at=? WHERE request_key=?",
                        (state, error, time.time(), key))

    def _mark_diagnostic(self, source, key, job_id=None):
        """Mirror an explicit server classification; never infer from a name."""
        with self.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("INSERT OR IGNORE INTO diagnostic_sources VALUES(?,?,?)",(self.api_url,source,time.time()))
            con.execute("UPDATE submissions SET status='technical_test',publish_state='blocked',error='technical_test_source',job_id=COALESCE(?,job_id),updated_at=? WHERE request_key=?",
                        (job_id,time.time(),key))

    def tick(self, now: float | None = None) -> list[dict]:
        moment = time.time() if now is None else now
        results, seen = [], set()
        with self.connection() as con:
            diagnostic_sources = {row["source"] for row in con.execute("SELECT source FROM diagnostic_sources WHERE api_url=?",(self.api_url,))}
        for path in candidate_files(self.inbox):
            if path.suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            relative = path.relative_to(self.inbox).as_posix()
            seen.add(relative)
            if relative in diagnostic_sources:
                continue
            try:
                stat = path.stat()
                signature = (stat.st_size, stat.st_mtime_ns)
                with self.connection() as con:
                    old = con.execute("SELECT * FROM observations WHERE source=?", (relative,)).fetchone()
                    if relative not in self.primed or old is None or (old["size"],old["mtime_ns"]) != signature:
                        con.execute("INSERT OR REPLACE INTO observations VALUES(?,?,?,?)", (relative,*signature,moment))
                        self.primed.add(relative)
                        self.hash_cache.pop(relative, None)
                        continue
                    if not stat.st_size or moment - old["stable_since"] < self.stable_seconds:
                        continue
                cached = self.hash_cache.get(relative)
                if cached and cached[0] == signature:
                    digest = cached[1]
                else:
                    digest, _ = stable_hash(path)
                    self.hash_cache[relative] = (signature, digest)
                identity = json.dumps([self.api_url, relative, digest, self.settings_json], ensure_ascii=False)
                request_key = "watch-" + hashlib.sha256(identity.encode()).hexdigest()
                with self.connection() as con:
                    con.execute("INSERT OR IGNORE INTO submissions(request_key,source,sha256,settings,updated_at) VALUES(?,?,?,?,?)",
                                (request_key,relative,digest,self.settings_json,moment))
                    row = con.execute("SELECT * FROM submissions WHERE request_key=?", (request_key,)).fetchone()
                if row["status"] in {"submitted","technical_test"} or row["next_retry"] > moment:
                    continue
                try:
                    response = self._request("POST", "/api/jobs", json={"source": relative, "title": path.stem,
                        "settings": self.settings, "idempotency_key": request_key})
                    if response.status_code == 409 and response.json().get("code") == "technical_test_source":
                        self._mark_diagnostic(relative,request_key)
                        results.append({"source":relative,"status":"technical_test","message":"已登记的技术测试素材已排除正式剪辑队列"})
                        continue
                    if response.status_code not in (200, 201):
                        raise RuntimeError("api_rejected")
                    body = response.json()
                    job_id = body.get("job", {}).get("id")
                    if not isinstance(job_id, str) or not job_id:
                        raise RuntimeError("invalid_response")
                    if body.get("job",{}).get("job_kind") == "technical_test":
                        self._mark_diagnostic(relative,request_key,job_id)
                        continue
                    owned = body.get("owned_by_request") is True
                    with self.connection() as con:
                        con.execute("UPDATE submissions SET status='submitted',job_id=?,error=NULL,updated_at=?,owned_by_request=?,publish_state=? WHERE request_key=?",
                                    (job_id,moment,int(owned),"none" if owned else "unowned",request_key))
                    results.append({"source": relative, "status": "submitted", "job_id": job_id,"owned_by_request":owned})
                except Exception:
                    # Submission itself is safe to retry with the same idempotency key.
                    with self.connection() as con:
                        con.execute("UPDATE submissions SET next_retry=?,error='submission_waiting',updated_at=? WHERE request_key=?",
                                    (moment + 15,moment,request_key))
                    results.append({"source": relative, "status": "waiting", "message": "任务暂未确认，稍后用相同编号重试"})
            except (OSError, RuntimeError):
                self.hash_cache.pop(relative, None)
                self.primed.discard(relative)
                results.append({"source": relative, "status": "waiting", "message": "素材尚未稳定，保留原文件等待下一轮"})
        self.primed.intersection_update(seen)
        self.hash_cache = {key:value for key,value in self.hash_cache.items() if key in seen}
        if self.auto_publish:
            results.extend(self._advance_publications())
        return results

    def _advance_publications(self) -> list[dict]:
        with self.connection() as con:
            records = con.execute("SELECT * FROM submissions WHERE status='submitted' AND owned_by_request=1 AND publish_state IN ('none','approving') ORDER BY updated_at LIMIT 30").fetchall()
        results = []
        for row in records:
            key, job_id = row["request_key"], row["job_id"]
            try:
                response = self._request("GET", f"/api/jobs/{job_id}")
                if response.status_code != 200:
                    continue
                job = response.json().get("job", {})
                if job.get("job_kind") == "technical_test":
                    self._mark_diagnostic(row["source"],key,job_id)
                    results.append({"job_id":job_id,"status":"technical_test","message":"技术测试记录已停止自动审批和发布"})
                    continue
                status = job.get("status")
                if status == "published":
                    self._mark(key, "published")
                    continue
                if status == "failed":
                    self._mark(key, "blocked", "job_failed")
                    continue
                if status == "review":
                    if (job.get("result") or {}).get("qc", {}).get("passed") is not True:
                        self._mark(key, "blocked", "qc_failed")
                        continue
                    self._mark(key, "approving")
                    approved = self._request("POST", f"/api/jobs/{job_id}/approve")
                    if approved.status_code != 200:
                        continue
                    job = approved.json().get("job", {})
                    status = job.get("status")
                if status != "approved":
                    continue
                with self.connection() as con:
                    con.execute("BEGIN IMMEDIATE")
                    claimed = con.execute("UPDATE submissions SET publish_state='publish_attempted',updated_at=? WHERE request_key=? AND publish_state IN ('none','approving')", (time.time(),key)).rowcount
                if claimed != 1:
                    continue
                # Persist intent before calling the external-send endpoint. A crash or
                # timeout leaves publish_attempted and is deliberately never retried.
                try:
                    sent = self._request("POST", f"/api/jobs/{job_id}/publish")
                    if sent.status_code == 200 and sent.json().get("job", {}).get("status") == "published":
                        self._mark(key, "published")
                        results.append({"job_id": job_id, "status": "published"})
                    else:
                        self._mark(key, "blocked" if sent.status_code in (400,401,403,404,409) else "publish_unknown", "publish_unconfirmed")
                        results.append({"job_id": job_id, "status": "publish_unconfirmed", "message": "发布未确认，请在控制台核实，观察器不会重发"})
                except Exception:
                    self._mark(key, "publish_unknown", "publish_unconfirmed")
                    results.append({"job_id": job_id, "status": "publish_unknown", "message": "发送结果未知，观察器不会自动重发"})
            except Exception:
                # Reading a job or approving it can be retried; neither sends media.
                continue
        return results

    def run(self, stop_event: threading.Event | None = None, poll_seconds: float = 3):
        stop_event = stop_event or threading.Event()
        while not stop_event.is_set():
            results = self.tick()
            if results:
                print(json.dumps({"watch": results}, ensure_ascii=False), flush=True)
            stop_event.wait(max(1,poll_seconds))


def main(argv=None):
    project = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="CutFlow 素材目录观察器")
    parser.add_argument("--inbox", default=os.getenv("CUTFLOW_INBOX", str(project / "素材投递箱")))
    parser.add_argument("--data", default=os.getenv("CUTFLOW_DATA", str(project / "data")))
    parser.add_argument("--api-url", default=os.getenv("CUTFLOW_API_URL", "http://127.0.0.1:8765"))
    parser.add_argument("--settings", type=Path)
    parser.add_argument("--stable-seconds", type=float, default=15)
    parser.add_argument("--poll-seconds", type=float, default=3)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    try:
        settings = json.loads(os.getenv("CUTFLOW_SETTINGS", "{}"))
        if args.settings:
            settings = json.loads(args.settings.read_text(encoding="utf-8-sig"))
        if not isinstance(settings, dict):
            raise ValueError("剪辑设置需要 JSON 对象")
        watcher = Watcher(args.inbox,args.data,args.api_url,os.getenv("CUTFLOW_TOKEN", ""),settings,
                          args.stable_seconds,os.getenv("CUTFLOW_AUTO_PUBLISH") == "1")
    except (ValueError,OSError):
        parser.error("观察器配置无效：请检查目录、API 地址、访问口令和剪辑设置文件；稳定时间至少 15 秒")
    try:
        if args.once:
            watcher.tick()  # prime a stability window even on a fresh --once run
            time.sleep(args.stable_seconds)
            result = watcher.tick()
            print(json.dumps({"watch": result}, ensure_ascii=False), flush=True)
            return 1 if any(item["status"] in {"waiting","publish_unknown","publish_unconfirmed"} for item in result) else 0
        watcher.run(poll_seconds=args.poll_seconds)
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        watcher.close()


if __name__ == "__main__":
    raise SystemExit(main())
