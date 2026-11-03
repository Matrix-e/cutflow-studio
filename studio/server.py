"""Authenticated editing API and durable single-process worker.

Run one Uvicorn process per data directory. Source media stays in the configured
inbox and finished files stay in data/renders; the web frontend never gets keys.
"""
from __future__ import annotations

import hashlib
import hmac
import importlib
import json
import os
import re
import shutil
import subprocess
import threading
import unicodedata
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from . import __version__
from .store import DiagnosticSourceConflict, StateConflict, Store
from .collection_service import register_collection_routes
from .profiles import display_name, import_mode
from .preview import PreviewManager, PreviewError
from .ideas_service import register_ideas_routes
from .scripts_service import BrandDetails, register_script_routes
from .presenters_service import PresenterLibrary, register_presenter_routes
from .progress import progress_snapshot, STAGE_MESSAGES
from .knowledge import register_knowledge_routes

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MEDIA_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mts"}
MEDIA_TYPES = {".mp4": "video/mp4", ".mov": "video/quicktime", ".mkv": "video/x-matroska",
               ".webm": "video/webm", ".avi": "video/x-msvideo", ".m4v": "video/mp4", ".mts": "video/mp2t"}
EDIT_SETTINGS = {"silence_min", "silence_db", "padding", "min_clip", "target_aspect", "fps", "encoder", "crf",
                 "normalize_audio", "remove_silence", "source_ranges", "width", "height", "srt_path"}


def video_download_name(title: str) -> str:
    """Readable download metadata only; never changes the stored render path."""
    stem = "".join(char for char in title if char not in '<>:"/\\|?*'
                   and not unicodedata.category(char).startswith("C")).strip(" .")
    # Bound both Windows characters and UTF-8 bytes for other download targets.
    stem = stem[:100].encode("utf-8")[:180].decode("utf-8", errors="ignore").rstrip(" .")
    if not stem or re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³]",
                                stem.split(".")[0].rstrip(" "), re.IGNORECASE):
        stem = "视频成片"
    return stem + ".mp4"


def detect_gpu() -> dict:
    """Bounded, best-effort GPU inventory; callers cache this at app startup."""
    unavailable = {"available": False, "name": None, "memory_mb": None}
    executable = shutil.which("nvidia-smi")
    if not executable:
        return unavailable
    try:
        response = subprocess.run([executable,"--query-gpu=name,memory.total","--format=csv,noheader,nounits"],
                                  capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=3,
                                  creationflags=0x08000000 if os.name == "nt" else 0)
        if response.returncode != 0:
            return unavailable
        name, memory = response.stdout.strip().splitlines()[0].rsplit(",",1)
        memory_mb = int(float(memory.strip()))
        if not name.strip() or memory_mb <= 0:
            return unavailable
        return {"available":True,"name":name.strip(),"memory_mb":memory_mb}
    except (OSError,subprocess.TimeoutExpired,ValueError,IndexError,OverflowError):
        return unavailable


class NewJob(BaseModel):
    source: str = Field(min_length=1, max_length=4096)
    title: str = Field(default="", max_length=160)
    settings: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)


class CreationOptions(BaseModel):
    model_config = ConfigDict(extra='forbid')
    target_duration: float | None = Field(default=None, ge=1, le=3600, allow_inf_nan=False)
    aspect: Literal['original', '16:9', '9:16', '1:1'] = 'original'
    keep_original_audio: StrictBool = True
    pace: Literal['preserve', 'gentle', 'compact'] = 'preserve'
    opening_title: str = Field(default='', max_length=120)
    music: StrictBool = False
    subtitles: StrictBool = True
    voice_style: Literal['professional', 'warm', 'energetic'] = 'professional'
    source_audio: Literal['mute', 'low'] = 'mute'

    @field_validator('target_duration', mode='before')
    @classmethod
    def numeric_duration(cls, value):
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise ValueError('目标时长必须是秒数')
        return value


class PresenterChoice(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['public', 'personal']
    portrait_id: str = Field(min_length=1, max_length=120, pattern=r'^[A-Za-z0-9_-]+$')
    background_id: str | None = Field(default=None, max_length=120, pattern=r'^[A-Za-z0-9_-]+$')
    outfit_id: str | None = Field(default=None, max_length=120, pattern=r'^[A-Za-z0-9_-]+$')
    consent_confirmed: StrictBool = False


class NewCreation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    sources: list[Annotated[str, Field(min_length=1, max_length=4096)]] = Field(min_length=0, max_length=12)
    brief: str = Field(min_length=1, max_length=3000)
    title: str = Field(default='', max_length=160)
    options: CreationOptions = Field(default_factory=CreationOptions)
    idempotency_key: str = Field(min_length=1, max_length=200)
    mode: Literal['edit', 'talking_head', 'narrated_promo', 'brand_promo'] = 'edit'
    spoken_script: str = Field(default='', max_length=1500)
    voice_id: str = Field(default='', max_length=120)
    visual_fill: Literal['reject', 'loop'] = 'reject'
    brand: BrandDetails = Field(default_factory=BrandDetails)
    subtype: Literal['corporate', 'salon_promo', 'salon_recap'] = 'salon_promo'
    presenter: PresenterChoice | None = None
    script_id: UUID | None = None
    parent_job_id: str | None = Field(default=None, pattern=r'^[a-f0-9]{32}$')
    revision_kind: Literal['rewrite', 'presenter', 'remix'] | None = None
    reference_ids: list[UUID] = Field(default_factory=list, max_length=3)

    @field_validator('brief', 'idempotency_key')
    @classmethod
    def nonempty_text(cls, value):
        value = value.strip()
        if not value or '\x00' in value:
            raise ValueError('想法和请求编号不能为空')
        return value

    @field_validator('spoken_script', 'voice_id')
    @classmethod
    def clean_spoken_text(cls, value):
        if any(ord(char) < 32 and char not in '\n\r\t' for char in value):
            raise ValueError('讲稿与音色不能包含无效控制字符')
        return value.strip()

    @model_validator(mode='after')
    def mode_contract(self):
        if bool(self.parent_job_id) != bool(self.revision_kind):
            raise ValueError('新版本需要完整的原作品记录和修改类型')
        added_options = {'subtitles', 'voice_style', 'source_audio'} & self.options.model_fields_set
        if self.mode == 'edit':
            if not self.sources:
                raise ValueError('请选择 1–12 段视频素材')
            if (self.spoken_script or self.voice_id or self.presenter or self.script_id or added_options
                    or any(self.brand.model_dump().values()) or self.visual_fill != 'reject'):
                raise ValueError('配音与品牌设置需要选择对应的宣传制作模式')
        elif self.mode in {'narrated_promo', 'brand_promo'}:
            if not self.spoken_script or not self.voice_id:
                raise ValueError('请先准备并确认讲稿、选择音色，再生成视频')
            if self.presenter:
                raise ValueError('人物形象只能用于人物口播模式')
            if 'pace' in self.options.model_fields_set and self.options.pace != 'preserve':
                raise ValueError('新旁白按完整讲稿生成，不使用原声静音裁剪节奏')
        elif self.mode == 'talking_head':
            if self.sources or not self.spoken_script or not self.voice_id or not self.presenter:
                raise ValueError('人物口播需要人物形象和完整讲稿，不使用原片素材')
            if self.presenter.background_id or self.presenter.outfit_id:
                raise ValueError('请选择已经保存的整套形象，独立换衣和换背景尚未接通')
            if not self.presenter.consent_confirmed:
                raise ValueError('请先确认人物肖像授权')
            if self.visual_fill != 'reject' or self.options.source_audio != 'mute':
                raise ValueError('人物口播不能循环嘴型或叠加第二条人声')
            if len(self.spoken_script) > 450:
                raise ValueError('人物口播请将讲稿控制在 450 字内；当前支持最长 120 秒')
        return self


def production_capabilities(script_supported=False):
    reason = '本地配音模型尚未就绪。'
    state, voices = {}, []
    try:
        provider = importlib.import_module('studio.narration')
        state = provider.configuration_status()
        directory = provider.list_voices()
        for item in directory:
            if (isinstance(item, dict) and isinstance(item.get('id'), str) and item['id']
                    and isinstance(item.get('name'), str) and isinstance(item.get('language'), str)):
                voices.append({key: item[key] for key in ('id', 'name', 'language')})
        ready = state.get('configured') is True and bool(voices)
        reason = None if ready else '本地配音模型或中文音色目录尚未就绪。'
    except (ImportError, OSError, ValueError, AttributeError, TypeError):
        ready = False
    person = importlib.import_module('studio.presenter').configuration_status()
    return {'modes': {'edit': {'ready': True, 'reason': None},
                     'narrated_promo': {'ready': ready, 'reason': reason},
                     'brand_promo': {'ready': ready, 'reason': reason},
                     'talking_head': {'ready': ready and person.get('configured') is True,
                         'reason': reason if not ready else person.get('reason')}},
            'voices': voices, 'script_supported': bool(script_supported),
            'narration_provider': state.get('provider'), 'narration_model': state.get('model')}


def safe_source(inbox: Path, relative: str) -> Path:
    # Reject Windows drive paths even when the server runs on Linux.
    if "\x00" in relative or re.match(r"^[a-zA-Z]:", relative):
        raise ValueError("素材路径必须相对于投递箱")
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts or relative.startswith("\\"):
        raise ValueError("素材路径不能越过投递箱")
    base = inbox.resolve()
    resolved = (base / candidate).resolve()
    if not resolved.is_relative_to(base) or resolved == base:
        raise ValueError("素材路径不能越过投递箱")
    if not resolved.is_file():
        raise ValueError("素材不存在，或仍未同步完成")
    if resolved.suffix.lower() not in MEDIA_EXTENSIONS:
        raise ValueError("暂不支持这种素材格式")
    return resolved


def job_source_snapshots(job: dict) -> list[dict]:
    creation = (job.get('settings') or {}).get('creation')
    if creation is not None:
        if isinstance(creation, dict) and creation.get('version') == 2 and creation.get('mode') == 'talking_head':
            if creation.get('sources') != [] or not isinstance(creation.get('portrait_snapshot'), dict):
                raise ValueError('人物形象记录不完整，请重新创建任务')
            return []
        if (not isinstance(creation, dict) or creation.get('version') not in {1, 2}
                or not isinstance(creation.get('sources'), list) or not 1 <= len(creation['sources']) <= 12):
            raise ValueError('创作任务的素材记录不完整，请重新创建任务')
        return creation['sources']
    return [{'source': job['source'], 'size': job['source_size'], 'mtime_ns': job['source_mtime_ns']}]


def validate_job_sources(job: dict, inbox: Path, store: Store) -> list[Path]:
    paths = []
    creation = (job.get('settings') or {}).get('creation')
    if isinstance(creation, dict) and creation.get('version') == 2 and creation.get('mode') == 'talking_head':
        job_source_snapshots(job)
        try:
            library = PresenterLibrary(store.path.parent)
            old = creation['portrait_snapshot']
            now = library.snapshot(old['id'])
            if now != old or not now['consent_confirmed'] or not now['face_validated']:
                raise ValueError('人物形象记录已变化，请重新选择后生成')
            library.resolve(now['id'])
        except (HTTPException, KeyError) as exc:
            raise ValueError('人物形象不可用，请重新选择已验证的形象') from exc
        return []
    diagnostics = store.technical_test_sources()
    for snapshot in job_source_snapshots(job):
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get('source'), str):
            raise ValueError('创作任务的素材记录不完整，请重新创建任务')
        path = safe_source(inbox, snapshot['source'])
        if path.relative_to(inbox).as_posix() in diagnostics:
            raise DiagnosticSourceConflict('所选素材包含已登记的技术测试，不能用于正式创作')
        stat = path.stat()
        if ((stat.st_size, stat.st_mtime_ns) != (snapshot.get('size'), snapshot.get('mtime_ns'))
                or 'ctime_ns' in snapshot and stat.st_ctime_ns != snapshot['ctime_ns']):
            raise ValueError('素材已变化，请新建任务以保留原有剪辑记录：' + display_name(snapshot['source']))
        paths.append(path)
    return paths


def creation_plan_for_ui(job: dict, creation: dict) -> dict:
    plan = job.get('plan') or creation.get('interpretation') or {}
    interpretation = creation.get('interpretation') or {}
    warnings = list(dict.fromkeys([*interpretation.get('warnings', []), *plan.get('warnings', []),
                                   *(job.get('result') or {}).get('warnings', [])]))
    result = {'resolved': plan.get('resolved', interpretation.get('resolved', {})),
              'summary': plan.get('summary', interpretation.get('summary', '')),
              'warnings': warnings, 'source_count': len(creation['sources']),
              'stage': 'planned' if job.get('plan') else 'interpreted'}
    if 'duration' in plan:
        result['duration'] = plan['duration']
    return result


def retry_block_reason(job: dict, inbox: Path, store: Store) -> str | None:
    """One authoritative check shared by UI capability hints and retry writes."""
    if job["job_kind"] == "technical_test":
        return "技术测试记录不能作为正式视频重新剪辑"
    if job["status"] != "failed":
        return "只有失败的剪辑任务可以重新排队"
    if job.get("publication") is not None or any(event["status"] == "publishing" for event in store.events(job["id"])):
        return "发布失败或发送结果未知的任务不能用此按钮重试，请先核实企业微信"
    try:
        validate_job_sources(job, inbox, store)
    except (ValueError, OSError) as exc:
        return str(exc) if isinstance(exc, ValueError) else "素材暂时无法读取，请检查投递箱"
    return None


def public_job(job: dict, *, inbox: Path, store: Store) -> dict:
    # Do not disclose machine paths through results; relative inbox path is enough.
    result = {key: value for key, value in job.items()
              if key not in {"output_path", "fingerprint", "source_size", "source_mtime_ns"}}
    result["has_video"] = bool(job.get("output_path") and Path(job["output_path"]).is_file())
    result["video_url"] = f"/api/jobs/{job['id']}/video" if result["has_video"] else None
    result["retry_block_reason"] = retry_block_reason(job, inbox, store)
    result["can_retry"] = result["retry_block_reason"] is None
    result["preview_supported"] = True
    result['progress_detail'] = progress_snapshot(job, store.events(job['id']), store.queue_position(job['id']))
    creation = (job.get('settings') or {}).get('creation')
    if isinstance(creation, dict):
        result['brief'] = creation['brief']
        result['sources'] = [entry['source'] for entry in creation['sources']]
        result['creation_plan'] = creation_plan_for_ui(job, creation)
        result['creation_warnings'] = result['creation_plan']['warnings']
        result['mode'] = creation.get('mode', 'edit')
        result['parent_job_id'] = creation.get('parent_job_id')
        result['revision_kind'] = creation.get('revision_kind')
        result['reference_works'] = creation.get('reference_works', [])
        if creation.get('version') == 2:
            for key in ('spoken_script', 'voice_id', 'brand', 'subtype', 'visual_fill', 'script_id'):
                result[key] = creation.get(key)
    return result


class Worker:
    def __init__(self, store: Store, inbox: Path, data: Path):
        self.store, self.inbox, self.data = store, inbox, data
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.lock_file = None

    def start(self):
        # OS locks disappear after a crash, unlike persistent pid-marker files.
        self.lock_file = (self.data / "worker.lock").open("a+b")
        try:
            if os.fstat(self.lock_file.fileno()).st_size == 0:
                self.lock_file.write(b"0")
                self.lock_file.flush()
            self.lock_file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.lock_file.close()
            self.lock_file = None
            raise RuntimeError("同一数据目录已有剪辑服务运行，请只启动一个服务进程") from exc
        self.store.recover()
        self.thread = threading.Thread(target=self.run, name="cutflow-render", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.wake_event.set()
        if self.thread:
            self.thread.join(timeout=2)

    def run(self):
        try:
            while not self.stop_event.is_set():
                job = self.store.claim()
                if job is None:
                    self.wake_event.wait(timeout=1)
                    self.wake_event.clear()
                    continue
                self.process(job)
        finally:
            if self.lock_file:
                self.lock_file.close()
                self.lock_file = None

    def process(self, job: dict):
        def stage(value):
            current = self.store.get(job['id'])
            if current is None or current['status'] not in {'analyzing', 'rendering'}:
                raise StateConflict('制作任务状态已变化')
            if (current.get('production') or {}).get('stage') == value:
                return
            self.store.transition(job['id'], [current['status']], current['status'], STAGE_MESSAGES[value],
                production={**(current.get('production') or {}), 'stage': value, 'error_code': None})
        try:
            paths = validate_job_sources(job, self.inbox, self.store)
            settings = dict(job["settings"])
            creation = settings.pop('creation', None)
            narrated = creation is not None and creation.get('version') == 2
            if narrated:
                if creation.get('mode') not in {'narrated_promo', 'brand_promo', 'talking_head'}:
                    raise ValueError('此制作模式尚未接通，不能创建替代成片')
                media = importlib.import_module('studio.narrated')
                production = {**(job.get('production') or {}), 'stage': 'preparing_voice', 'error_code': None}
                work_dir = self.data / 'production' / job['id']
                work_dir.mkdir(parents=True, exist_ok=True)
                if creation['mode'] == 'talking_head':
                    stage('preparing_voice')
                    voice = importlib.import_module('studio.narration')
                    presenter = importlib.import_module('studio.presenter')
                    attempt = work_dir / uuid4().hex
                    attempt.mkdir()
                    audio = voice.synthesize(creation['spoken_script'], creation['voice_id'], attempt/'narration.wav',
                        style=creation['options']['voice_style'], stop_event=self.stop_event)
                    portrait = PresenterLibrary(self.data).resolve(creation['portrait_snapshot']['id'])
                    stage('animating_presenter')
                    animated = presenter.render_presenter(portrait, audio['output_path'], attempt/'presenter.mp4',
                        consent_confirmed=True, stop_event=self.stop_event)
                    plan = media.build_narrated_plan([str(attempt/'presenter.mp4')], creation['spoken_script'],
                        creation['voice_id'], creation['options'], settings, work_dir=attempt,
                        stop_event=self.stop_event, prepared_narration=audio, on_stage=stage)
                    plan['presenter'] = animated
                    plan['warnings'].append('本版本使用静态肖像同步嘴型，身体、服装和背景保持所选形象。')
                else:
                    plan = media.build_narrated_plan([str(path) for path in paths], creation['spoken_script'],
                        creation['voice_id'], creation['options'], settings, work_dir=work_dir, stop_event=self.stop_event, on_stage=stage)
            elif creation is not None:
                media = importlib.import_module('studio.creative')
                plan = media.build_creation_plan([str(path) for path in paths], creation['brief'], creation['options'], settings)
            else:
                media = importlib.import_module("studio.media")
                if settings.get("srt_path"):
                    settings["srt_path"] = str(safe_subtitle(self.inbox, settings["srt_path"]))
                plan = media.build_plan(str(paths[0]), settings)
            validate_job_sources(job, self.inbox, self.store)
            fields = {}
            if narrated:
                narration = plan.get('narration') or {}
                fields['production'] = {**production, 'stage': 'compositing',
                    'narration_duration': narration.get('duration', plan.get('duration')),
                    'warnings': plan.get('warnings', [])}
            if not narrated:
                fields['production'] = {'stage': 'compositing'}
            self.store.transition(job["id"], ["analyzing"], "rendering", STAGE_MESSAGES['compositing'], plan=plan, progress=35, **fields)
            output = self.data / "renders" / f"{job['id']}.mp4"
            report = (media.render_narrated_plan(plan, str(output), settings, on_stage=stage) if narrated else
                      media.render_creation_plan(plan, str(output), settings) if creation is not None
                      else media.render_plan(plan, str(output), settings))
            validate_job_sources(job, self.inbox, self.store)
            passed = report.get("qc", {}).get("passed") is True
            if narrated:
                fields['production'] = {**fields['production'], 'stage': 'ready' if passed else 'failed',
                    'duration': report.get('duration'), 'warnings': report.get('warnings', [])}
            if not passed:
                self.store.transition(job["id"], ["rendering"], "failed", "成片检查未通过，禁止发布", result=report,
                                      output_path=str(output) if output.is_file() else None,
                                      error="成片检查未通过，请查看检查报告", progress=100, **fields)
                return
            self.store.transition(job["id"], ["rendering"], "review", "成片检查通过，等待人工试看确认", result=report,
                                  output_path=str(output), error=None, progress=100, **fields)
        except Exception as exc:
            current = self.store.get(job["id"])
            if current and current["status"] in {"analyzing", "rendering"}:
                # ffmpeg diagnostics may contain local paths but never delivery credentials.
                fields = {}
                if current.get('production'):
                    code = getattr(exc, 'code', 'production_failed')
                    if not isinstance(code, str) or not re.fullmatch(r'[a-z_]{1,60}', code):
                        code = 'production_failed'
                    fields['production'] = {**current['production'], 'last_stage': current['production'].get('stage'), 'stage': 'failed', 'error_code': code}
                self.store.transition(job["id"], [current["status"]], "failed", "剪辑失败", error=str(exc)[:4000], progress=0, **fields)


def delivery_state() -> dict:
    try:
        delivery = importlib.import_module("studio.delivery")
        if hasattr(delivery, "configuration_status"):
            return delivery.configuration_status()
    except ImportError:
        pass
    mode = os.getenv("CUTFLOW_WECOM_MODE", "webhook")
    required = (["CUTFLOW_WECOM_WEBHOOK"] if mode == "webhook" else
                ["CUTFLOW_WECOM_CORP_ID", "CUTFLOW_WECOM_SECRET", "CUTFLOW_WECOM_AGENT_ID", "CUTFLOW_WECOM_TO_USER"])
    missing = [name for name in required if not os.getenv(name)]
    return {"mode": mode, "configured": not missing and mode in {"webhook", "app"},
            "enabled": os.getenv("CUTFLOW_PUBLISH_ENABLED") == "1", "missing": missing}


def safe_subtitle(inbox: Path, relative: str) -> Path:
    if not isinstance(relative, str) or "\x00" in relative or re.match(r"^[a-zA-Z]:", relative):
        raise ValueError("字幕路径必须相对于投递箱")
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts or relative.startswith("\\"):
        raise ValueError("字幕路径不能越过投递箱")
    resolved = (inbox / candidate).resolve()
    if not resolved.is_relative_to(inbox.resolve()) or not resolved.is_file() or resolved.suffix.lower() != ".srt":
        raise ValueError("字幕必须是投递箱中的 SRT 文件")
    return resolved


def create_app(*, data_dir: str | Path | None = None, inbox: str | Path | None = None,
               token: str | None = None, start_worker: bool = True, idea_provider=None, script_provider=None) -> FastAPI:
    data = Path(data_dir or os.getenv("CUTFLOW_DATA", PROJECT_ROOT / "data")).resolve()
    inbox_path = Path(inbox or os.getenv("CUTFLOW_INBOX", PROJECT_ROOT / "素材投递箱")).resolve()
    token_value = os.getenv("CUTFLOW_TOKEN", "") if token is None else token
    data.mkdir(parents=True, exist_ok=True)
    inbox_path.mkdir(parents=True, exist_ok=True)
    (data / "renders").mkdir(exist_ok=True)
    store = Store(data / "studio.sqlite3")
    worker = Worker(store, inbox_path, data)
    previews = PreviewManager(data)
    gpu_status = detect_gpu()

    @asynccontextmanager
    async def lifespan(app):
        if start_worker:
            worker.start()
            collection_worker.start()
            ideas_worker.start()
            scripts_worker.start()
        try:
            yield
        finally:
            scripts_worker.stop()
            ideas_worker.stop()
            previews.stop()
            if start_worker:
                collection_worker.stop()
                worker.stop()

    app = FastAPI(title="CutFlow Studio", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store, app.state.worker = store, worker
    app.state.data, app.state.inbox = data, inbox_path
    app.state.previews = previews
    origins = [v.strip() for v in os.getenv("CUTFLOW_ALLOWED_ORIGINS", "").split(",") if v.strip()]
    if origins:
        if "*" in origins:
            raise ValueError("CUTFLOW_ALLOWED_ORIGINS must use exact HTTPS/localhost origins, never *")
        app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=False,
                           allow_methods=["GET", "POST", "PATCH", "PUT"], allow_headers=["Authorization", "Content-Type", "Idempotency-Key"])

    @app.middleware("http")
    async def response_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def authorize(request: Request, authorization: str | None = Header(default=None)):
        if not token_value:
            raise HTTPException(503, "尚未配置本机访问口令。请使用启动脚本，或设置 CUTFLOW_TOKEN 后重启。")
        supplied = authorization[7:] if authorization and authorization.startswith("Bearer ") else ""
        # Query tokens are accepted only for a video element, never for API writes.
        if not supplied and request.method == "GET" and request.url.path.endswith("/video"):
            supplied = request.query_params.get("token", "")
        if not supplied or not hmac.compare_digest(supplied.encode(), token_value.encode()):
            raise HTTPException(401, "需要有效的本机访问口令", headers={"WWW-Authenticate": "Bearer"})

    collection_worker = register_collection_routes(app, authorize, inbox_path, data)
    ideas_worker = register_ideas_routes(app, authorize, inbox_path, data, source_validator=safe_source,
                                        name_formatter=display_name, provider=idea_provider)
    app.state.ideas = ideas_worker
    knowledge = register_knowledge_routes(app, authorize, data, store)
    app.state.knowledge = knowledge

    def references(ids):
        return knowledge.reference_snapshot([UUID(str(identifier)).hex for identifier in ids])

    def completed_work(identifier):
        job = store.get(UUID(str(identifier)).hex)
        if not job or job.get('job_kind') != 'production':
            raise HTTPException(404, '正式作品不存在')
        if job['status'] not in {'review', 'approved', 'published'} or (job.get('result') or {}).get('qc', {}).get('passed') is not True:
            raise HTTPException(409, '请先完成原作品，再生成新版本')
        get_output(job)
        return job

    def rewrite_source(identifier):
        job = completed_work(identifier)
        try:
            validate_job_sources(job, inbox_path, store)
        except (ValueError, OSError) as exc:
            raise HTTPException(409, '原作品使用的素材或人物已变化，请重新选择后制作') from exc
        script = (job.get('settings') or {}).get('creation', {}).get('spoken_script', '')
        if not script:
            raise HTTPException(409, '此作品没有可修改的独白讲稿，请从开始制作填写新讲稿')
        return script

    scripts_worker = register_script_routes(app, authorize, inbox_path, data, source_validator=safe_source,
                                           name_formatter=display_name, provider=script_provider,
                                           reference_resolver=references, rewrite_resolver=rewrite_source,
                                           reference_validator=knowledge.validate_references,
                                           source_allowed=lambda relative: relative not in store.technical_test_sources())
    app.state.scripts = scripts_worker
    portraits = register_presenter_routes(app, authorize, data)
    app.state.portraits = portraits

    def get_job(job_id: str):
        job = store.get(job_id)
        if job is None:
            raise HTTPException(404, "任务不存在")
        return job

    def get_output(job: dict) -> Path:
        output = Path(job["output_path"]).resolve() if job.get("output_path") else None
        if output is None or not output.is_relative_to(data / "renders") or not output.is_file():
            raise HTTPException(404, "该任务还没有可播放的成片")
        return output

    @app.exception_handler(StateConflict)
    async def conflict_handler(request, exc):
        content = {"detail": str(exc)}
        if getattr(exc,"code",None) == "technical_test_source":
            content["code"] = "technical_test_source"
        return JSONResponse(status_code=409, content=content)

    @app.get("/api/health")
    def health():
        return {"ok": True, "service": "cutflow-studio", "version": __version__, "auth_configured": bool(token_value)}

    @app.get("/api/config", dependencies=[Depends(authorize)])
    def config():
        automatic = os.getenv("CUTFLOW_AUTO_PUBLISH") == "1"
        return {"inbox": str(inbox_path), "data_dir": str(data), "delivery": delivery_state(),
                "review_required": not automatic, "auto_publish": automatic, "supported_extensions": sorted(MEDIA_EXTENSIONS),
                "preview_supported": True, "creation_supported": True,
                "idea_refinement_supported": ideas_worker.supported(),
                "knowledge_supported": True,
                "face_swap": {"ready": False, "reason": "原视频换脸尚未开通：还需确认换脸与人脸识别模型的商用授权，并完成本机视频验收。现有口播可使用换人物功能。"},
                "production": production_capabilities(scripts_worker.supported())}

    @app.get("/api/status", dependencies=[Depends(authorize)])
    def status():
        return {"version": __version__, "counts": store.counts(), "worker_running": bool(worker.thread and worker.thread.is_alive()),
                "technical_test_count": sum(store.counts(kind="technical_test").values()),
                "ffmpeg_available": shutil.which("ffmpeg") is not None, "ffprobe_available": shutil.which("ffprobe") is not None,
                "delivery": delivery_state(), "review_required": True, "gpu": gpu_status,
                "execution_target": os.getenv("CUTFLOW_EXECUTION_TARGET", "local")}

    @app.get("/api/assets", dependencies=[Depends(authorize)])
    def assets():
        found = []
        diagnostic_sources = store.technical_test_sources()
        for path in inbox_path.rglob("*"):
            if len(found) >= 1000:
                break
            if path.suffix.lower() not in MEDIA_EXTENSIONS or not path.is_file():
                continue
            try:
                relative = path.relative_to(inbox_path).as_posix()
                checked = safe_source(inbox_path, relative)
                if checked.relative_to(inbox_path).as_posix() in diagnostic_sources:
                    continue
                stat = checked.stat()
            except (ValueError, OSError):
                continue
            found.append({"source": relative, "name": path.name, "display_name": display_name(relative),
                          "suggested_preset": import_mode(relative), "size": stat.st_size,
                          "modified_ns": stat.st_mtime_ns, "modified_at": stat.st_mtime,
                          "extension": path.suffix.lower()})
        return {"assets": sorted(found, key=lambda item: item["modified_ns"], reverse=True), "limit": 1000}

    @app.get("/api/assets/video", dependencies=[Depends(authorize)])
    def asset_video(source: str = Query(min_length=1, max_length=4096)):
        try:
            path = safe_source(inbox_path, source)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if path.relative_to(inbox_path).as_posix() in store.technical_test_sources():
            raise HTTPException(409, "该素材已登记为技术测试，请从技术测试记录查看验证资料")
        return FileResponse(path, media_type=MEDIA_TYPES[path.suffix.lower()], filename=display_name(path.name),
                            content_disposition_type="inline")

    @app.get("/api/jobs", dependencies=[Depends(authorize)])
    def jobs(limit: int = Query(default=100, ge=1, le=500), kind: Literal["production","technical_test"] = "production"):
        return {"jobs": [public_job(job, inbox=inbox_path, store=store) for job in store.list(limit, kind=kind)]}

    @app.post('/api/creations', dependencies=[Depends(authorize)])
    def new_creation(body: NewCreation):
        is_person = body.mode == 'talking_head'
        narrated = body.mode in {'narrated_promo', 'brand_promo', 'talking_head'}
        if narrated and not is_person and not body.sources:
            raise HTTPException(400, '请先上传用于搭配旁白的画面素材；当前不会自动生成或补拍画面。')
        capabilities = production_capabilities() if narrated else None
        if narrated:
            if not capabilities['modes'][body.mode]['ready']:
                raise HTTPException(409 if is_person else 503, capabilities['modes'][body.mode]['reason'])
            if body.voice_id not in {voice['id'] for voice in capabilities['voices']}:
                raise HTTPException(400, '所选音色不可用，请从当前中文音色目录重新选择。')
        portrait_snapshot = None
        if is_person:
            portraits.resolve(body.presenter.portrait_id)
            portrait_snapshot = portraits.snapshot(body.presenter.portrait_id)
            if portrait_snapshot['kind'] != body.presenter.kind or not portrait_snapshot['consent_confirmed']:
                raise HTTPException(409, '所选人物类型或肖像授权记录不一致')
        snapshots, seen = [], set()
        for requested in body.sources:
            try:
                path = safe_source(inbox_path, requested)
                stat = path.stat()
            except (ValueError, OSError) as exc:
                raise HTTPException(400, str(exc) if isinstance(exc, ValueError) else '素材暂时无法读取，请等待同步完成') from exc
            identity = os.path.normcase(str(path))
            if identity in seen:
                raise HTTPException(400, '同一素材不能重复选择，请调整素材顺序后提交')
            if stat.st_size <= 0:
                raise HTTPException(400, '素材为空或尚未同步完成')
            seen.add(identity)
            snapshots.append({'source': path.relative_to(inbox_path).as_posix(), 'size': stat.st_size,
                              'mtime_ns': stat.st_mtime_ns, 'ctime_ns': stat.st_ctime_ns})
        if {entry['source'] for entry in snapshots} & store.technical_test_sources():
            raise DiagnosticSourceConflict('所选素材包含已登记的技术测试，不能用于正式创作')
        requested_options = body.options.model_dump(exclude_unset=True)
        options = {key: value for key, value in requested_options.items()
                   if key not in {'subtitles', 'voice_style', 'source_audio'}}
        try:
            creative = importlib.import_module('studio.creative')
        except ImportError as exc:
            raise HTTPException(503, '创作组件尚未就绪，请稍后重新连接') from exc
        try:
            interpretation = creative.interpret_brief(body.brief, options)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(400, str(exc)) from exc
        if (not isinstance(interpretation, dict) or not isinstance(interpretation.get('resolved'), dict)
                or not isinstance(interpretation.get('warnings', []), list)
                or any(not isinstance(warning, str) for warning in interpretation.get('warnings', []))):
            raise HTTPException(503, '创作组件未返回完整制作方案，已停止提交')
        if narrated:
            resolved = interpretation['resolved']
            source_audio = body.options.source_audio
            if 'source_audio' not in requested_options and 'keep_original_audio' in requested_options:
                source_audio = 'low' if body.options.keep_original_audio else 'mute'
            options = {key: resolved.get(key) for key in ('aspect', 'opening_title', 'music', 'target_duration')}
            options.update(visual_fill=body.visual_fill, subtitles=body.options.subtitles,
                           voice_style=body.options.voice_style, source_audio=source_audio)
            warnings = [warning for warning in interpretation.get('warnings', [])
                        if not any(marker in warning for marker in ('不会新增或改写配音', '不会自动生成字幕',
                             '节奏调整仅清理', '完整创意、分镜和新旁白作为策划保存'))]
            warnings += ['按已确认讲稿生成新旁白；实际成片时长跟随完整语音，不截断旁白以凑目标时长。',
                         '画面按素材顺序组织，未自动理解内容或挑选语义亮点。']
            if resolved.get('pace') != 'preserve':
                warnings.append('独白模式不按原片静音缩短讲稿；将使用完整新旁白。')
            if body.visual_fill == 'loop':
                warnings.append('你已允许画面不足时循环所选素材，旁白保持完整且不循环。')
            interpretation = {'resolved': options, 'summary': ['按最终讲稿生成中文旁白',
                '画面跟随完整语音长度', '原素材声音静音' if source_audio == 'mute' else '低音量保留原素材声音',
                '生成讲稿字幕' if options['subtitles'] else '不显示字幕',
                '添加轻音乐' if options['music'] else '不额外添加音乐'], 'warnings': list(dict.fromkeys(warnings))}
        settings = {'encoder': 'nvenc' if gpu_status['available'] else 'cpu'}
        try:
            configured = json.loads(os.getenv('CUTFLOW_SETTINGS', '{}'))
            if isinstance(configured, dict):
                settings.update({key: value for key, value in configured.items()
                                 if key in {'encoder', 'fps', 'crf', 'normalize_audio'}})
        except ValueError:
            raise HTTPException(503, '服务端剪辑参数配置无效，请检查启动配置') from None
        title = body.title.strip() or body.brief[:30]
        specification = {'version': 2 if narrated else 1, 'sources': snapshots, 'brief': body.brief,
                         'options': options, 'interpretation': interpretation}
        if body.parent_job_id:
            parent = completed_work(body.parent_job_id)
            if body.revision_kind == 'presenter' and (body.mode != 'talking_head' or (parent.get('settings') or {}).get('creation', {}).get('mode') != 'talking_head'):
                raise HTTPException(409, '换人物只适用于已生成的人物口播')
            specification.update(parent_job_id=parent['id'], revision_kind=body.revision_kind)
        if body.reference_ids:
            specification['reference_works'] = references(body.reference_ids)
        if body.script_id:
            prepared = scripts_worker.store.get(str(body.script_id))
            if not prepared or prepared.get('status') != 'succeeded':
                raise HTTPException(409, '引用的讲稿尚未生成完成')
            specification['script_modified'] = (prepared.get('result') or {}).get('spoken_script') != body.spoken_script
            frozen = (prepared.get('resolved_context') or {}).get('reference_works') or []
            if {entry['id'] for entry in frozen} == {UUID(str(identifier)).hex for identifier in body.reference_ids}:
                knowledge.validate_references(frozen)
                specification['reference_works'] = frozen
            elif frozen or body.reference_ids:
                raise HTTPException(409, '讲稿所用参考与本次选择不同，请按当前参考重新生成讲稿')
        production = None
        if narrated:
            specification.update(mode=body.mode, spoken_script=body.spoken_script, voice_id=body.voice_id,
                visual_fill=body.visual_fill, brand=body.brand.model_dump(), subtype=body.subtype,
                script_id=str(body.script_id) if body.script_id else None, requested_options=requested_options,
                provider=capabilities['narration_provider'], model=capabilities['narration_model'])
            if is_person:
                specification.update(presenter=body.presenter.model_dump(), portrait_snapshot=portrait_snapshot)
            signature_value = ['creation-v2', specification, title, settings]
            production = {'mode': body.mode, 'stage': 'queued', 'duration_policy': 'complete_narration',
                          'warnings': interpretation['warnings'], 'error_code': None}
        else:
            signature_value = ['creation-v1', specification, title, settings]
        signature = json.dumps(signature_value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
        settings['creation'] = specification
        first = snapshots[0] if snapshots else {'source':'portrait:'+portrait_snapshot['id'], 'size':0, 'mtime_ns':0}
        job, created = store.create(source=first['source'], title=title,
                                    settings=settings, fingerprint=hashlib.sha256(signature.encode()).hexdigest(),
                                    source_size=first['size'], source_mtime_ns=first['mtime_ns'],
                                    idempotency_key=body.idempotency_key, production=production,
                                    allow_active_merge=not bool(body.parent_job_id))
        worker.wake_event.set()
        return JSONResponse(status_code=201 if created else 200,
                            content={'job': public_job(job, inbox=inbox_path, store=store), 'created': created,
                                     'owned_by_request': store.owned_by_request(job['id'], body.idempotency_key)})

    @app.post("/api/jobs", dependencies=[Depends(authorize)])
    def new_job(body: NewJob, idempotency_key: str | None = Header(default=None)):
        unsupported = set(body.settings) - EDIT_SETTINGS
        if unsupported:
            raise HTTPException(400, "当前版本不支持这些剪辑设置：" + ", ".join(sorted(unsupported)))
        for setting in ("remove_silence", "normalize_audio"):
            if setting in body.settings and not isinstance(body.settings[setting], bool):
                raise HTTPException(400, f"{setting} 必须为布尔值")
        try:
            path = safe_source(inbox_path, body.source)
            if body.settings.get("srt_path"):
                safe_subtitle(inbox_path, body.settings["srt_path"])
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if len(json.dumps(body.settings)) > 64000:
            raise HTTPException(400, "剪辑设置过大")
        stat = path.stat()
        source = path.relative_to(inbox_path).as_posix()
        signature = json.dumps([source, stat.st_size, stat.st_mtime_ns, body.settings], sort_keys=True)
        fingerprint = hashlib.sha256(signature.encode()).hexdigest()
        key = body.idempotency_key or idempotency_key
        if key is not None and (not key or len(key) > 200):
            raise HTTPException(400, "请求编号长度应为 1–200 个字符")
        job, created = store.create(source=source, title=body.title.strip() or path.stem, settings=body.settings,
                                    fingerprint=fingerprint, source_size=stat.st_size, source_mtime_ns=stat.st_mtime_ns,
                                    idempotency_key=key)
        worker.wake_event.set()
        return JSONResponse(status_code=201 if created else 200, content={"job": public_job(job, inbox=inbox_path, store=store), "created": created,
                            "owned_by_request": store.owned_by_request(job["id"], key)})

    @app.get("/api/jobs/{job_id}", dependencies=[Depends(authorize)])
    def job_detail(job_id: str):
        job = public_job(get_job(job_id), inbox=inbox_path, store=store)
        job["events"] = store.events(job_id)
        return {"job": job}

    @app.get("/api/jobs/{job_id}/video", dependencies=[Depends(authorize)])
    def video(job_id: str, preview: Literal["webm"] | None = None):
        job = get_job(job_id)
        output = get_output(job)
        if preview == "webm":
            try:
                compatible = previews.video_path(job_id, output)
            except PreviewError as exc:
                raise HTTPException(404, str(exc)) from exc
            if compatible is None:
                raise HTTPException(404, "兼容预览尚未生成，请先从任务页面生成预览")
            return FileResponse(compatible, media_type="video/webm",
                                filename=video_download_name(job["title"])[:-4] + "-预览.webm",
                                content_disposition_type="inline")
        return FileResponse(output, media_type="video/mp4", filename=video_download_name(job["title"]),
                            content_disposition_type="inline")

    @app.get('/api/jobs/{job_id}/draft', dependencies=[Depends(authorize)])
    def work_draft(job_id: UUID):
        job = completed_work(job_id)
        creation = (job.get('settings') or {}).get('creation') or {}
        if not creation:
            raise HTTPException(409, '此旧版作品没有完整制作草稿，请从素材库重新制作')
        defaults = {'mode': 'edit', 'brief': '', 'options': {}, 'spoken_script': '',
                    'voice_id': '', 'visual_fill': 'reject', 'brand': {}, 'subtype': 'salon_promo', 'presenter': None}
        draft = {key: creation.get(key, default) for key, default in defaults.items()}
        draft['options'] = {key: value for key, value in draft['options'].items() if key in CreationOptions.model_fields}
        return {'draft': draft,
                'sources': [entry['source'] for entry in creation.get('sources', [])],
                'parent_job_id': job['id'], 'title': job['title']}

    @app.get("/api/jobs/{job_id}/preview", dependencies=[Depends(authorize)])
    def preview_status(job_id: str):
        job = get_job(job_id)
        return previews.status(job_id, get_output(job))

    @app.post("/api/jobs/{job_id}/preview", dependencies=[Depends(authorize)])
    def generate_preview(job_id: str):
        job = get_job(job_id)
        return previews.request(job_id, get_output(job))

    @app.post("/api/jobs/{job_id}/approve", dependencies=[Depends(authorize)])
    def approve(job_id: str):
        job = get_job(job_id)
        if job["job_kind"] == "technical_test":
            raise HTTPException(409, "这是技术测试记录，不能确认为可发布的正式成片")
        if job["status"] in {"approved", "published"}:
            return {"job": public_job(job, inbox=inbox_path, store=store)}
        if (job.get("result") or {}).get("qc", {}).get("passed") is not True:
            raise HTTPException(409, "成片检查未通过，不能确认发布")
        updated = store.transition(job_id, ["review"], "approved", "用户已确认成片，可以发布")
        return {"job": public_job(updated, inbox=inbox_path, store=store)}

    @app.post("/api/jobs/{job_id}/retry", dependencies=[Depends(authorize)])
    def retry(job_id: str):
        job = get_job(job_id)
        reason = retry_block_reason(job, inbox_path, store)
        if reason is not None:
            raise HTTPException(409, reason)
        fields = {'production': {**job['production'], 'stage': 'queued', 'error_code': None}} if job.get('production') else {}
        updated = store.transition(job_id, ["failed"], "queued", "用户请求重新剪辑，已排队", plan=None,
                                   result=None, output_path=None, error=None, progress=0, **fields)
        worker.wake_event.set()
        return {"job": public_job(updated, inbox=inbox_path, store=store)}

    @app.post("/api/jobs/{job_id}/publish", dependencies=[Depends(authorize)])
    def publish(job_id: str):
        job = get_job(job_id)
        if job["job_kind"] == "technical_test":
            raise HTTPException(409, "技术测试记录不能发布为正式视频")
        if job["status"] == "published":
            return {"job": public_job(job, inbox=inbox_path, store=store), "already_published": True}
        if job["status"] != "approved":
            raise HTTPException(409, "请先试看并确认成片")
        destination = delivery_state()
        if not destination["enabled"] or not destination["configured"]:
            raise HTTPException(409, "企业微信尚未启用或配置完整。请在本机环境中填写机器人地址并启用发布。")
        try:
            delivery = importlib.import_module("studio.delivery")
        except ImportError as exc:
            raise HTTPException(503, "企业微信发布组件尚未安装") from exc
        output = Path(job["output_path"] or "").resolve()
        if not output.is_relative_to(data / "renders") or not output.is_file():
            raise HTTPException(409, "成片文件不存在，请重新剪辑")
        store.transition(job_id, ["approved"], "publishing", "开始向企业微信发送成片")
        try:
            result = delivery.publish_video(str(output), job["title"], job_id, approved=True)
            if result.get("status") != "published":
                raise RuntimeError("发布组件没有返回已确认的发送结果，请核实企业微信，勿重复发送")
        except Exception as exc:
            uncertain = getattr(exc, "uncertain", True)
            # Avoid displaying exception strings which might contain webhook URLs.
            code = str(getattr(exc, "code", "delivery_error"))[:80]
            message = ("发送结果未知，请先在企业微信核实。系统不会自动重发。" if uncertain else
                       f"企业微信发送失败（{code}）。请检查本机发布配置。")
            updated = store.transition(job_id, ["publishing"], "failed" if uncertain else "approved", message,
                                       error=message, publication={"status": "unknown" if uncertain else "failed", "code": code})
            return JSONResponse(status_code=502, content={"detail": message, "job": public_job(updated, inbox=inbox_path, store=store)})
        updated = store.transition(job_id, ["publishing"], "published", "企业微信已确认接收", publication=result, error=None)
        return {"job": public_job(updated, inbox=inbox_path, store=store)}

    web_root = PROJECT_ROOT / "web"
    if web_root.is_dir():
        app.mount("/", StaticFiles(directory=web_root, html=True), name="web")
    else:
        @app.get("/")
        def index():
            return {"service": "CutFlow Studio", "message": "前端文件尚未就绪，API 已启动"}
    return app


app = create_app()
