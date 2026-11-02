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
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .store import StateConflict, Store
from .collection_service import register_collection_routes

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MEDIA_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mts"}
EDIT_SETTINGS = {"silence_min", "silence_db", "padding", "min_clip", "target_aspect", "fps", "encoder", "crf",
                 "normalize_audio", "remove_silence", "source_ranges", "width", "height", "srt_path"}


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


def public_job(job: dict) -> dict:
    # Do not disclose machine paths through results; relative inbox path is enough.
    result = {key: value for key, value in job.items()
              if key not in {"output_path", "fingerprint", "source_size", "source_mtime_ns"}}
    result["has_video"] = bool(job.get("output_path") and Path(job["output_path"]).is_file())
    result["video_url"] = f"/api/jobs/{job['id']}/video" if result["has_video"] else None
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
        try:
            path = safe_source(self.inbox, job["source"])
            stat = path.stat()
            if stat.st_size != job["source_size"] or stat.st_mtime_ns != job["source_mtime_ns"]:
                raise ValueError("素材在排队后发生变化，请等同步完成后重新建任务")
            media = importlib.import_module("studio.media")
            settings = dict(job["settings"])
            if settings.get("srt_path"):
                settings["srt_path"] = str(safe_subtitle(self.inbox, settings["srt_path"]))
            plan = media.build_plan(str(path), settings)
            self.store.transition(job["id"], ["analyzing"], "rendering", "分析完成，开始渲染和成片检查", plan=plan, progress=35)
            output = self.data / "renders" / f"{job['id']}.mp4"
            report = media.render_plan(plan, str(output), settings)
            passed = report.get("qc", {}).get("passed") is True
            if not passed:
                self.store.transition(job["id"], ["rendering"], "failed", "成片检查未通过，禁止发布", result=report,
                                      output_path=str(output) if output.is_file() else None,
                                      error="成片检查未通过，请查看检查报告", progress=100)
                return
            self.store.transition(job["id"], ["rendering"], "review", "成片检查通过，等待人工试看确认", result=report,
                                  output_path=str(output), error=None, progress=100)
        except Exception as exc:
            current = self.store.get(job["id"])
            if current and current["status"] in {"analyzing", "rendering"}:
                # ffmpeg diagnostics may contain local paths but never delivery credentials.
                self.store.transition(job["id"], [current["status"]], "failed", "剪辑失败", error=str(exc)[:4000], progress=0)


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
               token: str | None = None, start_worker: bool = True) -> FastAPI:
    data = Path(data_dir or os.getenv("CUTFLOW_DATA", PROJECT_ROOT / "data")).resolve()
    inbox_path = Path(inbox or os.getenv("CUTFLOW_INBOX", PROJECT_ROOT / "素材投递箱")).resolve()
    token_value = os.getenv("CUTFLOW_TOKEN", "") if token is None else token
    data.mkdir(parents=True, exist_ok=True)
    inbox_path.mkdir(parents=True, exist_ok=True)
    (data / "renders").mkdir(exist_ok=True)
    store = Store(data / "studio.sqlite3")
    worker = Worker(store, inbox_path, data)
    gpu_status = detect_gpu()

    @asynccontextmanager
    async def lifespan(app):
        if start_worker:
            worker.start()
            collection_worker.start()
        try:
            yield
        finally:
            if start_worker:
                collection_worker.stop()
                worker.stop()

    app = FastAPI(title="CutFlow Studio", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store, app.state.worker = store, worker
    app.state.data, app.state.inbox = data, inbox_path
    origins = [v.strip() for v in os.getenv("CUTFLOW_ALLOWED_ORIGINS", "").split(",") if v.strip()]
    if origins:
        if "*" in origins:
            raise ValueError("CUTFLOW_ALLOWED_ORIGINS must use exact HTTPS/localhost origins, never *")
        app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=False,
                           allow_methods=["GET", "POST", "PATCH"], allow_headers=["Authorization", "Content-Type", "Idempotency-Key"])

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

    def get_job(job_id: str):
        job = store.get(job_id)
        if job is None:
            raise HTTPException(404, "任务不存在")
        return job

    @app.exception_handler(StateConflict)
    async def conflict_handler(request, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.get("/api/health")
    def health():
        return {"ok": True, "service": "cutflow-studio", "version": __version__, "auth_configured": bool(token_value)}

    @app.get("/api/config", dependencies=[Depends(authorize)])
    def config():
        automatic = os.getenv("CUTFLOW_AUTO_PUBLISH") == "1"
        return {"inbox": str(inbox_path), "data_dir": str(data), "delivery": delivery_state(),
                "review_required": not automatic, "auto_publish": automatic, "supported_extensions": sorted(MEDIA_EXTENSIONS)}

    @app.get("/api/status", dependencies=[Depends(authorize)])
    def status():
        return {"version": __version__, "counts": store.counts(), "worker_running": bool(worker.thread and worker.thread.is_alive()),
                "ffmpeg_available": shutil.which("ffmpeg") is not None, "ffprobe_available": shutil.which("ffprobe") is not None,
                "delivery": delivery_state(), "review_required": True, "gpu": gpu_status,
                "execution_target": os.getenv("CUTFLOW_EXECUTION_TARGET", "local")}

    @app.get("/api/assets", dependencies=[Depends(authorize)])
    def assets():
        found = []
        for path in inbox_path.rglob("*"):
            if len(found) >= 1000:
                break
            if path.suffix.lower() not in MEDIA_EXTENSIONS or not path.is_file():
                continue
            try:
                relative = path.relative_to(inbox_path).as_posix()
                checked = safe_source(inbox_path, relative)
                stat = checked.stat()
            except (ValueError, OSError):
                continue
            found.append({"source": relative, "name": path.name, "size": stat.st_size,
                          "modified_ns": stat.st_mtime_ns, "modified_at": stat.st_mtime,
                          "extension": path.suffix.lower()})
        return {"assets": sorted(found, key=lambda item: item["modified_ns"], reverse=True), "limit": 1000}

    @app.get("/api/jobs", dependencies=[Depends(authorize)])
    def jobs(limit: int = Query(default=100, ge=1, le=500)):
        return {"jobs": [public_job(job) for job in store.list(limit)]}

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
        return JSONResponse(status_code=201 if created else 200, content={"job": public_job(job), "created": created,
                            "owned_by_request": store.owned_by_request(job["id"], key)})

    @app.get("/api/jobs/{job_id}", dependencies=[Depends(authorize)])
    def job_detail(job_id: str):
        job = public_job(get_job(job_id))
        job["events"] = store.events(job_id)
        return {"job": job}

    @app.get("/api/jobs/{job_id}/video", dependencies=[Depends(authorize)])
    def video(job_id: str):
        job = get_job(job_id)
        output = Path(job["output_path"]).resolve() if job.get("output_path") else None
        if output is None or not output.is_relative_to(data / "renders") or not output.is_file():
            raise HTTPException(404, "该任务还没有可播放的成片")
        return FileResponse(output, media_type="video/mp4", filename=f"{job_id}.mp4", content_disposition_type="inline")

    @app.post("/api/jobs/{job_id}/approve", dependencies=[Depends(authorize)])
    def approve(job_id: str):
        job = get_job(job_id)
        if job["status"] in {"approved", "published"}:
            return {"job": public_job(job)}
        if (job.get("result") or {}).get("qc", {}).get("passed") is not True:
            raise HTTPException(409, "成片检查未通过，不能确认发布")
        updated = store.transition(job_id, ["review"], "approved", "用户已确认成片，可以发布")
        return {"job": public_job(updated)}

    @app.post("/api/jobs/{job_id}/retry", dependencies=[Depends(authorize)])
    def retry(job_id: str):
        job = get_job(job_id)
        if job["status"] != "failed":
            raise HTTPException(409, "只有失败的剪辑任务可以重新排队")
        if job.get("publication") is not None or any(event["status"] == "publishing" for event in store.events(job_id)):
            raise HTTPException(409, "发布失败或发送结果未知的任务不能用此按钮重试，请先核实企业微信")
        try:
            source = safe_source(inbox_path, job["source"])
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        stat = source.stat()
        if (stat.st_size, stat.st_mtime_ns) != (job["source_size"], job["source_mtime_ns"]):
            raise HTTPException(409, "素材已变化，请新建任务以保留原有剪辑记录")
        updated = store.transition(job_id, ["failed"], "queued", "用户请求重新剪辑，已排队", plan=None,
                                   result=None, output_path=None, error=None, progress=0)
        worker.wake_event.set()
        return {"job": public_job(updated)}

    @app.post("/api/jobs/{job_id}/publish", dependencies=[Depends(authorize)])
    def publish(job_id: str):
        job = get_job(job_id)
        if job["status"] == "published":
            return {"job": public_job(job), "already_published": True}
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
            return JSONResponse(status_code=502, content={"detail": message, "job": public_job(updated)})
        updated = store.transition(job_id, ["publishing"], "published", "企业微信已确认接收", publication=result, error=None)
        return {"job": public_job(updated)}

    web_root = PROJECT_ROOT / "web"
    if web_root.is_dir():
        app.mount("/", StaticFiles(directory=web_root, html=True), name="web")
    else:
        @app.get("/")
        def index():
            return {"service": "CutFlow Studio", "message": "前端文件尚未就绪，API 已启动"}
    return app


app = create_app()
