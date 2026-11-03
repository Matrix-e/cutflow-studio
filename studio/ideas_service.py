"""Durable, bounded semantic idea refinement; never creates video jobs."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import importlib
import json
import os
from pathlib import Path
import sqlite3
import threading
from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


MAX_PENDING = 4
ERRORS = {
    'not_configured': '想法模型尚未就绪，请稍后重试。',
    'busy': '想法模型正在处理其他请求，请稍后手动重试。',
    'cancelled': '本次完善已取消，可以手动重试。',
    'interrupted': '上次完善因服务中断未完成，可以手动重试。',
    'timeout': '完善想法超过处理时限，已停止生成，可以手动重试。',
    'invalid_output': '模型未返回完整、有效的创意方案，可以手动重试。',
    'gpu_unavailable': '模型所需的 GPU 暂时不可用，请稍后重试。',
    'resource_limit': '当前资源不足以运行想法模型，请稍后重试。',
    'generation_failed': '想法完善未完成，可以手动重试。',
    'invalid_request': '想法模型无法处理当前输入，请调整想法后重新提交。',
}


def timestamp():
    return datetime.now(timezone.utc).isoformat()


class NewIdea(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: UUID
    brief: str = Field(min_length=1, max_length=3000)
    sources: list[Annotated[str, Field(min_length=1, max_length=4096)]] = Field(default_factory=list, max_length=12)
    style: str | None = Field(default=None, max_length=100)

    @field_validator('brief')
    @classmethod
    def nonempty_brief(cls, value):
        value = value.strip()
        if not value or '\x00' in value:
            raise ValueError('请先写下一句想法')
        return value

    @field_validator('style')
    @classmethod
    def normalize_style(cls, value):
        if value is not None and '\x00' in value:
            raise ValueError('风格不能包含控制字符')
        return value.strip() or None if value is not None else None


class IdeaResult(BaseModel):
    # Return only the documented generated content, never arbitrary provider
    # metadata, diagnostics, paths, or raw model output.
    model_config = ConfigDict(extra='ignore')
    expanded_brief: str = Field(min_length=1, max_length=2400)
    production_brief: str = Field(min_length=1, max_length=500)
    title: str = Field(min_length=1, max_length=120)
    audience: str = Field(min_length=1, max_length=400)
    goal: str = Field(min_length=1, max_length=500)
    hook: str = Field(min_length=1, max_length=500)
    outline: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(min_length=1, max_length=20)
    visual_style: str = Field(min_length=1, max_length=500)
    narration: str = Field(min_length=1, max_length=800)
    music: str = Field(min_length=1, max_length=500)
    cta: str = Field(min_length=1, max_length=500)
    assumptions: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(max_length=20)
    summary: str = Field(default='', max_length=600)

    @field_validator('*', mode='after')
    @classmethod
    def reject_blank_values(cls, value):
        if isinstance(value, str) and value and (not value.strip() or '\x00' in value):
            raise ValueError('模型返回了无效文字')
        if isinstance(value, list) and any(not item.strip() or '\x00' in item for item in value):
            raise ValueError('模型返回了无效条目')
        return value


class LocalIdeaProvider:
    def configuration_status(self):
        try:
            return importlib.import_module('studio.idea_model').configuration_status()
        except Exception:
            return {'configured': False, 'model_exists': False, 'python_exists': False}

    def generate_idea(self, brief, source_names, style='', *, stop_event=None):
        return importlib.import_module('studio.idea_model').generate_idea(brief, source_names, style, stop_event=stop_event)


class IdeaStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS ideas (
                    id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, brief TEXT NOT NULL,
                    sources TEXT NOT NULL, source_names TEXT NOT NULL, style TEXT,
                    status TEXT NOT NULL, result TEXT, error TEXT, error_code TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ideas_queue ON ideas(status,created_at);
            ''')

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def decode(row):
        if row is None:
            return None
        data = dict(row)
        data.pop('fingerprint', None)
        for key in ('sources', 'source_names', 'result'):
            if data[key] is not None:
                data[key] = json.loads(data[key])
        data['can_retry'] = data['status'] == 'failed'
        return data

    def get(self, identifier):
        with self.connection() as conn:
            return self.decode(conn.execute('SELECT * FROM ideas WHERE id=?', (identifier,)).fetchone())

    @staticmethod
    def check_capacity(conn):
        count = conn.execute("SELECT COUNT(*) FROM ideas WHERE status IN ('queued','running')").fetchone()[0]
        if count >= MAX_PENDING:
            raise HTTPException(429, '已有 4 条想法正在处理或等待，请完成后再提交。')

    def create(self, identifier, *, brief, sources, source_names, style):
        payload = json.dumps([brief, sources, style], ensure_ascii=False, separators=(',', ':'))
        fingerprint = hashlib.sha256(payload.encode()).hexdigest()
        with self.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            previous = conn.execute('SELECT * FROM ideas WHERE id=?', (identifier,)).fetchone()
            if previous:
                if previous['fingerprint'] != fingerprint:
                    raise HTTPException(409, '这个请求编号已用于另一份想法，请重新提交新的编号。')
                return self.decode(previous)
            self.check_capacity(conn)
            stamp = timestamp()
            conn.execute('''INSERT INTO ideas(id,fingerprint,brief,sources,source_names,style,status,created_at,updated_at)
                            VALUES(?,?,?,?,?,?,'queued',?,?)''',
                         (identifier, fingerprint, brief, json.dumps(sources, ensure_ascii=False),
                          json.dumps(source_names, ensure_ascii=False), style, stamp, stamp))
            return self.decode(conn.execute('SELECT * FROM ideas WHERE id=?', (identifier,)).fetchone())

    def retry(self, identifier):
        with self.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM ideas WHERE id=?', (identifier,)).fetchone()
            if row is None:
                raise HTTPException(404, '想法记录不存在。')
            if row['status'] in {'queued', 'running'}:
                return self.decode(row)
            if row['status'] != 'failed':
                raise HTTPException(409, '已经完成的方案不需要重试；修改想法后可重新提交。')
            self.check_capacity(conn)
            conn.execute("UPDATE ideas SET status='queued',result=NULL,error=NULL,error_code=NULL,updated_at=? WHERE id=?",
                         (timestamp(), identifier))
            return self.decode(conn.execute('SELECT * FROM ideas WHERE id=?', (identifier,)).fetchone())

    def claim(self):
        with self.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute("SELECT * FROM ideas WHERE status='queued' ORDER BY created_at,id LIMIT 1").fetchone()
            if row is None:
                return None
            conn.execute("UPDATE ideas SET status='running',attempts=attempts+1,updated_at=? WHERE id=?",
                         (timestamp(), row['id']))
            return self.decode(conn.execute('SELECT * FROM ideas WHERE id=?', (row['id'],)).fetchone())

    def complete(self, identifier, *, result=None, code=None):
        with self.connection() as conn:
            status = 'failed' if code else 'succeeded'
            conn.execute("UPDATE ideas SET status=?,result=?,error=?,error_code=?,updated_at=? WHERE id=? AND status='running'",
                         (status, json.dumps(result, ensure_ascii=False) if result is not None else None,
                          ERRORS.get(code) if code else None, code, timestamp(), identifier))

    def recover(self):
        with self.connection() as conn:
            conn.execute("UPDATE ideas SET status='failed',result=NULL,error=?,error_code='interrupted',updated_at=? WHERE status='running'",
                         (ERRORS['interrupted'], timestamp()))


class IdeaWorker:
    def __init__(self, store: IdeaStore, provider=None):
        self.store, self.provider = store, provider or LocalIdeaProvider()
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.thread = None

    def supported(self):
        try:
            state = self.provider.configuration_status()
            return all(state.get(key) is True for key in ('configured', 'model_exists', 'python_exists'))
        except Exception:
            return False

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.store.recover()
        self.stop_event.clear()
        self.thread = threading.Thread(target=self.run, name='cutflow-ideas', daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.wake_event.set()
        if self.thread:
            self.thread.join(timeout=5)

    def run(self):
        while not self.stop_event.is_set():
            request = self.store.claim()
            if request is None:
                self.wake_event.wait(timeout=0.5)
                self.wake_event.clear()
                continue
            self.process(request)

    def process(self, request):
        try:
            if self.stop_event.is_set():
                self.store.complete(request['id'], code='cancelled')
                return
            if not self.supported():
                self.store.complete(request['id'], code='not_configured')
                return
            raw = self.provider.generate_idea(request['brief'], request['source_names'], request['style'] or '',
                                              stop_event=self.stop_event)
            result = IdeaResult.model_validate(raw).model_dump()
            if self.stop_event.is_set():
                self.store.complete(request['id'], code='cancelled')
            else:
                self.store.complete(request['id'], result=result)
        except ValidationError:
            self.store.complete(request['id'], code='invalid_output')
        except Exception as error:
            code = getattr(error, 'code', 'generation_failed')
            if not isinstance(code, str) or code not in ERRORS:
                code = 'generation_failed'
            self.store.complete(request['id'], code=code)


def register_ideas_routes(app, authorize, inbox: Path, data: Path, *, source_validator, name_formatter, provider=None):
    worker = IdeaWorker(IdeaStore(data / 'ideas.sqlite3'), provider)

    def ensure_supported():
        if not worker.supported():
            raise HTTPException(503, ERRORS['not_configured'])

    @app.post('/api/ideas', dependencies=[Depends(authorize)])
    def new_idea(body: NewIdea):
        # An already generated idea is independent of the later lifetime of its
        # optional media files. Resolve filenames only for a new request.
        identifier = str(body.request_id)
        previous = worker.store.get(identifier)
        if previous:
            if (previous['brief'], previous['sources'], previous['style']) != (body.brief, body.sources, body.style):
                raise HTTPException(409, '这个请求编号已用于另一份想法，请重新提交新的编号。')
            return JSONResponse({'idea': previous}, status_code=202 if previous['status'] in {'queued', 'running'} else 200)
        ensure_supported()
        names, seen = [], set()
        for source in body.sources:
            try:
                path = source_validator(inbox, source)
            except (ValueError, OSError):
                raise HTTPException(400, '所选素材不存在或路径无效，请从已同步素材中选择。') from None
            identity = os.path.normcase(str(path))
            if identity in seen:
                raise HTTPException(400, '同一素材不能重复选择。')
            seen.add(identity)
            names.append(name_formatter(path.relative_to(inbox).as_posix()))
        idea = worker.store.create(identifier, brief=body.brief, sources=body.sources, source_names=names, style=body.style)
        worker.wake_event.set()
        return JSONResponse({'idea': idea}, status_code=202 if idea['status'] in {'queued', 'running'} else 200)

    @app.get('/api/ideas/{idea_id}', dependencies=[Depends(authorize)])
    def get_idea(idea_id: UUID):
        idea = worker.store.get(str(idea_id))
        if idea is None:
            raise HTTPException(404, '想法记录不存在。')
        return {'idea': idea}

    @app.post('/api/ideas/{idea_id}/retry', dependencies=[Depends(authorize)])
    def retry_idea(idea_id: UUID):
        ensure_supported()
        idea = worker.store.retry(str(idea_id))
        worker.wake_event.set()
        return JSONResponse({'idea': idea}, status_code=202)

    return worker
