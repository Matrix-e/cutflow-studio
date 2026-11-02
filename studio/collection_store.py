"""Durable keyword schedules, browser leases and metadata-only material library."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import sqlite3
import time
from urllib.parse import urlsplit
import uuid

RIGHTS = {"unknown", "owned", "licensed", "reference"}
PROVIDERS = {"browser", "official"}
CDN_SUFFIXES = ("douyinpic.com", "douyincdn.com", "byteimg.com", "ibyteimg.com")


class CollectionConflict(ValueError):
    pass


def canonical_url(value: str) -> tuple[str, str | None]:
    if not isinstance(value,str) or len(value) > 4096:
        raise ValueError("请输入有效的抖音作品链接")
    try:
        parsed = urlsplit(value.strip())
        if parsed.scheme not in {"https","http"} or parsed.username or parsed.password or parsed.port not in {None,80,443}:
            raise ValueError()
        host = (parsed.hostname or "").lower()
        if host in {"douyin.com","www.douyin.com","m.douyin.com"}:
            match = re.fullmatch(r"/video/(\d{1,32})/?",parsed.path)
            if match:
                return f"https://www.douyin.com/video/{match.group(1)}",match.group(1)
        if host == "v.douyin.com":
            match = re.fullmatch(r"/([A-Za-z0-9_-]{2,80})/?",parsed.path)
            if match:
                return f"https://v.douyin.com/{match.group(1)}/",None
    except (ValueError,AttributeError):
        pass
    raise ValueError("仅支持抖音 /video/数字编号 作品链接或 v.douyin.com 短链接；短链接不会自动展开")


def safe_cover(value) -> str | None:
    if not isinstance(value,str) or len(value)>4096:
        return None
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in {None,443} or parsed.fragment:
            return None
        if any(host == suffix or host.endswith("."+suffix) for suffix in CDN_SUFFIXES):
            return value
    except ValueError:
        pass
    return None


def cleaned_item(item: dict) -> dict:
    if not isinstance(item,dict):
        raise ValueError("素材元数据必须是对象")
    item_id = item.get("item_id")
    item_id = str(item_id) if isinstance(item_id,(str,int)) and not isinstance(item_id,bool) else None
    if item_id and (len(item_id)>256 or any(ord(c)<33 for c in item_id)):
        item_id = None
    source_url = None
    numeric_id = None
    if item.get("source_url"):
        try:
            source_url,numeric_id = canonical_url(item["source_url"])
        except ValueError:
            pass
    if item_id and item_id.isdigit():
        if numeric_id and numeric_id != item_id:
            raise ValueError("作品编号与来源链接不一致")
        if len(item_id)<=32:
            source_url = f"https://www.douyin.com/video/{item_id}"
            numeric_id = item_id
    if not item_id:
        item_id = numeric_id
    if not item_id and not source_url:
        raise ValueError("素材缺少可识别的抖音作品编号或来源链接")
    def text(key,limit):
        value = item.get(key)
        return value[:limit] if isinstance(value,str) else ""
    likes = item.get("likes")
    likes = likes if isinstance(likes,int) and not isinstance(likes,bool) and 0<=likes<2**63 else None
    filename = text("local_filename",255)
    if not filename or filename in {".",".."} or any(c in filename for c in ("/","\\",":","\x00")) or any(ord(c)<32 for c in filename):
        filename = None
    digest = text("sha256",64).lower()
    digest = digest if re.fullmatch(r"[a-f0-9]{64}",digest or "") else None
    size = item.get("size_bytes")
    size = size if isinstance(size,int) and not isinstance(size,bool) and 0<size<2**63 else None
    download = item.get("download_status","not_requested")
    if download not in {"downloaded","not_available","not_requested","failed","reference_only"}:
        download = "not_requested"
    if download == "downloaded" and not (filename and digest and size):
        download = "failed"
        filename = digest = size = None
    return {"item_id":item_id,"numeric_id":numeric_id,"source_url":source_url,"title":text("title",1000),
            "author":text("author",200),"cover_url":safe_cover(item.get("cover_url")),
            "published_at":text("published_at",64) or None,"likes":likes,
            "local_filename":filename,"sha256":digest,"size_bytes":size,"download_status":download,
            "download_reason":text("reason",500),"media_availability":"local_quarantine" if download=="downloaded" else "reference_only"}


class CollectionStore:
    def __init__(self,path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connection() as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript("""
                CREATE TABLE IF NOT EXISTS keyword_tasks (
                    id TEXT PRIMARY KEY,keyword TEXT NOT NULL,provider TEXT NOT NULL DEFAULT 'browser',
                    enabled INTEGER NOT NULL,interval_seconds INTEGER NOT NULL,max_items INTEGER NOT NULL,
                    publish_time INTEGER NOT NULL,sort_type INTEGER NOT NULL,
                    created_at REAL NOT NULL,updated_at REAL NOT NULL,next_run_at REAL NOT NULL,last_run_at REAL
                );
                CREATE TABLE IF NOT EXISTS collection_runs (
                    id TEXT PRIMARY KEY,task_id TEXT NOT NULL,provider TEXT NOT NULL,trigger TEXT NOT NULL,
                    status TEXT NOT NULL,created_at REAL NOT NULL,started_at REAL,finished_at REAL,
                    items_seen INTEGER NOT NULL DEFAULT 0,items_added INTEGER NOT NULL DEFAULT 0,
                    items_skipped INTEGER NOT NULL DEFAULT 0,pages INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT,message TEXT,lease_hash TEXT,lease_expires_at REAL,agent_id TEXT,task_snapshot TEXT NOT NULL DEFAULT '{}'
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_collection ON collection_runs(task_id)
                    WHERE status IN ('queued','running');
                CREATE TABLE IF NOT EXISTS reference_items (
                    id TEXT PRIMARY KEY,item_id TEXT,source_url TEXT UNIQUE,title TEXT NOT NULL,author TEXT NOT NULL,
                    cover_url TEXT,published_at TEXT,likes INTEGER,rights_status TEXT NOT NULL DEFAULT 'unknown',
                    rights_note TEXT NOT NULL DEFAULT '',source_kind TEXT NOT NULL,media_availability TEXT NOT NULL DEFAULT 'reference_only',
                    local_filename TEXT,sha256 TEXT,size_bytes INTEGER,download_status TEXT NOT NULL DEFAULT 'not_requested',
                    download_reason TEXT NOT NULL DEFAULT '',created_at REAL NOT NULL,updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS douyin_item_keys (item_key TEXT PRIMARY KEY,reference_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS task_references (
                    task_id TEXT NOT NULL,reference_id TEXT NOT NULL,first_seen REAL NOT NULL,last_seen REAL NOT NULL,
                    PRIMARY KEY(task_id,reference_id)
                );
                CREATE TABLE IF NOT EXISTS run_references (
                    run_id TEXT NOT NULL,reference_id TEXT NOT NULL,PRIMARY KEY(run_id,reference_id)
                );
                CREATE TABLE IF NOT EXISTS browser_agents (agent_id TEXT PRIMARY KEY,last_seen REAL NOT NULL,status TEXT NOT NULL);
            """)
            columns = {row["name"] for row in con.execute("PRAGMA table_info(collection_runs)")}
            if "task_snapshot" not in columns:
                con.execute("ALTER TABLE collection_runs ADD COLUMN task_snapshot TEXT NOT NULL DEFAULT '{}'")

    @contextmanager
    def connection(self):
        con = sqlite3.connect(self.path,timeout=20)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    @staticmethod
    def task_dict(row):
        if row is None:
            return None
        result = dict(row)
        result["enabled"] = bool(result["enabled"])
        return result

    @staticmethod
    def run_dict(row):
        if row is None:
            return None
        result = dict(row)
        result.pop("lease_hash",None)
        result["task_snapshot"] = json.loads(result.get("task_snapshot") or "{}")
        return result

    @staticmethod
    def item_dict(row):
        if row is None:
            return None
        return {**dict(row),"rights_verified":False,"local_media_verified":False}

    @staticmethod
    def validate_task(values):
        keyword = values.get("keyword")
        if not isinstance(keyword,str) or not keyword.strip() or len(keyword)>100:
            raise ValueError("关键词长度应为 1–100 个字符")
        if values.get("provider") not in PROVIDERS:
            raise ValueError("素材来源模式无效")
        for key,low,high in (("interval_seconds",3600,31*86400),("max_items",1,50),("publish_time",0,365),("sort_type",0,2)):
            if not isinstance(values.get(key),int) or isinstance(values[key],bool) or not low<=values[key]<=high:
                raise ValueError(f"{key} 超出支持范围")
        if not isinstance(values.get("enabled"),bool):
            raise ValueError("enabled 必须是布尔值")
        if values["provider"]=="browser" and (values["max_items"]>10 or values["publish_time"]!=0 or values["sort_type"]!=0):
            raise ValueError("浏览器采集每次最多 10 条，当前仅支持默认排序和不限发布时间")
        values["keyword"] = keyword.strip()
        return values

    def create_task(self,keyword,provider="browser",enabled=True,interval_seconds=86400,max_items=10,publish_time=0,sort_type=0,now=None):
        values = self.validate_task(dict(keyword=keyword,provider=provider,enabled=enabled,interval_seconds=interval_seconds,
                                         max_items=max_items,publish_time=publish_time,sort_type=sort_type))
        moment = time.time() if now is None else now
        task_id = uuid.uuid4().hex
        with self.connection() as con:
            con.execute("""INSERT INTO keyword_tasks(id,keyword,provider,enabled,interval_seconds,max_items,publish_time,sort_type,created_at,updated_at,next_run_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",(task_id,values["keyword"],provider,int(enabled),interval_seconds,max_items,publish_time,sort_type,moment,moment,moment))
        return self.get_task(task_id)

    def get_task(self,task_id):
        with self.connection() as con:
            return self.task_dict(con.execute("SELECT * FROM keyword_tasks WHERE id=?",(task_id,)).fetchone())

    def list_tasks(self):
        with self.connection() as con:
            tasks = []
            for row in con.execute("SELECT * FROM keyword_tasks ORDER BY created_at DESC LIMIT 500"):
                task = self.task_dict(row)
                task["last_run"] = self.run_dict(con.execute("SELECT * FROM collection_runs WHERE task_id=? ORDER BY created_at DESC LIMIT 1",(task["id"],)).fetchone())
                tasks.append(task)
            return tasks

    def update_task(self,task_id,changes:dict,now=None):
        allowed = {"keyword","provider","enabled","interval_seconds","max_items","publish_time","sort_type"}
        if set(changes)-allowed:
            raise ValueError("不支持的任务设置")
        moment = time.time() if now is None else now
        with self.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            old = self.task_dict(con.execute("SELECT * FROM keyword_tasks WHERE id=?",(task_id,)).fetchone())
            if not old:
                raise KeyError(task_id)
            merged = self.validate_task({**old,**changes})
            fields = {key:merged[key] for key in changes}
            fields["updated_at"] = moment
            if (changes.get("enabled") is True and not old["enabled"]) or "interval_seconds" in changes:
                fields["next_run_at"] = moment
            if "provider" in changes and changes["provider"] != old["provider"]:
                active = con.execute("SELECT 1 FROM collection_runs WHERE task_id=? AND status IN ('queued','running')",(task_id,)).fetchone()
                if active:
                    raise CollectionConflict("当前任务仍在排队或执行，结束后才能切换采集方式")
            con.execute("UPDATE keyword_tasks SET "+",".join(key+"=?" for key in fields)+" WHERE id=?",list(fields.values())+[task_id])
        return self.get_task(task_id)

    def _enqueue(self,con,task,trigger,moment):
        active = con.execute("SELECT * FROM collection_runs WHERE task_id=? AND status IN ('queued','running')",(task["id"],)).fetchone()
        if active:
            if trigger=="manual" and active["status"]=="queued" and active["trigger"]!="manual":
                con.execute("UPDATE collection_runs SET trigger='manual' WHERE id=?",(active["id"],))
                active = con.execute("SELECT * FROM collection_runs WHERE id=?",(active["id"],)).fetchone()
            return self.run_dict(active),False
        run_id = uuid.uuid4().hex
        snapshot = {key:task[key] for key in ("keyword","provider","max_items","publish_time","sort_type")}
        con.execute("INSERT INTO collection_runs(id,task_id,provider,trigger,status,created_at,task_snapshot) VALUES(?,?,?,?,?,?,?)",
                    (run_id,task["id"],task["provider"],trigger,"queued",moment,json.dumps(snapshot,ensure_ascii=False)))
        return self.run_dict(con.execute("SELECT * FROM collection_runs WHERE id=?",(run_id,)).fetchone()),True

    def enqueue(self,task_id,trigger="manual",now=None):
        moment = time.time() if now is None else now
        with self.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            task = con.execute("SELECT * FROM keyword_tasks WHERE id=?",(task_id,)).fetchone()
            if not task:
                raise KeyError(task_id)
            return self._enqueue(con,task,trigger,moment)

    def queue_due(self,now=None):
        moment = time.time() if now is None else now
        queued = []
        with self.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            tasks = con.execute("SELECT * FROM keyword_tasks WHERE enabled=1 AND next_run_at<=? ORDER BY next_run_at LIMIT 100",(moment,)).fetchall()
            for task in tasks:
                run,created = self._enqueue(con,task,"scheduled",moment)
                next_due = task["next_run_at"] + (int((moment-task["next_run_at"])/task["interval_seconds"])+1)*task["interval_seconds"]
                con.execute("UPDATE keyword_tasks SET next_run_at=? WHERE id=?",(next_due,task["id"]))
                if created:
                    queued.append(run)
        return queued

    def list_runs(self,task_id=None,limit=50):
        with self.connection() as con:
            if task_id:
                rows = con.execute("SELECT * FROM collection_runs WHERE task_id=? ORDER BY created_at DESC LIMIT ?",(task_id,limit))
            else:
                rows = con.execute("SELECT * FROM collection_runs ORDER BY created_at DESC LIMIT ?",(limit,))
            return [self.run_dict(row) for row in rows]

    def get_run(self,run_id):
        with self.connection() as con:
            return self.run_dict(con.execute("SELECT * FROM collection_runs WHERE id=?",(run_id,)).fetchone())

    def recover(self,now=None):
        moment = time.time() if now is None else now
        with self.connection() as con:
            # Browser work runs elsewhere: a server restart preserves its valid lease.
            con.execute("UPDATE collection_runs SET status='interrupted',finished_at=?,error_code='service_restarted',message='采集服务重启，本次执行已中断；定时任务仍保留' WHERE status='running' AND provider='official'",(moment,))
        self.expire_leases(moment)

    def expire_leases(self,now=None):
        moment = time.time() if now is None else now
        with self.connection() as con:
            con.execute("UPDATE collection_runs SET status='interrupted',finished_at=?,error_code='lease_expired',message='本机浏览器代理未在期限内回报；不会自动重放本次执行' WHERE provider='browser' AND status='running' AND lease_expires_at<=?",(moment,moment))

    def claim_official(self,now=None):
        moment = time.time() if now is None else now
        with self.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT r.* FROM collection_runs r JOIN keyword_tasks t ON t.id=r.task_id WHERE r.status='queued' AND r.provider='official' AND (t.enabled=1 OR r.trigger='manual') ORDER BY r.created_at LIMIT 1").fetchone()
            if not row:
                return None
            con.execute("UPDATE collection_runs SET status='running',started_at=? WHERE id=?",(moment,row["id"]))
            con.execute("UPDATE keyword_tasks SET last_run_at=? WHERE id=?",(moment,row["task_id"]))
            return self.run_dict(con.execute("SELECT * FROM collection_runs WHERE id=?",(row["id"],)).fetchone())

    def finish_run(self,run_id,status,*,items_seen=0,items_added=0,items_skipped=0,pages=0,error_code=None,message=None,now=None):
        if status not in {"succeeded","failed","not_configured","interrupted"}:
            raise ValueError("无效的执行结果")
        moment = time.time() if now is None else now
        with self.connection() as con:
            changed = con.execute("UPDATE collection_runs SET status=?,finished_at=?,items_seen=?,items_added=?,items_skipped=?,pages=?,error_code=?,message=? WHERE id=? AND status='running'",
                (status,moment,items_seen,items_added,items_skipped,pages,error_code,message,run_id)).rowcount
            if not changed:
                raise CollectionConflict("任务已经结束，不能重复提交结果")
        return self.get_run(run_id)

    def _upsert_item(self,con,item,task_id=None,run_id=None,rights_status="unknown",rights_note="",source_kind="search",now=None):
        data = cleaned_item(item)
        moment = time.time() if now is None else now
        keys = list(dict.fromkeys(key for key in (data["item_id"],data.pop("numeric_id")) if key))
        existing = None
        for key in keys:
            matched = con.execute("SELECT r.* FROM reference_items r JOIN douyin_item_keys k ON k.reference_id=r.id WHERE k.item_key=?",(key,)).fetchone()
            if matched:
                if existing and existing["id"]!=matched["id"]:
                    raise ValueError("作品标识与已记录的来源冲突")
                existing = matched
        if existing is None and data["source_url"]:
            existing = con.execute("SELECT * FROM reference_items WHERE source_url=?",(data["source_url"],)).fetchone()
        if existing and existing["source_url"] and data["source_url"] and existing["source_url"]!=data["source_url"]:
            raise ValueError("同一作品标识对应了不同来源链接")
        added = existing is None
        reference_id = uuid.uuid4().hex if added else existing["id"]
        if added:
            values = {"id":reference_id,**data,"rights_status":rights_status,"rights_note":rights_note,"source_kind":source_kind,"created_at":moment,"updated_at":moment}
            con.execute("INSERT INTO reference_items("+",".join(values)+") VALUES("+",".join("?" for _ in values)+")",list(values.values()))
        else:
            # Recollection may refresh metadata; it never grants or resets rights.
            changes = {key:value for key,value in data.items() if value is not None and value!="" and key not in {"item_id","source_url"}}
            if existing["download_status"] == "downloaded" and data["download_status"] != "downloaded":
                for key in ("local_filename","sha256","size_bytes","download_status","download_reason","media_availability"):
                    changes.pop(key,None)
            if not existing["source_url"] and data["source_url"]:
                changes["source_url"] = data["source_url"]
            changes["updated_at"] = moment
            con.execute("UPDATE reference_items SET "+",".join(key+"=?" for key in changes)+" WHERE id=?",list(changes.values())+[reference_id])
        for key in keys:
            con.execute("INSERT OR IGNORE INTO douyin_item_keys(item_key,reference_id) VALUES(?,?)",(key,reference_id))
        if task_id:
            con.execute("INSERT INTO task_references(task_id,reference_id,first_seen,last_seen) VALUES(?,?,?,?) ON CONFLICT(task_id,reference_id) DO UPDATE SET last_seen=excluded.last_seen",(task_id,reference_id,moment,moment))
        if run_id:
            con.execute("INSERT OR IGNORE INTO run_references(run_id,reference_id) VALUES(?,?)",(run_id,reference_id))
        return self.item_dict(con.execute("SELECT * FROM reference_items WHERE id=?",(reference_id,)).fetchone()),added

    def add_item(self,item,**kwargs):
        if kwargs.get("rights_status","unknown") not in RIGHTS:
            raise ValueError("素材授权状态无效")
        with self.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            return self._upsert_item(con,item,**kwargs)

    def list_items(self,task_id=None,rights_status=None,q=None,limit=100,offset=0):
        conditions,args = [],[]
        if task_id:
            conditions.append("r.id IN (SELECT reference_id FROM task_references WHERE task_id=?)")
            args.append(task_id)
        if rights_status:
            conditions.append("r.rights_status=?")
            args.append(rights_status)
        if q:
            conditions.append("(r.title LIKE ? OR r.author LIKE ?)")
            args.extend(["%"+q+"%"]*2)
        where = " WHERE "+" AND ".join(conditions) if conditions else ""
        with self.connection() as con:
            rows = con.execute("SELECT r.* FROM reference_items r"+where+" ORDER BY r.updated_at DESC LIMIT ? OFFSET ?",args+[limit,offset])
            return [self.item_dict(row) for row in rows]

    def update_rights(self,reference_id,rights_status=None,rights_note=None):
        if rights_status is not None and rights_status not in RIGHTS:
            raise ValueError("素材授权状态无效")
        if rights_note is not None and (not isinstance(rights_note,str) or len(rights_note)>2000):
            raise ValueError("授权备注最多 2000 个字符")
        with self.connection() as con:
            row = con.execute("SELECT * FROM reference_items WHERE id=?",(reference_id,)).fetchone()
            if row is None:
                raise KeyError(reference_id)
            con.execute("UPDATE reference_items SET rights_status=?,rights_note=?,updated_at=? WHERE id=?",
                        (rights_status if rights_status is not None else row["rights_status"],rights_note if rights_note is not None else row["rights_note"],time.time(),reference_id))
            return self.item_dict(con.execute("SELECT * FROM reference_items WHERE id=?",(reference_id,)).fetchone())

    def browser_status(self):
        moment = time.time()
        with self.connection() as con:
            active = con.execute("SELECT r.id,r.agent_id,r.lease_expires_at,a.last_seen FROM collection_runs r JOIN browser_agents a ON a.agent_id=r.agent_id WHERE r.provider='browser' AND r.status='running' AND r.lease_expires_at>? ORDER BY r.started_at DESC LIMIT 1",(moment,)).fetchone()
            if active:
                return {"status":"online","busy":True,"last_seen_at":active["last_seen"],"agent_id":active["agent_id"],
                        "active_run_id":active["id"],"lease_expires_at":active["lease_expires_at"]}
            row = con.execute("SELECT * FROM browser_agents ORDER BY last_seen DESC LIMIT 1").fetchone()
            if row is None:
                return {"status":"local_agent_required","busy":False,"last_seen_at":None}
            return {"status":"online" if moment-row["last_seen"]<90 else "offline","busy":False,"last_seen_at":row["last_seen"],"agent_id":row["agent_id"]}

    def claim_browser(self,agent_id,now=None):
        if not isinstance(agent_id,str) or not 1<=len(agent_id)<=100 or any(ord(c)<32 for c in agent_id):
            raise ValueError("浏览器代理编号无效")
        moment = time.time() if now is None else now
        self.expire_leases(moment)
        self.queue_due(moment)
        with self.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("INSERT INTO browser_agents VALUES(?,?,'online') ON CONFLICT(agent_id) DO UPDATE SET last_seen=excluded.last_seen,status='online'",(agent_id,moment))
            if con.execute("SELECT 1 FROM collection_runs WHERE status='running' AND provider='browser' AND agent_id=? AND lease_expires_at>?",(agent_id,moment)).fetchone():
                return None
            row = con.execute("SELECT r.* FROM collection_runs r JOIN keyword_tasks t ON t.id=r.task_id WHERE r.status='queued' AND r.provider='browser' AND (t.enabled=1 OR r.trigger='manual') ORDER BY r.created_at LIMIT 1").fetchone()
            if row is None:
                return None
            token = secrets.token_urlsafe(32)
            con.execute("UPDATE collection_runs SET status='running',started_at=?,agent_id=?,lease_hash=?,lease_expires_at=? WHERE id=?",(moment,agent_id,hashlib.sha256(token.encode()).hexdigest(),moment+900,row["id"]))
            con.execute("UPDATE keyword_tasks SET last_run_at=? WHERE id=?",(moment,row["task_id"]))
            task = json.loads(row["task_snapshot"] or "{}") or dict(con.execute("SELECT * FROM keyword_tasks WHERE id=?",(row["task_id"],)).fetchone())
            result = self.run_dict(con.execute("SELECT * FROM collection_runs WHERE id=?",(row["id"],)).fetchone())
            result.update({key:task[key] for key in ("keyword","max_items","publish_time","sort_type")})
            result["lease_token"] = token
            return result

    def _lease(self,con,run_id,token,moment,allow_completed=False):
        row = con.execute("SELECT * FROM collection_runs WHERE id=?",(run_id,)).fetchone()
        digest = hashlib.sha256(token.encode()).hexdigest() if isinstance(token,str) and len(token)<=200 else ""
        if row is None or row["provider"] != "browser" or not row["lease_hash"] or not hmac.compare_digest(row["lease_hash"],digest):
            raise CollectionConflict("浏览器执行凭据无效")
        if allow_completed and row["status"] == "succeeded":
            return row
        if row["status"] != "running" or (row["lease_expires_at"] or 0)<=moment:
            raise CollectionConflict("浏览器执行已结束或超时；请查看本次记录")
        return row

    def complete_browser(self,run_id,token,items,now=None):
        if not isinstance(items,list) or len(items)>50:
            raise ValueError("每次最多提交 50 条素材元数据")
        moment = time.time() if now is None else now
        with self.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            row = self._lease(con,run_id,token,moment,allow_completed=True)
            if row["status"] == "succeeded":
                return self.run_dict(row)
            task = json.loads(row["task_snapshot"] or "{}") or dict(con.execute("SELECT * FROM keyword_tasks WHERE id=?",(row["task_id"],)).fetchone())
            if len(items)>task["max_items"]:
                raise ValueError("提交数量超过任务设定上限")
            added,skipped = 0,0
            for item in items:
                try:
                    _,created = self._upsert_item(con,item,task_id=row["task_id"],run_id=run_id,source_kind="browser",now=moment)
                    added += int(created)
                except ValueError:
                    skipped += 1
            all_invalid = bool(items) and skipped==len(items)
            con.execute("UPDATE collection_runs SET status=?,finished_at=?,items_seen=?,items_added=?,items_skipped=?,pages=1,error_code=?,message=? WHERE id=?",
                        ("failed" if all_invalid else "succeeded",moment,len(items),added,skipped,"invalid_results" if all_invalid else None,
                         "浏览器返回的素材元数据均无效，本次没有入库" if all_invalid else "浏览器回报已保存；授权状态未确认，本地文件尚未由服务器核验",run_id))
            con.execute("UPDATE browser_agents SET last_seen=? WHERE agent_id=?",(moment,row["agent_id"]))
            return self.run_dict(con.execute("SELECT * FROM collection_runs WHERE id=?",(run_id,)).fetchone())

    def fail_browser(self,run_id,token,code,items=None,now=None):
        messages = {"login_required":"请在本机浏览器完成登录", "captcha_required":"请在本机浏览器手动完成验证",
                    "download_unavailable":"当前作品未提供可用下载入口", "browser_unavailable":"本机浏览器连接不可用",
                    "collection_failed":"浏览器采集未完成，请检查本机代理", "deadline_exceeded":"本次浏览器任务超时"}
        if code not in messages:
            raise ValueError("不支持的浏览器错误码")
        items = [] if items is None else items
        if not isinstance(items,list) or len(items)>50:
            raise ValueError("每次最多提交 50 条素材元数据")
        moment = time.time() if now is None else now
        with self.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            row = self._lease(con,run_id,token,moment)
            task = json.loads(row["task_snapshot"] or "{}") or dict(con.execute("SELECT * FROM keyword_tasks WHERE id=?",(row["task_id"],)).fetchone())
            if len(items)>task["max_items"]:
                raise ValueError("提交数量超过任务设定上限")
            added,skipped = 0,0
            for item in items:
                try:
                    _,created = self._upsert_item(con,item,task_id=row["task_id"],run_id=run_id,source_kind="browser",now=moment)
                    added += int(created)
                except ValueError:
                    skipped += 1
            message = messages[code]
            if items:
                message += f"；已保存 {len(items)-skipped} 条已取得记录，授权状态仍需确认"
            con.execute("UPDATE collection_runs SET status='failed',finished_at=?,error_code=?,message=?,items_seen=?,items_added=?,items_skipped=?,pages=? WHERE id=?",
                        (moment,code,message,len(items),added,skipped,1 if items else 0,run_id))
            con.execute("UPDATE browser_agents SET last_seen=? WHERE agent_id=?",(moment,row["agent_id"]))
            return self.run_dict(con.execute("SELECT * FROM collection_runs WHERE id=?",(run_id,)).fetchone())

    def heartbeat(self,run_id,token,now=None):
        moment = time.time() if now is None else now
        with self.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            row = self._lease(con,run_id,token,moment)
            con.execute("UPDATE collection_runs SET lease_expires_at=? WHERE id=?",(moment+900,run_id))
            con.execute("UPDATE browser_agents SET last_seen=? WHERE agent_id=?",(moment,row["agent_id"]))
        return {"lease_expires_at":moment+900}
