"""Small durable job store. Every transition is checked inside SQLite."""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StateConflict(ValueError):
    pass


class DiagnosticSourceConflict(StateConflict):
    code = "technical_test_source"


class Store:
    JSON_FIELDS = {"settings", "plan", "result", "publication", "production"}

    @staticmethod
    def source_names(source: str, settings: dict) -> set[str]:
        names = {source}
        creation = settings.get('creation') if isinstance(settings, dict) else None
        if isinstance(creation, dict):
            for item in creation.get('sources', []):
                if isinstance(item, dict) and isinstance(item.get('source'), str):
                    names.add(item['source'])
        return names

    @classmethod
    def _diagnostic_sources(cls, conn) -> set[str]:
        names = set()
        for row in conn.execute("SELECT source,settings FROM jobs WHERE job_kind='technical_test'"):
            names.update(cls.source_names(row['source'], json.loads(row['settings'])))
        return names

    @classmethod
    def _check_diagnostic_sources(cls, conn, source: str, settings: dict):
        if cls.source_names(source, settings) & cls._diagnostic_sources(conn):
            raise DiagnosticSourceConflict("所选素材包含已登记的技术测试，不能进入正式剪辑或发布流程")

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, source TEXT NOT NULL, title TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, idempotency_key TEXT UNIQUE,
                    source_size INTEGER NOT NULL, source_mtime_ns INTEGER NOT NULL,
                    settings TEXT NOT NULL, status TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    plan TEXT, result TEXT, output_path TEXT, error TEXT,
                    publication TEXT, progress INTEGER NOT NULL DEFAULT 0,
                    job_kind TEXT NOT NULL DEFAULT 'production', diagnostic_reason TEXT
                );
                CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status,created_at);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                    created_at TEXT NOT NULL, status TEXT NOT NULL, message TEXT NOT NULL
                );
            """)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
            if "job_kind" not in columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN job_kind TEXT NOT NULL DEFAULT 'production'")
            if "diagnostic_reason" not in columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN diagnostic_reason TEXT")
            if "production" not in columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN production TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS jobs_kind_status ON jobs(job_kind,status,created_at)")

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @classmethod
    def decode(cls, row):
        if row is None:
            return None
        value = dict(row)
        for key in cls.JSON_FIELDS:
            if value.get(key) is not None:
                value[key] = json.loads(value[key])
        value.pop("idempotency_key", None)
        return value

    def get(self, job_id: str):
        with self.connection() as conn:
            return self.decode(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def owned_by_request(self, job_id: str, request_key: str | None) -> bool:
        """Prove request ownership without returning the private idempotency key."""
        if not request_key:
            return False
        with self.connection() as conn:
            return conn.execute("SELECT 1 FROM jobs WHERE id=? AND idempotency_key=?",
                                (job_id,request_key)).fetchone() is not None

    def list(self, limit: int = 100, kind: str = "production"):
        if kind not in {"production", "technical_test", "all"}:
            raise ValueError("Invalid job kind")
        with self.connection() as conn:
            if kind == "all":
                return [self.decode(row) for row in conn.execute(
                    "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))]
            return [self.decode(row) for row in conn.execute(
                "SELECT * FROM jobs WHERE job_kind=? ORDER BY created_at DESC LIMIT ?", (kind,limit))]

    def events(self, job_id: str):
        with self.connection() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT created_at,status,message FROM events WHERE job_id=? ORDER BY id", (job_id,))]

    def counts(self, kind: str = "production"):
        if kind not in {"production", "technical_test", "all"}:
            raise ValueError("Invalid job kind")
        with self.connection() as conn:
            if kind == "all":
                return {row["status"]: row["count"] for row in conn.execute(
                    "SELECT status,COUNT(*) AS count FROM jobs GROUP BY status")}
            return {row["status"]: row["count"] for row in conn.execute(
                "SELECT status,COUNT(*) AS count FROM jobs WHERE job_kind=? GROUP BY status", (kind,))}

    def technical_test_sources(self) -> set[str]:
        """Only explicitly registered diagnostics; never infer from filenames."""
        with self.connection() as conn:
            return self._diagnostic_sources(conn)

    def mark_technical_test(self, job_id: str, reason: str):
        """Internal administrative classification; retains media and audit evidence.

        Callers must establish the asset's provenance before invoking this method.
        There is deliberately no public API route that performs classification.
        """
        if not isinstance(reason,str) or not reason.strip() or len(reason)>2000:
            raise ValueError("技术测试分类必须记录 1–2000 字符的具体原因")
        reason = reason.strip()
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM jobs WHERE id=?",(job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            if row["status"] not in {"review","approved","failed"}:
                raise StateConflict("任务正在执行或已发布，不能在此状态重新分类为技术测试")
            if row["job_kind"] == "technical_test" and row["diagnostic_reason"] == reason:
                return self.decode(row)
            timestamp = now()
            conn.execute("UPDATE jobs SET job_kind='technical_test',diagnostic_reason=?,updated_at=? WHERE id=?",
                         (reason,timestamp,job_id))
            conn.execute("INSERT INTO events(job_id,created_at,status,message) VALUES(?,?,?,?)",
                         (job_id,timestamp,row["status"],"已明确归档为技术测试，保留验证资料，不用于正式发布："+reason))
            return self.decode(conn.execute("SELECT * FROM jobs WHERE id=?",(job_id,)).fetchone())

    def create(self, *, source: str, title: str, settings: dict, fingerprint: str,
               source_size: int, source_mtime_ns: int, idempotency_key: str | None = None,
               production: dict | None = None):
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._check_diagnostic_sources(conn, source, settings)
            if idempotency_key:
                previous = conn.execute("SELECT * FROM jobs WHERE idempotency_key=?", (idempotency_key,)).fetchone()
                if previous:
                    if previous["fingerprint"] != fingerprint:
                        raise StateConflict("同一请求编号已用于其他素材或剪辑设置")
                    return self.decode(previous), False
            previous = conn.execute(
                "SELECT * FROM jobs WHERE fingerprint=? AND status IN ('queued','analyzing','rendering') LIMIT 1",
                (fingerprint,)).fetchone()
            if previous:
                return self.decode(previous), False
            job_id, timestamp = uuid.uuid4().hex, now()
            conn.execute("""INSERT INTO jobs
                (id,source,title,fingerprint,idempotency_key,source_size,source_mtime_ns,settings,status,created_at,updated_at,production)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (
                job_id, source, title, fingerprint, idempotency_key, source_size, source_mtime_ns,
                json.dumps(settings, ensure_ascii=False), "queued", timestamp, timestamp,
                json.dumps(production, ensure_ascii=False) if production is not None else None))
            conn.execute("INSERT INTO events(job_id,created_at,status,message) VALUES (?,?,?,?)",
                         (job_id, timestamp, "queued", "任务已加入队列"))
            return self.decode(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()), True

    def transition(self, job_id: str, expected: Iterable[str], status: str, message: str, **fields: Any):
        allowed = {"plan", "result", "output_path", "error", "publication", "progress", "production"}
        if fields.keys() - allowed:
            raise ValueError("Unsupported job fields")
        expected = tuple(expected)
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise KeyError(job_id)
            if row["status"] not in expected:
                raise StateConflict(f"当前状态 {row['status']} 不允许此操作")
            if row["job_kind"] == "technical_test" and status in {"queued","analyzing","rendering","approved","publishing","published"}:
                raise StateConflict("技术测试记录仅供排查，不能审批、重新剪辑或发布为正式视频")
            if status in {"queued", "analyzing", "rendering", "approved", "publishing"}:
                self._check_diagnostic_sources(conn, row['source'], json.loads(row['settings']))
            timestamp = now()
            changes = {"status": status, "updated_at": timestamp, **fields}
            values = [json.dumps(v, ensure_ascii=False) if k in self.JSON_FIELDS and v is not None else v
                      for k, v in changes.items()]
            conn.execute("UPDATE jobs SET " + ",".join(k + "=?" for k in changes) + " WHERE id=?", values + [job_id])
            conn.execute("INSERT INTO events(job_id,created_at,status,message) VALUES (?,?,?,?)",
                         (job_id, timestamp, status, message))
            return self.decode(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def claim(self):
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM jobs WHERE status='queued' AND job_kind='production' ORDER BY created_at LIMIT 1").fetchone()
            if not row:
                return None
            timestamp = now()
            conn.execute("UPDATE jobs SET status='analyzing',progress=10,updated_at=? WHERE id=?", (timestamp,row["id"]))
            conn.execute("INSERT INTO events(job_id,created_at,status,message) VALUES (?,?,?,?)",
                         (row["id"], timestamp, "analyzing", "正在分析原始素材"))
            return self.decode(conn.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())

    def recover(self):
        """Re-render interrupted encodes, but never retry an uncertain external send."""
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("SELECT id,status,production FROM jobs WHERE status IN ('analyzing','rendering','publishing')").fetchall()
            for row in rows:
                was_sending = row["status"] == "publishing"
                state = "failed" if was_sending else "queued"
                note = ("发布期间服务中断，发送结果未知。请先到企业微信核实，系统不会自动重复发送。"
                        if was_sending else "服务重新启动，已重新排队恢复剪辑")
                conn.execute("UPDATE jobs SET status=?,updated_at=?,error=?,progress=0 WHERE id=?",
                             (state,now(),note if was_sending else None,row["id"]))
                if row['production'] and not was_sending:
                    production = json.loads(row['production'])
                    production.update(stage='queued', error_code=None)
                    conn.execute('UPDATE jobs SET production=? WHERE id=?', (json.dumps(production, ensure_ascii=False), row['id']))
                conn.execute("INSERT INTO events(job_id,created_at,status,message) VALUES (?,?,?,?)",
                             (row["id"],now(),state,note))
