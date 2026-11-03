"""Durable spoken-script drafts, separate from ideas and rendered video jobs."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .ideas_service import IdeaWorker, timestamp
from . import script_model


class BrandDetails(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(default='', max_length=120)
    cta: str = Field(default='', max_length=240)
    event_date: str = Field(default='', max_length=80)
    event_place: str = Field(default='', max_length=160)

    @field_validator('*')
    @classmethod
    def clean(cls, value):
        if any(ord(char) < 32 for char in value):
            raise ValueError('品牌资料不能包含控制字符')
        return value.strip()


class NewScript(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: UUID
    mode: Literal['talking_head', 'narrated_promo', 'brand_promo'] = 'narrated_promo'
    brief: str = Field(min_length=1, max_length=3000)
    sources: list[Annotated[str, Field(min_length=1, max_length=4096)]] = Field(default_factory=list, max_length=12)
    brand: BrandDetails = Field(default_factory=BrandDetails)
    subtype: Literal['corporate', 'salon_promo', 'salon_recap'] = 'salon_promo'
    target_duration: float | None = Field(default=None, ge=1, le=3600, allow_inf_nan=False)

    @field_validator('brief')
    @classmethod
    def clean_brief(cls, value):
        if not value.strip() or '\x00' in value:
            raise ValueError('请写下准备讲稿的想法')
        return value.strip()

    @field_validator('target_duration', mode='before')
    @classmethod
    def duration_number(cls, value):
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise ValueError('目标时长必须是秒数')
        return value


class ScriptStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.executescript('''CREATE TABLE IF NOT EXISTS scripts (
                id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, request_json TEXT NOT NULL,
                source_names TEXT NOT NULL, status TEXT NOT NULL, result TEXT, error TEXT,
                error_code TEXT, attempts INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            ); CREATE INDEX IF NOT EXISTS scripts_queue ON scripts(status,created_at);''')

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def decode(row):
        if row is None:
            return None
        record = dict(row)
        record.pop('fingerprint', None)
        record.update(json.loads(record.pop('request_json')))
        record['source_names'] = json.loads(record['source_names'])
        record['result'] = json.loads(record['result']) if record['result'] else None
        record['can_retry'] = record['status'] == 'failed'
        return record

    @staticmethod
    def fingerprint(request):
        return hashlib.sha256(json.dumps(request, sort_keys=True, ensure_ascii=False,
                                        separators=(',', ':')).encode()).hexdigest()

    @staticmethod
    def capacity(conn):
        if conn.execute("SELECT COUNT(*) FROM scripts WHERE status IN ('queued','running')").fetchone()[0] >= 4:
            raise HTTPException(429, '已有 4 份讲稿正在准备或等待，请完成后再提交。')

    def get(self, identifier):
        with self.connection() as conn:
            return self.decode(conn.execute('SELECT * FROM scripts WHERE id=?', (identifier,)).fetchone())

    def same_request(self, identifier, request):
        with self.connection() as conn:
            row = conn.execute('SELECT * FROM scripts WHERE id=?', (identifier,)).fetchone()
            if row is None:
                return None
            if row['fingerprint'] != self.fingerprint(request):
                raise HTTPException(409, '这个请求编号已用于另一份讲稿，请使用新的编号。')
            return self.decode(row)

    def create(self, identifier, request, source_names):
        with self.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM scripts WHERE id=?', (identifier,)).fetchone()
            signature = self.fingerprint(request)
            if row:
                if row['fingerprint'] != signature:
                    raise HTTPException(409, '这个请求编号已用于另一份讲稿，请使用新的编号。')
                return self.decode(row)
            self.capacity(conn)
            stamp = timestamp()
            conn.execute('''INSERT INTO scripts(id,fingerprint,request_json,source_names,status,created_at,updated_at)
                            VALUES(?,?,?,?,'queued',?,?)''', (identifier, signature,
                json.dumps(request, ensure_ascii=False), json.dumps(source_names, ensure_ascii=False), stamp, stamp))
            return self.decode(conn.execute('SELECT * FROM scripts WHERE id=?', (identifier,)).fetchone())

    def claim(self):
        with self.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute("SELECT * FROM scripts WHERE status='queued' ORDER BY created_at,id LIMIT 1").fetchone()
            if row is None:
                return None
            conn.execute("UPDATE scripts SET status='running',attempts=attempts+1,updated_at=? WHERE id=?", (timestamp(), row['id']))
            return self.decode(conn.execute('SELECT * FROM scripts WHERE id=?', (row['id'],)).fetchone())

    def complete(self, identifier, *, result=None, code=None):
        with self.connection() as conn:
            conn.execute("UPDATE scripts SET status=?,result=?,error=?,error_code=?,updated_at=? WHERE id=? AND status='running'",
                ('failed' if code else 'succeeded', json.dumps(result, ensure_ascii=False) if result is not None else None,
                 script_model.ERRORS.get(code) if code else None, code, timestamp(), identifier))

    def retry(self, identifier):
        with self.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM scripts WHERE id=?', (identifier,)).fetchone()
            if row is None:
                raise HTTPException(404, '讲稿记录不存在。')
            if row['status'] in {'queued', 'running'}:
                return self.decode(row)
            if row['status'] != 'failed':
                raise HTTPException(409, '这份讲稿已经完成；修改要求后可新建请求。')
            self.capacity(conn)
            conn.execute("UPDATE scripts SET status='queued',result=NULL,error=NULL,error_code=NULL,updated_at=? WHERE id=?",
                         (timestamp(), identifier))
            return self.decode(conn.execute('SELECT * FROM scripts WHERE id=?', (identifier,)).fetchone())

    def recover(self):
        with self.connection() as conn:
            conn.execute("UPDATE scripts SET status='failed',result=NULL,error=?,error_code='interrupted',updated_at=? WHERE status='running'",
                         (script_model.ERRORS['interrupted'], timestamp()))


class ScriptWorker(IdeaWorker):
    def __init__(self, store, provider=None):
        super().__init__(store, provider or script_model)

    def process(self, request):
        try:
            if self.stop_event.is_set():
                self.store.complete(request['id'], code='cancelled')
                return
            if not self.supported():
                self.store.complete(request['id'], code='not_configured')
                return
            values = script_model._request(request['brief'], request['source_names'], mode=request['mode'],
                brand=request['brand'], subtype=request['subtype'], target_duration=request['target_duration'])
            raw = self.provider.generate_script(**values, stop_event=self.stop_event)
            result = script_model.validate_result(raw, values)
            self.store.complete(request['id'], code='cancelled' if self.stop_event.is_set() else None,
                                result=None if self.stop_event.is_set() else result)
        except Exception as exc:
            code = getattr(exc, 'code', 'generation_failed')
            if not isinstance(code, str) or code not in script_model.ERRORS:
                code = 'generation_failed'
            self.store.complete(request['id'], code=code)


def register_script_routes(app, authorize, inbox, data, *, source_validator, name_formatter, provider=None):
    worker = ScriptWorker(ScriptStore(Path(data) / 'scripts.sqlite3'), provider)

    def require_ready():
        if not worker.supported():
            raise HTTPException(503, script_model.ERRORS['not_configured'])

    def response(record):
        return JSONResponse({'script': record}, status_code=202 if record['status'] in {'queued', 'running'} else 200)

    @app.post('/api/scripts', dependencies=[Depends(authorize)])
    def create_script(body: NewScript):
        identifier = str(body.request_id)
        request = body.model_dump(exclude={'request_id'})
        previous = worker.store.same_request(identifier, request)
        if previous:
            return response(previous)
        require_ready()
        names, seen = [], set()
        for relative in body.sources:
            try:
                path = source_validator(inbox, relative)
            except (ValueError, OSError):
                raise HTTPException(400, '所选素材不存在或路径无效，请从已同步素材选择。') from None
            identity = os.path.normcase(str(path))
            if identity in seen:
                raise HTTPException(400, '同一素材不能重复选择。')
            seen.add(identity)
            names.append(name_formatter(path.relative_to(inbox).as_posix()))
        record = worker.store.create(identifier, request, names)
        worker.wake_event.set()
        return response(record)

    @app.get('/api/scripts/{script_id}', dependencies=[Depends(authorize)])
    def get_script(script_id: UUID):
        record = worker.store.get(str(script_id))
        if record is None:
            raise HTTPException(404, '讲稿记录不存在。')
        return {'script': record}

    @app.post('/api/scripts/{script_id}/retry', dependencies=[Depends(authorize)])
    def retry_script(script_id: UUID):
        require_ready()
        record = worker.store.retry(str(script_id))
        worker.wake_event.set()
        return response(record)

    return worker
