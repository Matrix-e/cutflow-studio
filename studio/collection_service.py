"""Authenticated collection API, official scheduler and local-browser bridge."""
from __future__ import annotations

import importlib
from pathlib import Path
import threading
from typing import Any, Literal

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from .collection_store import CollectionConflict, CollectionStore, canonical_url


class CollectionWorker:
    def __init__(self,store:CollectionStore,provider=None):
        self.store,self.provider = store,provider
        self.stop_event,self.wake_event = threading.Event(),threading.Event()
        self.thread = None

    def provider_status(self):
        if self.provider is None:
            return {"configured":False,"status":"not_configured"}
        try:
            status = self.provider.configuration_status()
            # Adapter supplies safe status; do not serialize arbitrary objects/secrets.
            return {key:value for key,value in status.items() if key in {
                "configured","enabled","status","missing","mode","message","requirements","reason","available"}}
        except Exception:
            return {"configured":False,"status":"not_configured"}

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.store.recover()
        self.stop_event.clear()
        self.thread = threading.Thread(target=self.run,name="cutflow-collections",daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.wake_event.set()
        if self.thread:
            self.thread.join(timeout=3)

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception:
                # No credentials, raw HTTP errors or provider response bodies in logs.
                pass
            self.wake_event.wait(timeout=5)
            self.wake_event.clear()

    def tick(self,now=None):
        self.store.expire_leases(now)
        self.store.queue_due(now)
        run = self.store.claim_official(now)
        if run:
            self.process(run)
        return run

    def process(self,run):
        if self.provider_status().get("configured") is not True:
            return self.store.finish_run(run["id"],"not_configured",error_code="not_configured",message="官方搜索接口尚未配置；没有发起搜索或下载")
        task = {"id":run["task_id"],**(run.get("task_snapshot") or self.store.get_task(run["task_id"]))}
        seen,added,skipped,pages = 0,0,0,0
        cursor,search_id = 0,""
        page_keys = set()
        try:
            while seen<task["max_items"] and pages<5 and not self.stop_event.is_set():
                marker = (str(cursor),str(search_id))
                if marker in page_keys:
                    break
                page_keys.add(marker)
                page = self.provider.search(task["keyword"],count=min(20,task["max_items"]-seen),cursor=cursor,
                                            search_id=search_id,publish_time=task["publish_time"],sort_type=task["sort_type"])
                if not isinstance(page,dict) or not isinstance(page.get("items"),list):
                    raise ValueError("invalid_provider_response")
                pages += 1
                batch = page["items"][:min(50-seen,task["max_items"]-seen)]
                for item in batch:
                    seen += 1
                    try:
                        _,created = self.store.add_item(item,task_id=task["id"],run_id=run["id"],source_kind="official")
                        added += int(created)
                    except ValueError:
                        skipped += 1
                if not page.get("has_more") or not batch:
                    break
                cursor = page.get("cursor",cursor)
                search_id = page.get("search_id",search_id)
            interrupted = self.stop_event.is_set()
            all_invalid = seen>0 and seen==skipped
            return self.store.finish_run(run["id"],"interrupted" if interrupted else "failed" if all_invalid else "succeeded",items_seen=seen,
                                          items_added=added,items_skipped=skipped,pages=pages,
                                          error_code="invalid_results" if all_invalid else None,
                                          message="执行已停止，已保存已取得的参考资料" if interrupted else "来源返回的元数据均无效，本次没有入库" if all_invalid else "官方搜索参考资料已入库；未下载视频，未确认使用授权")
        except Exception as exc:
            code = getattr(exc,"code","provider_failed")
            code = code if isinstance(code,str) and code.isidentifier() and len(code)<=80 else "provider_failed"
            return self.store.finish_run(run["id"],"failed",items_seen=seen,items_added=added,items_skipped=skipped,pages=pages,
                                          error_code=code,message="采集未完成；已保存已取得的资料，请检查来源配置或本次错误码")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskCreate(StrictModel):
    keyword:str = Field(min_length=1,max_length=100)
    provider:Literal["browser","official"] = "browser"
    enabled:bool = True
    interval_seconds:int = Field(default=86400,ge=3600,le=31*86400)
    max_items:int = Field(default=10,ge=1,le=50)
    publish_time:int = Field(default=0,ge=0,le=365)
    sort_type:int = Field(default=0,ge=0,le=2)


class TaskUpdate(StrictModel):
    keyword:str | None = Field(default=None,min_length=1,max_length=100)
    provider:Literal["browser","official"] | None = None
    enabled:bool | None = None
    interval_seconds:int | None = Field(default=None,ge=3600,le=31*86400)
    max_items:int | None = Field(default=None,ge=1,le=50)
    publish_time:int | None = Field(default=None,ge=0,le=365)
    sort_type:int | None = Field(default=None,ge=0,le=2)


class ItemImport(StrictModel):
    source_url:str = Field(min_length=1,max_length=4096)
    title:str = Field(default="",max_length=1000)
    author:str = Field(default="",max_length=200)
    rights_status:Literal["unknown","owned","licensed","reference"] = "unknown"
    rights_note:str = Field(default="",max_length=2000)


class RightsUpdate(StrictModel):
    rights_status:Literal["unknown","owned","licensed","reference"] | None = None
    rights_note:str | None = Field(default=None,max_length=2000)


class BrowserClaim(StrictModel):
    agent_id:str = Field(min_length=1,max_length=100)


class BrowserLease(StrictModel):
    lease_token:str = Field(min_length=1,max_length=200)


class BrowserComplete(BrowserLease):
    items:list[dict[str,Any]] = Field(default_factory=list,max_length=50)


class BrowserFail(BrowserLease):
    code:Literal["login_required","captcha_required","download_unavailable","browser_unavailable","collection_failed","deadline_exceeded"]
    # Accept agent notes for compatibility, but persist only curated safe messages.
    message:str | None = Field(default=None,max_length=500)
    items:list[dict[str,Any]] = Field(default_factory=list,max_length=50)


def register_collection_routes(app,authorize,inbox,data,provider=None) -> CollectionWorker:
    """Register routes, return worker for the owner's existing app lifespan."""
    store = CollectionStore(Path(data)/"collections.sqlite3")
    if provider is None:
        try:
            provider = importlib.import_module("studio.douyin").DouyinProvider()
        except (ImportError,AttributeError):
            provider = None
    worker = CollectionWorker(store,provider)
    app.state.collection_store,app.state.collection_worker = store,worker
    auth = [Depends(authorize)]

    def invoke(fn,*args,**kwargs):
        try:
            return fn(*args,**kwargs)
        except CollectionConflict as exc:
            raise HTTPException(409,str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(404,"采集任务或素材不存在") from exc
        except ValueError as exc:
            raise HTTPException(400,str(exc)) from exc

    @app.get("/api/collections/config",dependencies=auth)
    def collection_config():
        official = worker.provider_status()
        browser = store.browser_status()
        return {"provider":"browser","providers":{"browser":browser,"official":official},"browser":browser,"official":official,
                "min_interval_seconds":3600,"max_items_per_run":50,"browser_lease_seconds":900,
                "browser_max_items_per_run":10,"browser_supported_publish_time":[0],"browser_supported_sort_type":[0],
                "automatic_inbox_import":False,"rights_verified":False,
                "notice":"搜索结果先进入素材参考库。本机下载存入隔离目录；确认授权并选择使用后才能进入剪辑素材箱。"}

    @app.get("/api/collections/tasks",dependencies=auth)
    def tasks():
        return {"tasks":store.list_tasks()}

    @app.post("/api/collections/tasks",dependencies=auth,status_code=201)
    def create_task(body:TaskCreate):
        task = invoke(store.create_task,**body.model_dump())
        worker.wake_event.set()
        return {"task":task}

    @app.patch("/api/collections/tasks/{task_id}",dependencies=auth)
    def update_task(task_id:str,body:TaskUpdate):
        task = invoke(store.update_task,task_id,body.model_dump(exclude_unset=True))
        worker.wake_event.set()
        return {"task":task}

    @app.post("/api/collections/tasks/{task_id}/run",dependencies=auth)
    def run_task(task_id:str):
        run,created = invoke(store.enqueue,task_id)
        worker.wake_event.set()
        return {"run":run,"created":created}

    @app.get("/api/collections/tasks/{task_id}/runs",dependencies=auth)
    def task_runs(task_id:str,limit:int=Query(default=50,ge=1,le=200)):
        if not store.get_task(task_id):
            raise HTTPException(404,"采集任务不存在")
        return {"runs":store.list_runs(task_id,limit)}

    @app.get("/api/collections/runs",dependencies=auth)
    def runs(limit:int=Query(default=50,ge=1,le=200)):
        return {"runs":store.list_runs(limit=limit)}

    @app.get("/api/collections/items",dependencies=auth)
    def items(task_id:str | None=None,rights_status:Literal["unknown","owned","licensed","reference"] | None=None,
              q:str | None=Query(default=None,max_length=100),limit:int=Query(default=100,ge=1,le=500),offset:int=Query(default=0,ge=0)):
        return {"items":store.list_items(task_id,rights_status,q,limit,offset),"limit":limit,"offset":offset}

    @app.post("/api/collections/items/import",dependencies=auth,status_code=201)
    def import_item(body:ItemImport):
        source_url,item_id = invoke(canonical_url,body.source_url)
        item,created = invoke(store.add_item,{"source_url":source_url,"item_id":item_id,"title":body.title,"author":body.author},
                              rights_status=body.rights_status,rights_note=body.rights_note,source_kind="manual")
        return {"item":item,"created":created}

    @app.patch("/api/collections/items/{item_id}",dependencies=auth)
    def update_item(item_id:str,body:RightsUpdate):
        return {"item":invoke(store.update_rights,item_id,**body.model_dump(exclude_unset=True))}

    @app.post("/api/collections/browser/claim",dependencies=auth)
    def browser_claim(body:BrowserClaim):
        return {"run":invoke(store.claim_browser,body.agent_id)}

    @app.post("/api/collections/browser/runs/{run_id}/complete",dependencies=auth)
    def browser_complete(run_id:str,body:BrowserComplete):
        return {"run":invoke(store.complete_browser,run_id,body.lease_token,body.items)}

    @app.post("/api/collections/browser/runs/{run_id}/fail",dependencies=auth)
    def browser_fail(run_id:str,body:BrowserFail):
        return {"run":invoke(store.fail_browser,run_id,body.lease_token,body.code,items=body.items)}

    @app.post("/api/collections/browser/runs/{run_id}/heartbeat",dependencies=auth)
    def browser_heartbeat(run_id:str,body:BrowserLease):
        return invoke(store.heartbeat,run_id,body.lease_token)

    return worker
