"""Durable material-to-delivery production with immutable, recoverable checkpoints.

The browser submits intent once.  Every expensive phase is committed separately;
recovery never uses an uncommitted render as a successful delivery.  No external
publication is performed here.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import threading
import time
import traceback
from uuid import UUID, uuid4
import zipfile

from fastapi import HTTPException

from .assembly_service import PIPELINE_LOCK

STAGES = {
    'queued': '等待制作', 'preflight': '检查素材与制作条件',
    'understanding': '理解画面与原声', 'planning': '选择主题与内容方案',
    'scripting': '确定脚本与分镜', 'editing': '匹配镜头与声音',
    'preparing_voice': '准备独白与声音', 'rendering': '渲染成片', 'quality': '检查实际成片',
    'repair': '修复并重新检查', 'delivery': '整理交付文件',
    'completed': '制作完成', 'blocked': '需要处理制作条件', 'failed': '制作未完成',
}
MAX_ATTEMPTS = 3
MAX_REPAIRS = 1
MAX_SECONDS = 3600
USAGE_HEARTBEAT_SECONDS = 5
TRANSIENT = {'analysis_unavailable', 'model_unavailable', 'vision_unavailable',
             'narration_failed', 'render_failed', 'service_unavailable'}
BLOCKING = {'material_shortfall', 'no_usable_material', 'insufficient_evidence',
            'source_changed', 'source_unavailable', 'dependency_unavailable',
            'unsupported_mode', 'unsupported_revision', 'preflight_failed', 'evidence_shortfall', 'brand_media_unavailable'}
ERRORS = {
    'source_changed': '所选素材发生变化，已保留内容方案，请重新选择素材后制作。',
    'source_unavailable': '部分素材已不可读，已保留任务，请恢复素材或重新选择。',
    'checkpoint_changed': '已保存的制作资料校验不一致，已停止使用该资料。',
    'preflight_failed': '制作条件未全部就绪，请一次处理下方列出的项目后重试。',
    'quality_failed': '成片未通过关键质量检查，尚未标为制作完成。',
    'delivery_incomplete': '交付文件不完整，已保留可用制作结果。',
    'deadline_exceeded': '本次制作已达到最长处理时间，已保存中间成果。',
    'production_failed': '制作未完成，已有素材和中间成果已保留。',
}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def _file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path, value):
    temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        stream.write(_json(value)); stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path)


class DirectorServiceError(RuntimeError):
    def __init__(self, code, message=None, details=None):
        self.code = code
        self.details = details or []
        super().__init__(message or ERRORS.get(code, ERRORS['production_failed']))


class _BudgetStop:
    def __init__(self, stop_event, deadline):
        self.event, self.deadline = stop_event, deadline

    def is_set(self):
        return self.event.is_set() or time.monotonic() >= self.deadline

    def wait(self, timeout=None):
        return self.event.wait(min(timeout or 1, max(0, self.deadline - time.monotonic()))) or self.is_set()


def public_plan(plan):
    if not isinstance(plan, dict):
        return None
    fields = ('title', 'theme', 'viewpoint', 'core_viewpoint', 'audience_value', 'audience',
              'purpose', 'mode', 'style', 'target_duration', 'actual_duration', 'actual_script',
              'structure', 'scenes', 'candidates', 'selected_direction', 'selection_reason',
              'limitations', 'warnings', 'gaps', 'evidence', 'script_sha256', 'plan_sha256',
              'brand_context_revision', 'revision', 'publication_copy', 'posting_copy', 'content_check', 'content_review',
              'parent_plan_sha256','revision_patch','fact_ledger','brand_facts','brand_media')
    result = {key: copy.deepcopy(plan[key]) for key in fields if key in plan}
    # Raw image payloads and filesystem implementation details never belong in
    # the content review card. Source indexes and time ranges remain available.
    def clean(value):
        if isinstance(value, list): return [clean(v) for v in value]
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items()
                    if k not in {'path', 'output_path', 'image_url', 'base64', 'frame_path', 'render_plan'}}
        return value
    return clean(result)


def _presentation_only(instruction, before, after, old_mode, new_mode):
    """Only exact presentation instructions can bypass a new editorial plan."""
    if old_mode != new_mode: return False
    changed = {key for key in set(before) | set(after) if before.get(key) != after.get(key)}
    if not changed or not changed <= {'aspect','subtitles','music'}: return False
    phrases = (r'(?:请|帮我|麻烦|只|仅)?(?:不要|关闭|去掉|移除|不加|加上|打开|保留|添加)(?:背景)?(?:字幕|音乐|配乐)',
               r'(?:请|帮我|只|仅)?(?:改成|换成|改为|调整为)(?:横屏|竖屏|方形|9:16|16:9|1:1)(?:视频|画幅)?')
    residue = instruction
    for phrase in phrases: residue = re.sub(phrase, '', residue)
    return not re.sub(r'[\s，。；、,.;！!和并且即可就好]+', '', residue)


class DirectorService:
    def __init__(self, data, inbox, *, validate_source, source_allowed=None,
                 brand_snapshot=None, settings_loader=None, preflight=None,
                 analyzer=None, planner=None, renderer=None, quality_checker=None,
                 repairer=None, deliverer=None, supported_modes=None, reviser=None):
        self.data, self.inbox = Path(data), Path(inbox)
        self.data.mkdir(parents=True, exist_ok=True)
        self.root = self.data / 'productions'; self.root.mkdir(exist_ok=True)
        self.db = self.data / 'productions.sqlite3'
        self.validate_source = validate_source
        self.source_allowed = source_allowed or (lambda source: True)
        self.brand_snapshot = brand_snapshot or (lambda query: {})
        self.settings_loader = settings_loader or (lambda: {})
        self.preflight_check, self.analyzer, self.planner = preflight, analyzer, planner
        self.reviser = reviser
        self.renderer, self.quality_checker, self.repairer = renderer, quality_checker, repairer
        self.deliverer = deliverer
        self.supported_modes = set(supported_modes or ('auto','vlog','story','narrated'))
        self.stop_event = threading.Event(); self.wake_event = threading.Event(); self.thread = None
        self.process_lock = threading.Lock()
        with closing(self.connect()) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''CREATE TABLE IF NOT EXISTS productions(
                id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, request_json TEXT NOT NULL,
                snapshot_json TEXT NOT NULL, status TEXT NOT NULL, stage TEXT NOT NULL,
                checkpoints_json TEXT NOT NULL DEFAULT '{}', preflight_json TEXT,
                plan_json TEXT, quality_json TEXT, artifacts_json TEXT, error_json TEXT,
                attempts INTEGER NOT NULL DEFAULT 0, repair_attempts INTEGER NOT NULL DEFAULT 0,
                elapsed_seconds REAL NOT NULL DEFAULT 0, started_at REAL,
                next_run REAL NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS production_events(
                id INTEGER PRIMARY KEY AUTOINCREMENT, production_id TEXT NOT NULL,
                stage TEXT NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS production_retries(
                request_id TEXT PRIMARY KEY, production_id TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS production_status ON productions(status,created_at);
            '''); db.commit()

    def connect(self):
        db = sqlite3.connect(self.db, timeout=20); db.row_factory = sqlite3.Row
        return db

    def get(self, identifier):
        with closing(self.connect()) as db:
            row = db.execute('SELECT * FROM productions WHERE id=?', (str(identifier),)).fetchone()
        if row is None: return None
        item = dict(row)
        for key in ('request', 'snapshot', 'checkpoints', 'preflight', 'plan', 'quality', 'artifacts', 'error'):
            item[key] = json.loads(item.pop(key + '_json')) if item[key + '_json'] else None
        return item

    def list(self, limit=40):
        with closing(self.connect()) as db:
            ids = [r[0] for r in db.execute('SELECT id FROM productions ORDER BY created_at DESC,id DESC LIMIT ?',
                                           (min(100, max(1, limit)),))]
        return [self.get(identifier) for identifier in ids]

    def update(self, identifier, **values):
        allowed = {'status', 'stage', 'checkpoints', 'preflight', 'plan', 'quality', 'artifacts', 'error',
                   'attempts', 'repair_attempts', 'elapsed_seconds', 'started_at', 'next_run'}
        if not set(values) <= allowed: raise ValueError('invalid update')
        encoded = {(key + '_json' if key in {'checkpoints','preflight','plan','quality','artifacts','error'} else key):
                   (_json(value) if key in {'checkpoints','preflight','plan','quality','artifacts','error'} and value is not None else value)
                   for key, value in values.items()}
        encoded['updated_at'] = _now()
        with closing(self.connect()) as db:
            db.execute('UPDATE productions SET ' + ','.join(key + '=?' for key in encoded) + ' WHERE id=?',
                       (*encoded.values(), str(identifier))); db.commit()

    def event(self, identifier, stage, message=None):
        with closing(self.connect()) as db:
            db.execute('INSERT INTO production_events(production_id,stage,message,created_at) VALUES(?,?,?,?)',
                       (str(identifier), stage, message or STAGES.get(stage, stage), _now())); db.commit()

    def public(self, record):
        request = record['request']
        with closing(self.connect()) as db:
            events = [dict(row) for row in db.execute(
                'SELECT stage,message,created_at FROM production_events WHERE production_id=? ORDER BY id DESC LIMIT 100',
                (record['id'],))][::-1]
        artifacts = []
        if record['status'] == 'completed':
            for item in record.get('artifacts') or []:
                kind = item['kind']
                artifacts.append({key: item[key] for key in ('kind','name','size','sha256') if key in item} | {
                    'url': f'/api/productions/{record["id"]}/video' if kind == 'video'
                    else f'/api/productions/{record["id"]}/artifacts/{kind}'})
        return {'id': record['id'], 'status': record['status'], 'stage': record['stage'],
                'stage_label': STAGES.get(record['stage'], record['stage']),
                'goal': request['goal'], 'mode': request['mode'],
                'title': (record.get('plan') or {}).get('title') or request.get('title') or '自动制作',
                'sources': [item['source'] for item in record['snapshot']['sources']],
                'scope': record['snapshot'].get('scope', {}), 'options': request['options'],
                'parent_id': request.get('parent_id'), 'revision': request.get('revision'),
                'revision_scope': record['snapshot'].get('revision_scope'),
                'created_at': record['created_at'], 'updated_at': record['updated_at'],
                'events': events, 'preflight': record['preflight'], 'content_plan': public_plan(record['plan']),
                'quality': record['quality'], 'artifacts': artifacts, 'error': record['error'],
                'attempts': record['attempts'], 'max_attempts': MAX_ATTEMPTS,
                'repair_attempts': record['repair_attempts'], 'elapsed_seconds': round(record['elapsed_seconds'], 1),
                'can_retry': (record['status'] in {'failed','blocked'} and record['attempts'] < MAX_ATTEMPTS
                              and record['elapsed_seconds'] < MAX_SECONDS
                              and (record.get('error') or {}).get('code') not in {'source_changed','checkpoint_changed','unsupported_revision'}),
                'can_revise': record['status'] in {'completed','failed','blocked'} and bool(record.get('plan')),
                'brand_context_revision': record['snapshot'].get('brand_context', {}).get('revision')}

    def _paths_snapshot(self, sources):
        result = []
        for source in sources:
            if not self.source_allowed(source):
                raise HTTPException(400, '技术测试素材不能用于正式作品。')
            try:
                path = self.validate_source(self.inbox, source); stat = path.stat()
            except (OSError, ValueError, HTTPException):
                raise HTTPException(400, '部分所选素材无法读取，请重新选择。') from None
            result.append({'source': source, 'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns,
                           'ctime_ns': stat.st_ctime_ns})
        return result

    def choose_sources(self, requested):
        if requested: return list(dict.fromkeys(requested)), {'selected': True, 'analysis_limit': 12}
        from .creative import VIDEO_SUFFIXES
        candidates = []
        # A bounded discovery window is explicitly reported; it is never called
        # an exhaustive library analysis. No symlink is allowed into the scan.
        scanned = 0
        for base, dirs, files in os.walk(self.inbox, followlinks=False):
            dirs[:] = [name for name in sorted(dirs) if not Path(base, name).is_symlink()]
            for name in sorted(files):
                path = Path(base, name)
                if path.suffix.lower() not in VIDEO_SUFFIXES or path.is_symlink(): continue
                scanned += 1
                source = path.relative_to(self.inbox).as_posix()
                if self.source_allowed(source):
                    try: candidates.append((path.stat().st_mtime_ns, source))
                    except OSError: pass
                if scanned >= 1000: break
            if scanned >= 1000: break
        candidates.sort(key=lambda item: (-item[0], item[1]))
        return [item[1] for item in candidates[:12]], {'selected': False, 'analysis_limit': 12,
                'library_available': len(candidates), 'scope_limited': len(candidates) > 12 or scanned >= 1000,
                'scan_limit': 1000}

    def create(self, identifier, request):
        identifier = str(UUID(str(identifier))); request = copy.deepcopy(request)
        fingerprint = _digest(request)
        previous = self.get(identifier)
        if previous:
            if previous['fingerprint'] != fingerprint: raise HTTPException(409, '此请求编号已用于其他制作需求。')
            return previous
        parent = None
        if request.get('parent_id'):
            parent = self.get(request['parent_id'])
            if not parent or not parent.get('plan'): raise HTTPException(409, '原作品还没有可继续修改的内容方案。')
            if parent['status'] not in {'completed','failed','blocked'}:
                raise HTTPException(409,'原作品仍在制作，请等待该版本保存完成后再修改。')
            try:self.validate_snapshot(parent)
            except DirectorServiceError as exc:raise HTTPException(409,str(exc)) from None
            try:locked_parent=self.load_checkpoint(parent,'content-plan')
            except DirectorServiceError as exc:raise HTTPException(409,str(exc)) from None
            if locked_parent!=parent['plan']:
                raise HTTPException(409,'原作品方案与保存的版本不一致，不能据此生成新版本。')
        sources, scope = self.choose_sources(request['sources'])
        if not sources and request['mode'] != 'presenter':
            raise HTTPException(400, '素材库中还没有可用视频，请先导入素材。')
        source_snapshot = self._paths_snapshot(sources)
        snapshot = {'sources': source_snapshot, 'scope': scope,
                    'brand_context': (copy.deepcopy(parent['snapshot'].get('brand_context',{})) if parent
                                      else self.brand_snapshot(request['goal'])), 'settings': self.settings_loader()}
        brand_changed=False
        if parent:
            snapshot['parent_plan'] = copy.deepcopy(parent['plan'])
            # Brand facts are part of the original version, not silently replaced
            # when the user merely asks to shorten a film or change its opening.
            snapshot['brand_context'] = copy.deepcopy(parent['snapshot'].get('brand_context', {}))
            snapshot['reuse_analysis_from'] = parent['id'] if source_snapshot == parent['snapshot']['sources'] else None
            snapshot['presentation_only'] = bool(snapshot['reuse_analysis_from'] and _presentation_only(
                request.get('revision') or '', parent['request']['options'], request['options'],
                parent['request']['mode'], request['mode']))
            brand_changed=any(request['options'].get(key,default)!=parent['request']['options'].get(key,default)
                              for key,default in (('brand_usage','auto'),('brand_screenshot_ids',[])))
            structural=(parent['plan'].get('mode')=='vlog' or
                        request['mode']!=parent['request']['mode'] or not snapshot['reuse_analysis_from'] or
                        request['options'].get('target_duration')!=parent['request']['options'].get('target_duration') or
                        brand_changed)
            snapshot['revision_scope']=('presentation_only' if snapshot['presentation_only'] else
                                        'structural' if structural else 'local_text')
            snapshot['parent_plan_sha256']=parent['plan'].get('plan_sha256') or _digest(parent['plan'])
        # A simple text revision uses the parent's exact frozen facts and images.
        # Explicit selection changes start a new image snapshot, never overwrite it.
        if not parent or brand_changed:
            from .director_brand import snapshot_brand_assets
            try:
                snapshot['brand_context']=snapshot_brand_assets(self.data,snapshot['brand_context'],
                    self.root/identifier,request['options'])
            except Exception as exc:
                if getattr(exc,'code',None) not in {'brand_media_unavailable','source_changed'}:raise
                snapshot['brand_error']={'code':getattr(exc,'code'),'message':str(exc),
                    'solution':'请在知识库确认对应事实与小程序截图，重新提交本次制作。'}
        now = _now()
        with closing(self.connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT fingerprint FROM productions WHERE id=?', (identifier,)).fetchone()
            if prior:
                if prior['fingerprint'] != fingerprint: raise HTTPException(409, '此请求编号已用于其他制作需求。')
            else:
                count = db.execute("SELECT count(*) FROM productions WHERE status IN ('queued','running')").fetchone()[0]
                if count >= 3: raise HTTPException(429, '已有三条作品等待或正在制作，请稍后再提交。')
                db.execute('''INSERT INTO productions(id,fingerprint,request_json,snapshot_json,status,stage,
                           created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)''',
                           (identifier, fingerprint, _json(request), _json(snapshot), 'queued', 'queued', now, now))
                db.execute('INSERT INTO production_events(production_id,stage,message,created_at) VALUES(?,?,?,?)',
                           (identifier, 'queued', '需求和素材版本已保存，准备自动制作。', now))
            db.commit()
        self.wake_event.set()
        return self.get(identifier)

    def validate_snapshot(self, record):
        from .director_brand import validate_frozen_assets
        try:validate_frozen_assets(record['snapshot'].get('brand_context',{}),self.root)
        except Exception as exc:raise DirectorServiceError('source_changed',details=[{'message':str(exc)}]) from None
        paths = []
        for item in record['snapshot']['sources']:
            if not self.source_allowed(item['source']): raise DirectorServiceError('source_unavailable')
            try:
                path = self.validate_source(self.inbox, item['source']); stat = path.stat()
            except (ValueError, OSError, HTTPException): raise DirectorServiceError('source_unavailable') from None
            if any(item[key] != value for key, value in [('size',stat.st_size),('mtime_ns',stat.st_mtime_ns),('ctime_ns',stat.st_ctime_ns)]):
                raise DirectorServiceError('source_changed')
            paths.append(path)
        return paths

    def checkpoint(self, identifier, name, value):
        folder = self.root / identifier; folder.mkdir(exist_ok=True)
        path = folder / (name + '.json'); _atomic_json(path, value)
        record = self.get(identifier); checkpoints = record['checkpoints'] or {}
        checkpoints[name] = {'sha256': _file_hash(path), 'created_at': _now()}
        self.update(identifier, checkpoints=checkpoints)

    def load_checkpoint(self, record, name):
        checkpoint = (record['checkpoints'] or {}).get(name)
        if not checkpoint: return None
        path = self.root / record['id'] / (name + '.json')
        try:
            if _file_hash(path) != checkpoint['sha256']: raise DirectorServiceError('checkpoint_changed')
            return json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise DirectorServiceError('checkpoint_changed') from exc

    @staticmethod
    def analysis_current(analysis):
        from .director_media import ANALYSIS_VERSION, ANALYSIS_STRATEGY
        return (isinstance(analysis,dict) and analysis.get('analysis_version')==ANALYSIS_VERSION
                and analysis.get('analysis_strategy')==ANALYSIS_STRATEGY)

    def archive_outdated_analysis(self, record):
        """Preserve obsolete evidence before rebuilding an unfinished version."""
        if record['status']=='completed':
            raise DirectorServiceError('checkpoint_changed')
        folder=self.root/record['id'];history=folder/'history'/('analysis-'+uuid4().hex)
        history.mkdir(parents=True)
        names={'analysis','content-plan','render-result','quality'}
        for name in names & set(record['checkpoints'] or {}):
            value=self.load_checkpoint(record,name)  # Verify before archiving.
            _atomic_json(history/(name+'.json'),value)
            if name=='render-result':
                for key in ('output_path','srt_path'):
                    path=Path(value.get(key) or '')
                    if path.is_file() and path.resolve().is_relative_to(folder.resolve()):
                        shutil.copy2(path,history/(key+'-'+path.name))
        _atomic_json(history/'previous-state.json',{
            'archived_at':_now(),'reason':'analysis_version_upgrade','checkpoints':record['checkpoints'],
            'plan':record['plan'],'quality':record['quality'],'artifacts':record['artifacts']})
        self.update(record['id'],checkpoints={k:v for k,v in (record['checkpoints'] or {}).items() if k not in names},
                    plan=None,quality=None,artifacts=None)
        self.event(record['id'],'understanding','素材分析已升级，原分析及后续记录已归档；将重新核对画面后策划。')

    def _functions(self):
        from . import director_media, director_quality
        return (self.preflight_check or director_quality.preflight,
                self.analyzer or director_media.analyze_sources,
                self.planner or director_media.plan_content,
                self.renderer or director_media.render_content,
                self.quality_checker or director_quality.verify_delivery)

    def preflight(self, mode='auto', options=None, paths=None, active=False):
        if self.preflight_check is None:
            from .director_quality import preflight
        else: preflight = self.preflight_check
        folder = self.root / 'dependency-check'; folder.mkdir(exist_ok=True)
        return self._mode_report(preflight(paths or [], mode, options or {}, data=self.data, work_dir=folder, active=active),mode)

    def _mode_report(self, report, mode):
        if mode not in self.supported_modes:
            report=copy.deepcopy(report)
            item={'id':'production_mode','label':'制作方式','status':'blocked',
                  'detail':'此方式尚未接入自动策划的完整制作流程。',
                  'solution':'人物口播可继续使用原有人物口播入口；自动制作暂不替代人物选择与肖像授权。'}
            report.setdefault('checks',[]).append(item);report.setdefault('blockers',[]).append(item);report['ready']=False
        return report

    def _quality_plan(self, record, plan, analysis, render):
        return {**copy.deepcopy(plan), 'render_report': render, 'srt_path': render.get('srt_path'),
                'options': record['request']['options'], 'source_snapshots': record['snapshot']['sources'],
                'brand_assets_snapshot':copy.deepcopy(record['snapshot'].get('brand_context',{}).get('brand_assets',[])),
                'material_evidence': analysis, 'plan_sha256': plan.get('plan_sha256') or _digest(plan),
                'content_plan_snapshot_sha256': _digest(plan),
                'script_sha256': hashlib.sha256(str(plan.get('actual_script', '')).encode('utf-8')).hexdigest()}

    def process(self, record):
        # Public for deterministic recovery tests. The normal run loop also owns
        # PIPELINE_LOCK, shared with existing render/assembly workers.
        with self.process_lock:
            record = self.get(record['id'])
            if not record or record['status'] not in {'queued','running'}: return
            identifier = record['id']; request = record['request']; snapshot = record['snapshot']
            if record['attempts'] >= MAX_ATTEMPTS:
                self.update(identifier, status='failed', stage='failed', started_at=None,
                            error={'code':'attempts_exhausted','message':'本任务已达到处理次数上限，已有成果已保留。','details':[]})
                self.event(identifier,'failed','本任务已达到处理次数上限，已有成果已保留。')
                return
            folder = self.root / identifier; folder.mkdir(exist_ok=True)
            started = time.monotonic(); budget = MAX_SECONDS - record['elapsed_seconds']
            stopper = _BudgetStop(self.stop_event, started + max(0, budget))
            self.update(identifier, status='running', attempts=record['attempts'] + 1, started_at=time.time(), error=None)
            meter_stop = threading.Event()
            def persist_usage():
                # Persist only monotonic processing time. A wall-clock start
                # cannot distinguish work from hours with the server offline.
                with closing(self.connect()) as db:
                    db.execute('UPDATE productions SET elapsed_seconds=MAX(elapsed_seconds,?) WHERE id=?',
                               (record['elapsed_seconds'] + time.monotonic()-started, identifier))
                    db.commit()
            def meter():
                while not meter_stop.wait(USAGE_HEARTBEAT_SECONDS):
                    try: persist_usage()
                    except sqlite3.Error:
                        # A transient busy database must not kill accounting;
                        # the next heartbeat includes all time since this run.
                        continue
            meter_thread = threading.Thread(target=meter,name='cutflow-usage',daemon=True)
            meter_thread.start()
            def stage(value, message=None):
                if self.stop_event.is_set(): raise DirectorServiceError('cancelled')
                if stopper.is_set(): raise DirectorServiceError('deadline_exceeded')
                value = value if value in STAGES else {'analyzing':'understanding','voicing':'editing','compositing':'rendering','checking':'quality'}.get(value, 'editing')
                self.update(identifier, stage=value); self.event(identifier, value, message)
            try:
                if budget <= 0: raise DirectorServiceError('deadline_exceeded')
                if snapshot.get('brand_error'):
                    problem=snapshot['brand_error'];item={'id':'brand_assets','label':'企业知识与小程序截图',
                        'status':'blocked','detail':problem['message'],'solution':problem['solution']}
                    self.update(identifier,preflight={'ready':False,'checks':[item],'blockers':[item]})
                    raise DirectorServiceError('brand_media_unavailable',problem['message'],[item])
                paths = self.validate_snapshot(record)
                preflight, analyze, plan_fn, render_fn, verify = self._functions()
                processing_mode=request['mode']
                if request.get('parent_id') and processing_mode=='auto':
                    processing_mode=(snapshot.get('parent_plan') or {}).get('mode') or processing_mode
                options = {**request['options'], 'mode': processing_mode}
                stage('preflight')
                report = self._mode_report(preflight(paths, processing_mode, options, data=self.data, work_dir=folder/'preflight', active=True),processing_mode)
                self.update(identifier, preflight=report)
                if report.get('ready') is not True:
                    raise DirectorServiceError('preflight_failed', details=report.get('blockers') or report.get('checks') or [])
                analysis = self.load_checkpoint(self.get(identifier), 'analysis')
                analysis_upgraded=False
                if analysis is not None and not self.analysis_current(analysis):
                    self.archive_outdated_analysis(self.get(identifier))
                    analysis=None;analysis_upgraded=True
                if analysis is None and snapshot.get('reuse_analysis_from'):
                    parent = self.get(snapshot['reuse_analysis_from'])
                    if parent and parent['snapshot']['sources'] == snapshot['sources']:
                        self.validate_snapshot(parent); analysis = self.load_checkpoint(parent, 'analysis')
                        if analysis is not None and not self.analysis_current(analysis):
                            analysis=None;analysis_upgraded=True
                        if analysis is not None:
                            self.checkpoint(identifier, 'analysis', analysis)
                            self.event(identifier, 'understanding', '素材未变化，复用已验证的画面与原声分析。')
                if analysis is None:
                    stage('understanding')
                    (folder/'analysis').mkdir(exist_ok=True)
                    analysis = analyze(paths, data=self.data, work_dir=folder/'analysis', on_stage=stage, stop_event=stopper)
                    self.validate_snapshot(record); self.checkpoint(identifier, 'analysis', analysis)
                plan = self.load_checkpoint(self.get(identifier), 'content-plan')
                if plan is None and snapshot.get('presentation_only') and not analysis_upgraded:
                    parent=self.get(snapshot['reuse_analysis_from'])
                    old_plan=self.load_checkpoint(parent,'content-plan')
                    if old_plan is None or old_plan.get('content_check',{}).get('passed') is not True:
                        raise DirectorServiceError('checkpoint_changed')
                    plan=copy.deepcopy(old_plan);plan.pop('plan_sha256',None)
                    plan['options']={**plan.get('options',{}),**options}
                    plan['revision']={'instruction':request['revision'],'parent_id':parent['id'],
                                      'scope':'presentation_only','reused':'主题、事实、脚本与分镜保持原版本，更新画幅、字幕或配乐设置。'}
                    plan['plan_sha256']=_digest(plan)
                    self.checkpoint(identifier,'content-plan',plan)
                    self.event(identifier,'planning','只调整包装设置，复用已核查的主题、脚本和分镜。')
                if plan is None:
                    stage('planning')
                    (folder/'content').mkdir(exist_ok=True)
                    previous = snapshot.get('parent_plan') or {}
                    revision = {'instruction':request['revision'], 'parent_id':request.get('parent_id'),
                                'previous':{key:previous.get(key) for key in ('theme','viewpoint','mode','title','actual_script')},
                                'previous_scenes':[{key:scene.get(key) for key in ('role','visual_id','speech_ids','spoken_text','purpose')}
                                                   for scene in previous.get('scenes',[])]} if request.get('revision') else None
                    if request.get('parent_id') and snapshot.get('revision_scope')=='local_text':
                        parent=self.get(request['parent_id'])
                        if not parent or self.load_checkpoint(parent,'content-plan')!=previous:
                            raise DirectorServiceError('checkpoint_changed')
                        revise_fn=self.reviser
                        if revise_fn is None:
                            from .director_media import revise_content
                            revise_fn=revise_content
                        self.event(identifier,'planning','保留父版主题、镜头与素材依据，只修改本次要求影响的台词及相关说明。')
                        revision_options={**options,'mode':previous.get('options',{}).get('mode',request['mode'])}
                        plan=revise_fn(copy.deepcopy(previous),request['revision'],data=self.data,
                                       work_dir=folder/'content',options=revision_options,
                                       brand_context=copy.deepcopy(snapshot.get('brand_context',{})))
                    else:
                        plan = plan_fn(analysis, request['goal'], snapshot.get('brand_context', {}), options,
                                       data=self.data, work_dir=folder/'content', revision=revision)
                    if not isinstance(plan, dict) or not plan.get('scenes'):
                        raise DirectorServiceError('insufficient_evidence', '现有素材未形成可执行的内容方案，已保留素材分析。')
                    self.validate_snapshot(record); self.checkpoint(identifier, 'content-plan', plan)
                self.update(identifier, plan=plan); stage('scripting', '主题、脚本与分镜已统一锁定，后续制作使用同一版本。')
                render = self.load_checkpoint(self.get(identifier), 'render-result')
                if render is not None:
                    output = Path(render['output_path'])
                    if not output.is_file() or _file_hash(output) != render.get('output_sha256'):
                        raise DirectorServiceError('checkpoint_changed')
                else:
                    stage('editing'); (folder/'render').mkdir(exist_ok=True)
                    render_options = {'data':self.data,'work_dir':folder/'render',
                                      'settings':snapshot['settings'],'on_stage':stage,'stop_event':stopper}
                    unchanged_script=(request.get('parent_id') and plan.get('mode')!='vlog'
                        and plan.get('actual_script')==(snapshot.get('parent_plan') or {}).get('actual_script')
                        and plan.get('mode')==(snapshot.get('parent_plan') or {}).get('mode'))
                    if unchanged_script and snapshot.get('reuse_analysis_from'):
                        parent = self.get(snapshot['reuse_analysis_from'])
                        self.validate_snapshot(parent)
                        parent_render = self.load_checkpoint(parent,'render-result') or {}
                        prepared_voice = (parent_render.get('render_plan') or {}).get('narration')
                        if prepared_voice and request['options'].get('voice_style','warm')==parent['request']['options'].get('voice_style','warm'):
                            render_options['prepared_narration'] = copy.deepcopy(prepared_voice)
                            self.event(identifier,'editing','讲稿未变化，校验并复用原配音，重新合成包装。')
                    render = render_fn(plan, paths, **render_options)
                    output = Path(render['output_path'])
                    if not output.resolve().is_relative_to(folder.resolve()) or not output.is_file():
                        raise DirectorServiceError('delivery_incomplete')
                    render['output_sha256'] = _file_hash(output)
                    self.validate_snapshot(record); self.checkpoint(identifier, 'render-result', render)
                quality = self.load_checkpoint(self.get(identifier), 'quality')
                if quality is None:
                    stage('quality'); (folder/'quality').mkdir(exist_ok=True)
                    quality = verify(output, self._quality_plan(record,plan,analysis,render),
                                     work_dir=folder/'quality',data=self.data,render_result=render,
                                     paths=paths,options=options,stop_event=stopper)
                    self.checkpoint(identifier, 'quality', quality)
                self.update(identifier, quality=quality)
                if quality.get('passed') is not True:
                    from .director_quality import repair_delivery
                    repairer = self.repairer or repair_delivery
                    current = self.get(identifier)
                    if repairer and current['repair_attempts'] < MAX_REPAIRS and any(i.get('repairable') for i in quality.get('issues', [])):
                        stage('repair'); self.update(identifier, repair_attempts=current['repair_attempts'] + 1)
                        repaired = repairer(output, self._quality_plan(record,plan,analysis,render), quality,
                                            data=self.data, work_dir=folder/'repair', render_result=render,
                                            paths=paths, options=options, stop_event=stopper)
                        if repaired:
                            render = repaired; output = Path(render['output_path']); render['output_sha256'] = _file_hash(output)
                            self.checkpoint(identifier, 'render-result', render)
                            stage('quality')
                            quality = verify(output, self._quality_plan(record,plan,analysis,render), work_dir=folder/'quality-repair',
                                             data=self.data,render_result=render,paths=paths,options=options,stop_event=stopper)
                            self.checkpoint(identifier, 'quality', quality); self.update(identifier, quality=quality)
                    if quality.get('passed') is not True:
                        raise DirectorServiceError('quality_failed', details=quality.get('issues') or quality.get('checks') or [])
                stage('delivery'); self.validate_snapshot(record)
                deliver = self.deliverer or self._deliver
                artifacts = deliver(self.get(identifier), output, plan, render, quality, folder)
                if not {'video','subtitles','cover','copy','record','package'} <= {a['kind'] for a in artifacts}:
                    raise DirectorServiceError('delivery_incomplete')
                self.update(identifier, status='completed', stage='completed', artifacts=artifacts, error=None)
                self.event(identifier, 'completed', '成片与质量记录已保存，可播放、下载或继续修改。')
            except Exception as exc:
                (folder/'error.log').write_text(traceback.format_exc(), encoding='utf-8')
                current = self.get(identifier)
                code = getattr(exc, 'code', 'production_failed')
                if stopper.is_set() and not self.stop_event.is_set(): code = 'deadline_exceeded'
                if self.stop_event.is_set():
                    self.update(identifier, status='queued', stage='queued', error=None,
                                attempts=max(0,current['attempts']-1))
                    self.event(identifier, 'queued', '服务已暂停，已保存阶段成果，下次启动继续。')
                else:
                    # Provider exception strings may contain service URLs or paths;
                    # only our defined errors and media's deliberately safe errors surface.
                    safe = str(exc) if isinstance(exc, DirectorServiceError) or exc.__class__.__name__ == 'DirectorMediaError' else ERRORS.get(code, ERRORS['production_failed'])
                    details = getattr(exc, 'details', [])
                    error = {'code':code, 'message':safe[:800], 'details':details}
                    retry = code in TRANSIENT and current['attempts'] < MAX_ATTEMPTS
                    status = 'queued' if retry else ('blocked' if code in BLOCKING else 'failed')
                    self.update(identifier, status=status, stage='queued' if retry else status, error=error,
                                next_run=time.time() + (5 if current['attempts'] == 1 else 20) if retry else 0)
                    self.event(identifier, status, safe[:800] + (' 已保存成果，将进行有次数限制的重试。' if retry else ''))
            finally:
                meter_stop.set(); meter_thread.join()
                persist_usage()
                self.update(identifier, started_at=None)

    def _artifact(self, kind, path, name=None):
        path = Path(path)
        if not path.is_file() or path.stat().st_size <= 0: raise DirectorServiceError('delivery_incomplete')
        return {'kind':kind,'path':str(path),'name':name or path.name,'size':path.stat().st_size,'sha256':_file_hash(path)}

    def _cover_frame(self, record, plan, output_info, folder):
        """Pick a clean, traceable frame before any titles or subtitles exist."""
        from . import media
        from PIL import Image, ImageFilter, ImageStat, ImageOps
        if record.get('plan')!=plan:
            raise DirectorServiceError('checkpoint_changed')
        scenes=plan.get('scenes') or []
        if not scenes:raise DirectorServiceError('delivery_incomplete',details=[{'missing':'封面所需的首镜头来源'}])
        scene=scenes[0];index=scene.get('source_index')
        snapshots=record['snapshot']['sources']
        paths=self.validate_snapshot(record)
        width,height=int(output_info['width']),int(output_info['height'])
        if not (32<=width<=4096 and 32<=height<=4096):raise DirectorServiceError('delivery_incomplete')
        if scene.get('kind')=='brand_screenshot':
            # Resolve only the version's frozen record, never a model-supplied path.
            asset=next((row for row in record['snapshot'].get('brand_context',{}).get('brand_assets',[])
                        if row.get('id')==scene.get('brand_media_id')),None)
            if not asset or asset.get('sha256')!=(scene.get('brand_asset') or {}).get('sha256'):
                raise DirectorServiceError('checkpoint_changed')
            with Image.open(asset['path']) as original:
                picture=ImageOps.contain(original.convert('RGB'),(int(width*.9),int(height*.72)),Image.Resampling.LANCZOS)
            canvas=Image.new('RGB',(width,height),(18,42,37))
            canvas.paste(picture,((width-picture.width)//2,int(height*.23)+(int(height*.72)-picture.height)//2))
            cover=folder/'cover.png';canvas.save(cover)
            return cover,{'kind':'frozen_brand_screenshot','brand_media_id':asset['id'],
                          'image_sha256':asset['sha256'],'source_reference':asset.get('source_reference'),
                          'canvas':[width,height],'geometry':'fit_without_crop','title_layers':1,
                          'base_frame_sha256':_file_hash(cover),'inherits_finished_video_overlays':False}
        if isinstance(index,bool) or not isinstance(index,int) or not 0<=index<len(snapshots):
            raise DirectorServiceError('checkpoint_changed')
        source=paths[index]  # Never accept a path supplied in a plan/model field.
        info=media.inspect_media(source)
        try:start,end=float(scene['source_in']),float(scene['source_out'])
        except (KeyError,TypeError,ValueError):raise DirectorServiceError('checkpoint_changed') from None
        if not (math.isfinite(start) and math.isfinite(end) and 0<=start<end<=info['duration']+.001):
            raise DirectorServiceError('checkpoint_changed')
        color=media._hdr_workflow(info)
        filters=(media.HDR_TO_SDR_FILTER+',' if color else '')
        filters+=(f'scale=trunc(iw*sar/2)*2:ih,setsar=1,'
                  f'scale={width}:{height}:force_original_aspect_ratio=decrease,'
                  f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,format=rgb24')
        candidates=[]
        for number,fraction in enumerate((.25,.5,.75)):
            timestamp=start+(end-start)*fraction
            path=folder/f'cover-source-{number}.png'
            media._run([media._tool('ffmpeg'),'-v','error','-nostdin','-y',
                        *media.INPUT_OPTIONS,'-ss',f'{timestamp:.8f}','-i',str(source),
                        '-an','-vf',filters,'-frames:v','1',str(path)],60)
            with Image.open(path) as frame:
                gray=frame.convert('L');gray.thumbnail((320,320))
                # Remove letterbox bars before ranking actual scene detail.
                bounds=gray.getbbox()
                if bounds:gray=gray.crop(bounds)
                stats=ImageStat.Stat(gray)
                edges=gray.filter(ImageFilter.FIND_EDGES)
                if edges.width>4 and edges.height>4:edges=edges.crop((2,2,edges.width-2,edges.height-2))
                score=ImageStat.Stat(edges).var[0]
                candidates.append({'path':path,'source_time':timestamp,'clarity':score,'luminance':stats.mean[0],
                                   'usable':bool(bounds) and 4<stats.mean[0]<251,'center_distance':abs(fraction-.5)})
        usable=[item for item in candidates if item['usable']]
        if not usable:
            raise DirectorServiceError('delivery_incomplete',details=[{'missing':'首个已选镜头内没有可用的清晰封面帧'}])
        chosen=max(usable,key=lambda item:(item['clarity'],-item['center_distance']))
        cover=folder/'cover.png';shutil.copy2(chosen['path'],cover)
        self.validate_snapshot(record)
        return cover,{'kind':'selected_original_frame','source_index':index,'source':snapshots[index]['source'],
                      'source_in':start,'source_out':end,'source_time':round(chosen['source_time'],6),
                      'canvas':[width,height],'geometry':'fit_without_crop','color_workflow':color,
                      'candidates':[{key:round(item[key],6) for key in ('source_time','clarity','luminance')}
                                    for item in candidates],
                      'base_frame_sha256':_file_hash(cover),'title_layers':1,'inherits_finished_video_overlays':False}

    def _deliver(self, record, output, plan, render, quality, folder):
        from . import media, creative
        from PIL import Image, ImageDraw, ImageFont
        delivery = folder/'delivery'; delivery.mkdir(exist_ok=True)
        srt = Path(render.get('srt_path') or output.with_suffix('.srt'))
        if not srt.is_file() or not srt.read_text(encoding='utf-8').strip(): raise DirectorServiceError('delivery_incomplete')
        title = str(plan.get('title') or plan.get('theme') or '自动制作作品').strip()[:60]
        safe_title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', title).strip('. ') or '自动制作作品'
        info = media.inspect_media(output)
        cover,cover_source=self._cover_frame(record,plan,info,delivery)
        with Image.open(cover) as source:
            image=source.convert('RGB'); draw=ImageDraw.Draw(image)
            size=max(20,round(image.width*.045)); font=ImageFont.truetype(str(creative._font()),size)
            lines=[];line=''
            for char in title:
                if draw.textlength(line+char,font=font)>image.width*.84 and line:lines.append(line);line=''
                line+=char
            if line:lines.append(line)
            height=round(size*1.5)*len(lines)+size
            draw.rectangle((0,0,image.width,height),fill=(14,35,31))
            for i,line in enumerate(lines):draw.text((image.width*.08,size*.45+i*size*1.5),line,font=font,fill='white')
            image.save(cover)
        copy_text = str(plan.get('posting_copy') or plan.get('publication_copy') or plan.get('audience_value') or plan.get('theme') or '').strip()
        copy_path=delivery/'发布文案.txt';copy_path.write_text(title+'\n\n'+copy_text+'\n',encoding='utf-8')
        report = {'version_id':record['id'],'parent_id':record['request'].get('parent_id'),
                  'created_at':record['created_at'],'sources':record['snapshot']['sources'],
                  'brand_context_revision':record['snapshot'].get('brand_context',{}).get('revision'),
                  'content_plan':public_plan(plan),'quality':quality,'render_sha256':_file_hash(output),
                  'cover_source':cover_source,'publication_performed':False}
        record_path=delivery/'制作记录.json';_atomic_json(record_path,report)
        artifacts=[self._artifact('video',output,safe_title+'.mp4'),self._artifact('subtitles',srt,safe_title+'.srt'),
                   self._artifact('cover',cover,safe_title+'-封面.png'),self._artifact('copy',copy_path),self._artifact('record',record_path)]
        package=delivery/'交付包.zip';temp=package.with_suffix('.zip.tmp')
        with zipfile.ZipFile(temp,'w',compression=zipfile.ZIP_STORED,allowZip64=True) as archive:
            for item in artifacts:archive.write(item['path'],arcname=item['name'])
        os.replace(temp,package);artifacts.append(self._artifact('package',package,safe_title+'-交付包.zip'))
        return artifacts

    def artifact_path(self, identifier, kind):
        record = self.get(str(identifier))
        if not record: raise HTTPException(404, '作品不存在。')
        if record['status'] != 'completed': raise HTTPException(409, '作品还未通过检查，暂不作为成片交付。')
        item = next((value for value in record.get('artifacts') or [] if value['kind'] == kind), None)
        if not item: raise HTTPException(404, '此交付文件不存在。')
        path = Path(item['path'])
        if (not path.resolve().is_relative_to((self.root/record['id']).resolve()) or not path.is_file()
                or path.stat().st_size != item['size'] or _file_hash(path) != item['sha256']):
            raise HTTPException(409, '交付文件已经变化，请查看任务记录。')
        return path, item

    def retry(self, identifier, request_id):
        identifier, request_id = str(UUID(str(identifier))), str(UUID(str(request_id)))
        record=self.get(identifier)
        if not record: raise HTTPException(404,'制作任务不存在。')
        with closing(self.connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            prior=db.execute('SELECT production_id FROM production_retries WHERE request_id=?',(request_id,)).fetchone()
            if prior:
                if prior[0]!=identifier:raise HTTPException(409,'重试编号已用于其他作品。')
                return self.get(identifier)
            current=db.execute('SELECT status,attempts,elapsed_seconds,error_json FROM productions WHERE id=?',(identifier,)).fetchone()
            if current['status'] not in {'failed','blocked'}:return self.get(identifier)
            if current['attempts']>=MAX_ATTEMPTS:raise HTTPException(409,'本任务已达到重试上限；请根据保留的方案创建新版本。')
            if current['elapsed_seconds']>=MAX_SECONDS:raise HTTPException(409,'本任务已达到处理时间上限；请缩小范围后创建新版本。')
            error=json.loads(current['error_json']) if current['error_json'] else {}
            if error.get('code') in {'source_changed','checkpoint_changed','unsupported_revision'}:
                raise HTTPException(409,'素材或制作资料已经变化，请重新选择并创建新版本。')
            db.execute('INSERT INTO production_retries VALUES(?,?)',(request_id,identifier))
            db.execute("UPDATE productions SET status='queued',stage='queued',error_json=NULL,next_run=0,updated_at=? WHERE id=?",(_now(),identifier));db.commit()
        self.event(identifier,'queued','已保存重试请求，将复用完整的阶段成果。');self.wake_event.set()
        return self.get(identifier)

    def revise(self, identifier, request_id, instruction, options=None):
        record=self.get(str(identifier))
        if not record or not record.get('plan'):raise HTTPException(409,'原作品尚无可修改方案。')
        request=copy.deepcopy(record['request']);request['parent_id']=record['id'];request['revision']=instruction
        request['sources']=[item['source'] for item in record['snapshot']['sources']]
        request['options'].update(options or {})
        from .creative import _duration_intent
        duration=_duration_intent(instruction,[])
        if duration is not None:request['options']['target_duration']=min(180,max(5,duration))
        if re.search(r'保留.{0,6}(?:更多)?.*(?:现场原声|原声|原音)',instruction):request['mode']='vlog'
        if re.search(r'(?:不要|关闭|去掉)字幕',instruction):request['options']['subtitles']=False
        elif re.search(r'(?:加上|打开|保留)字幕',instruction):request['options']['subtitles']=True
        if re.search(r'(?:不要|关闭|去掉)(?:音乐|配乐)',instruction):request['options']['music']=False
        elif re.search(r'(?:加上|打开|保留)(?:音乐|配乐)',instruction):request['options']['music']=True
        for expression,aspect in [('横屏','16:9'),('竖屏','9:16'),('方形','1:1'),('16:9','16:9'),('9:16','9:16'),('1:1','1:1')]:
            if re.search(r'(?:改成|换成|改为|调整为)'+re.escape(expression),instruction):request['options']['aspect']=aspect
        return self.create(request_id,request)

    def recover(self):
        with closing(self.connect()) as db:
            rows=list(db.execute("SELECT id FROM productions WHERE status='running'"))
            for row in rows:
                # Heartbeats already contain cumulative processing time. Never
                # bill downtime as work, or erase usage/attempts on a restart.
                db.execute("UPDATE productions SET status='queued',stage='queued',started_at=NULL WHERE id=?",(row['id'],))
            db.commit()
        for row in rows:self.event(row['id'],'queued','服务恢复，继续使用已保存并校验的阶段成果。')

    def start(self):
        if self.thread and self.thread.is_alive():return
        self.stop_event.clear();self.recover()
        self.thread=threading.Thread(target=self.run,name='cutflow-director',daemon=True);self.thread.start()

    def stop(self):
        self.stop_event.set();self.wake_event.set()
        if self.thread:self.thread.join(timeout=2)

    def run(self):
        while not self.stop_event.is_set():
            with closing(self.connect()) as db:
                row=db.execute("SELECT id FROM productions WHERE status='queued' AND next_run<=? ORDER BY created_at,id LIMIT 1",(time.time(),)).fetchone()
            if not row:self.wake_event.wait(1);self.wake_event.clear();continue
            with PIPELINE_LOCK:
                if not self.stop_event.is_set():self.process(self.get(row['id']))
