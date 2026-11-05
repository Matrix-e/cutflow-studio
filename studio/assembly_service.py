"""Durable script-to-footage planning. A ready plan is immutable and renderable.

The browser may close during analysis: auto_render intent and its idempotency
key live in SQLite, and handoff resumes after a service restart.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import traceback
from uuid import UUID

from fastapi import HTTPException

PIPELINE_LOCK = threading.Lock()
OPTION_DEFAULTS = {'aspect':'9:16','music':True,'subtitles':True,'target_duration':None,'opening_title':''}
HANDOFF_MAX_ATTEMPTS = 3
HANDOFF_RETRY_SECONDS = (5,20)
SAFE_ERRORS = {
    'material_shortfall':'素材可用画面不足以覆盖完整脚本，请补充素材或缩短脚本。',
    'no_usable_material':'没有可用的视频画面，请重新选择或上传素材。',
    'analysis_unavailable':'素材分析服务暂时不可用，请稍后重试。',
    'narration_failed':'旁白生成未完成，请稍后重试。',
    'invalid_request':'编排输入无效，请检查脚本与所选素材。',
    'cancelled':'编排已停止，请稍后重试。',
    'render_failed':'画面处理未完成，请稍后重试。',
    'source_changed':'素材在编排后发生变化，请重新选材，原脚本已保留。',
    'source_unavailable':'选中素材已不可用，请重新编排。',
    'plan_changed':'编排文件已发生变化，请重新编排。',
    'script_mismatch':'编排没有保留最终脚本，已停止制作。',
    'assembly_failed':'编排未完成，请稍后重试。',
}


class AssemblyServiceError(ValueError):
    def __init__(self,code):
        self.code=code if code in SAFE_ERRORS else 'assembly_failed'
        super().__init__(SAFE_ERRORS[self.code])


def _safe_error(error):
    code=getattr(error,'code','assembly_failed')
    if not isinstance(code,str) or code not in SAFE_ERRORS:code='assembly_failed'
    return code,SAFE_ERRORS[code]


def script_hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def normalized_options(options):
    return {key:options.get(key,default) for key,default in OPTION_DEFAULTS.items()}


def _json(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)


def _now():
    return datetime.now(timezone.utc).isoformat()


class AssemblyService:
    def __init__(self, data, inbox, *, validate_source, source_allowed, enqueue_render, builder=None):
        self.data=Path(data);self.inbox=Path(inbox);self.validate_source=validate_source
        self.source_allowed=source_allowed;self.enqueue_render=enqueue_render;self.builder=builder
        self.root=self.data/'assembly-plans';self.root.mkdir(parents=True,exist_ok=True)
        self.db=self.data/'assembly.sqlite3';self.stop_event=threading.Event();self.wake_event=threading.Event();self.thread=None
        self.handoff_lock=threading.Lock()
        with closing(self.connect()) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('''CREATE TABLE IF NOT EXISTS assemblies(
                id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, request_json TEXT NOT NULL,
                snapshot_json TEXT NOT NULL, status TEXT NOT NULL, stage TEXT NOT NULL,
                result_json TEXT, plan_sha256 TEXT, error TEXT, job_id TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')
            columns={row['name'] for row in db.execute('PRAGMA table_info(assemblies)')}
            for name,definition in [('error_code','TEXT'),('handoff_attempts','INTEGER NOT NULL DEFAULT 0'),
                                    ('handoff_next_at','REAL NOT NULL DEFAULT 0')]:
                if name not in columns:db.execute(f'ALTER TABLE assemblies ADD COLUMN {name} {definition}')
            db.commit()

    def connect(self):
        db=sqlite3.connect(self.db,timeout=10);db.row_factory=sqlite3.Row;return db

    def get(self, identifier):
        with closing(self.connect()) as db:row=db.execute('SELECT * FROM assemblies WHERE id=?',(str(identifier),)).fetchone()
        if row is None:return None
        item=dict(row)
        for field in ('request','snapshot','result'):
            item[field]=json.loads(item.pop(field+'_json')) if item[field+'_json'] else None
        return item

    def same_request(self, identifier, request):
        prior=self.get(identifier)
        if prior and prior['fingerprint']!=script_hash(_json(request)):
            # title was added in 0.10.1. Restoring a 0.10.0 browser request must
            # preserve its identity when the new field is simply empty.
            comparable=lambda value:{key:item for key,item in value.items() if not (key=='title' and item=='')}
            if comparable(prior['request'])!=comparable(request):
                raise HTTPException(409,'这个编排请求已用于另一份脚本，请重新提交。')
        return prior

    def create(self, identifier, request, snapshot):
        identifier=str(UUID(str(identifier)))
        prior=self.same_request(identifier,request)
        if prior:return prior
        now=_now()
        with closing(self.connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            prior=db.execute('SELECT fingerprint FROM assemblies WHERE id=?',(identifier,)).fetchone()
            if prior:
                if prior['fingerprint']!=script_hash(_json(request)):raise HTTPException(409,'请求编号已被另一份编排使用。')
            else:
                if db.execute("SELECT count(*) FROM assemblies WHERE status IN ('queued','running')").fetchone()[0]>=3:
                    raise HTTPException(429,'已有三份编排正在处理，请等待当前任务完成。')
                db.execute('INSERT INTO assemblies(id,fingerprint,request_json,snapshot_json,status,stage,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                    (identifier,script_hash(_json(request)),_json(request),_json(snapshot),'queued','queued',now,now))
            db.commit()
        self.wake_event.set();return self.get(identifier)

    def public(self, record):
        return {key:record.get(key) for key in ('id','status','stage','result','error','error_code','job_id','handoff_attempts','created_at','updated_at')}

    def update(self, identifier, **values):
        assert set(values)<={'status','stage','result_json','plan_sha256','error','error_code','job_id','handoff_attempts','handoff_next_at'}
        values['updated_at']=_now()
        with closing(self.connect()) as db:
            db.execute('UPDATE assemblies SET '+','.join(key+'=?' for key in values)+' WHERE id=?',(*values.values(),identifier));db.commit()

    def validate_snapshot(self, record):
        paths=[]
        for item in record['snapshot']['sources']:
            if not self.source_allowed(item['source']):raise AssemblyServiceError('source_unavailable')
            try:path=self.validate_source(self.inbox,item['source']);stat=path.stat()
            except (OSError,ValueError):raise AssemblyServiceError('source_unavailable') from None
            if any(item[key]!=value for key,value in [('size',stat.st_size),('mtime_ns',stat.st_mtime_ns),('ctime_ns',stat.st_ctime_ns)]):
                raise AssemblyServiceError('source_changed')
            paths.append(path)
        return paths

    def load_plan(self, identifier):
        record=self.get(str(identifier))
        if not record:raise HTTPException(404,'编排方案不存在。')
        if record['status']!='ready':raise HTTPException(409,'请等待自动选材与编排完成。')
        try:
            self.validate_snapshot(record)
            path=self.root/record['id']/'plan.json'
            raw=path.read_bytes()
            if script_hash(raw.decode('utf-8'))!=record['plan_sha256']:raise AssemblyServiceError('plan_changed')
            plan=json.loads(raw)
            if (plan['assembly']['script_sha256']!=script_hash(record['request']['spoken_script'])
                    or plan['assembly'].get('script')!=record['request']['spoken_script']
                    or plan.get('spoken_script')!=record['request']['spoken_script']):
                raise AssemblyServiceError('script_mismatch')
            return record,plan
        except (OSError,ValueError,KeyError) as exc:
            raise HTTPException(409,_safe_error(exc)[1]) from None

    def process(self, record):
        identifier=record['id'];folder=self.root/identifier;folder.mkdir(exist_ok=True)
        try:
            paths=self.validate_snapshot(record)
            request=record['request'];snapshot=record['snapshot']
            def stage(value):
                if self.stop_event.is_set():raise RuntimeError('服务正在重启，稍后会继续编排。')
                self.update(identifier,stage=value)
            stage('validating')
            if self.builder is None:
                from .auto_assembly import build_auto_assembly
                builder=build_auto_assembly
            else:builder=self.builder
            plan=builder(paths,request['spoken_script'],normalized_options(request['options']),snapshot['settings'],
                work_dir=folder,data=self.data,stop_event=self.stop_event,on_stage=stage)
            self.validate_snapshot(record)
            assembly=plan['assembly']
            if (assembly['script_sha256']!=script_hash(request['spoken_script']) or assembly['script']!=request['spoken_script']
                    or plan.get('spoken_script')!=request['spoken_script']):
                raise AssemblyServiceError('script_mismatch')
            plan['brand_context']=snapshot.get('brand_context',{})
            raw=_json(plan);temp=folder/'plan.json.tmp'
            with temp.open('w',encoding='utf-8') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
            os.replace(temp,folder/'plan.json')
            public={key:assembly.get(key) for key in ('script','script_sha256','summary','stats')}
            public['sources']=[item['source'] for item in snapshot['sources']]
            public['shots']=[{**{k:shot.get(k) for k in ('source_in','source_out','timeline_start','timeline_end','frames','narration','selection_basis','selection_note','score','thumbnail')},
                              'source':public['sources'][shot['source_index']]} for shot in assembly['shots']]
            public['stats']={**(public.get('stats') or {}),**snapshot.get('scope',{})}
            public['brand_context_revision']=snapshot.get('brand_context',{}).get('revision')
            self.update(identifier,status='ready',stage='ready',result_json=_json(public),plan_sha256=script_hash(raw),
                        error=None,error_code=None,handoff_attempts=0,handoff_next_at=0)
        except Exception as exc:
            try:(folder/'error.log').write_text(traceback.format_exc(),encoding='utf-8')
            except OSError:pass
            if self.stop_event.is_set():self.update(identifier,status='queued',stage='queued',error=None,error_code=None)
            else:
                code,detail=_safe_error(exc)
                self.update(identifier,status='failed',stage='failed',error=detail,error_code=code)
            return
        self.handoff(self.get(identifier))

    def handoff(self, record):
        with self.handoff_lock:
            record=self.get(record['id'])
            if (not record or not record['request'].get('auto_render') or record.get('job_id') or record['status']!='ready'
                    or record['handoff_attempts']>=HANDOFF_MAX_ATTEMPTS or record['handoff_next_at']>time.time()):return
            try:
                job_id=self.enqueue_render(record)
                self.update(record['id'],job_id=job_id,error=None,error_code=None,handoff_next_at=0)
            except Exception as exc:
                try:(self.root/record['id']/'handoff-error.log').write_text(traceback.format_exc(),encoding='utf-8')
                except OSError:pass
                attempts=record['handoff_attempts']+1
                delay=HANDOFF_RETRY_SECONDS[min(attempts-1,len(HANDOFF_RETRY_SECONDS)-1)]
                status=getattr(exc,'status_code',None)
                code='handoff_conflict' if status in (400,404,409,422) else 'handoff_unavailable'
                detail='制作资料需要重新检查，请重新编排。' if code=='handoff_conflict' else (
                    '稍后会自动重试。' if attempts<HANDOFF_MAX_ATTEMPTS else '请点击精剪重试。')
                self.update(record['id'],error='编排已完成，但成片任务尚未提交。'+detail,error_code=code,
                            handoff_attempts=attempts,handoff_next_at=time.time()+delay)

    def retry_handoff(self, identifier):
        """Synchronous intent update only; the worker performs the actual submit."""
        record=self.get(str(identifier))
        if record is None:raise HTTPException(404,'编排方案不存在。')
        if record['status']=='ready' and record['request'].get('auto_render') and not record.get('job_id'):
            self.update(record['id'],handoff_attempts=0,handoff_next_at=0,error=None,error_code=None)
            self.wake_event.set()
            return self.get(record['id'])
        return record

    def retry_ready_handoffs(self):
        with closing(self.connect()) as db:
            identifiers=[row['id'] for row in db.execute(
                "SELECT id FROM assemblies WHERE status='ready' AND job_id IS NULL AND handoff_attempts<? AND handoff_next_at<=?",
                (HANDOFF_MAX_ATTEMPTS,time.time()))]
        for identifier in identifiers:
            if self.stop_event.is_set():return
            self.handoff(self.get(identifier))

    def start(self):
        if self.thread and self.thread.is_alive():return
        self.stop_event.clear()
        with closing(self.connect()) as db:
            db.execute("UPDATE assemblies SET status='queued',stage='queued' WHERE status='running'");db.commit()
        self.thread=threading.Thread(target=self.run,name='cutflow-assembly',daemon=True);self.thread.start()

    def stop(self):
        self.stop_event.set();self.wake_event.set()
        if self.thread:self.thread.join(timeout=2)

    def run(self):
        # Complete an interrupted handoff exactly once through the render API's
        # deterministic idempotency key, even after a crash between DB writes.
        while not self.stop_event.is_set():
            self.retry_ready_handoffs()
            with closing(self.connect()) as db:
                row=db.execute("SELECT id FROM assemblies WHERE status='queued' ORDER BY created_at,id LIMIT 1").fetchone()
            if not row:
                self.wake_event.wait(1);self.wake_event.clear();continue
            with PIPELINE_LOCK:
                if self.stop_event.is_set():break
                self.update(row['id'],status='running',stage='validating')
                self.process(self.get(row['id']))
