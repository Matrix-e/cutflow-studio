"""Authenticated, idempotent brand screenshot storage, separate from brand facts."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re
import threading
from uuid import UUID, uuid4
import warnings

from fastapi import Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from PIL import Image, ImageOps, UnidentifiedImageError
from starlette.concurrency import run_in_threadpool

MAX_BYTES=8*1024**2
MAX_IMAGES=64
MAX_PIXELS=16_000_000
MAX_STORED_BYTES=80*1024**2
_lock=threading.RLock()
_ID=re.compile(r'^[0-9a-f]{32}$')
_SHA=re.compile(r'^[0-9a-f]{64}$')
EVIDENCE_KINDS={'miniapp_screenshot','website_screenshot','brand_document','unknown'}
CAPTURE_MODES={'local_miniapp','published_miniapp','website','document','unknown'}


def _root(data):
    parent=Path(data).resolve()
    folder=parent/'brand-screenshots'
    if folder.is_symlink() or (folder.exists() and not folder.resolve().is_relative_to(parent)):
        raise HTTPException(409,'截图存储位置无效')
    folder.mkdir(parents=True,exist_ok=True)
    return folder


def _records(folder):
    manifest=folder/'manifest.json'
    if not manifest.exists():return []
    if manifest.is_symlink():raise HTTPException(409,'截图清单无效')
    try:
        items=json.loads(manifest.read_text(encoding='utf-8'))
        if not isinstance(items,list) or len(items)>MAX_IMAGES:raise ValueError()
        seen=set()
        for item in items:
            if (not isinstance(item,dict) or not _ID.fullmatch(item.get('id',''))
                    or item['id'] in seen or not isinstance(item.get('name'),str)
                    or not isinstance(item.get('sha256'),str)):
                raise ValueError()
            seen.add(item['id'])
        return items
    except (OSError,ValueError,TypeError):
        raise HTTPException(409,'截图清单暂不可读取，请检查存储') from None


def _public(item):
    result={'id':item['id'],'name':item['name'],
            'url':'/api/brand-context/screenshots/'+item['id'],
            'width':item['width'],'height':item['height'],
            'content_type':'image/png','created_at':item['created_at'],
            'sha256':item.get('content_sha256'), 'evidence_revision':item.get('evidence_revision',0)}
    if isinstance(item.get('evidence'),dict):
        for key in ('kind','capture_mode','page_route','source_reference','source_sha256','fact_ids','purpose','verification_scope','review_status'):
            if key in item['evidence']:result[key]=item['evidence'][key]
    return result


def list_screenshots(data):
    with _lock:
        folder=_root(data)
        return [_public(item) for item in reversed(_records(folder))]


def resolve_screenshot(data, screenshot_id, expected_sha256=None):
    if not isinstance(screenshot_id,str) or not _ID.fullmatch(screenshot_id):
        raise HTTPException(404,'截图不存在')
    with _lock:
        folder=_root(data)
        item=next((item for item in _records(folder) if item['id']==screenshot_id),None)
        if item is None:
            raise HTTPException(404,'截图不存在')
        path=folder/(screenshot_id+'.png')
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(folder.resolve()):
            raise HTTPException(404,'截图文件不可用')
        if path.stat().st_size>MAX_STORED_BYTES:raise HTTPException(409,'截图文件已变化，请重新核对')
        digest=hashlib.sha256(path.read_bytes()).hexdigest()
        stored=item.get('content_sha256')
        if stored is not None and (not isinstance(stored,str) or not _SHA.fullmatch(stored) or digest!=stored):
            raise HTTPException(409,'截图文件已变化，请重新核对')
        if expected_sha256 is not None and (not isinstance(expected_sha256,str) or not _SHA.fullmatch(expected_sha256) or digest!=expected_sha256):
            raise HTTPException(409,'截图与制作版本不一致，请重新选择')
        return path


def _field(value,limit,label):
    if not isinstance(value,str) or len(value)>limit or any(ord(char)<32 for char in value):
        raise HTTPException(400,label+'格式无效')
    return value.strip()


def _evidence_metadata(metadata, confirmed_ids):
    fields={'kind','capture_mode','page_route','source_reference','source_sha256','fact_ids','purpose','verification_scope','review_status'}
    if not isinstance(metadata,dict) or set(metadata)-fields:
        raise HTTPException(400,'截图依据字段无效')
    kind=metadata.get('kind','unknown');capture=metadata.get('capture_mode','unknown')
    status=metadata.get('review_status','needs_review')
    if (not all(isinstance(value,str) for value in (kind,capture,status))
            or kind not in EVIDENCE_KINDS or capture not in CAPTURE_MODES or status not in {'confirmed','needs_review'}):
        raise HTTPException(400,'截图来源类型或核验状态无效')
    references=metadata.get('fact_ids',[])
    if not isinstance(references,list) or len(references)>16:
        raise HTTPException(400,'截图最多关联16条已确认事实')
    fact_ids=list(dict.fromkeys(_field(value,80,'事实编号') for value in references))
    if any(value not in confirmed_ids for value in fact_ids):
        raise HTTPException(400,'截图只能关联当前知识库中已确认的事实')
    source_hash=metadata.get('source_sha256','')
    if not isinstance(source_hash,str) or (source_hash and not _SHA.fullmatch(source_hash)):
        raise HTTPException(400,'来源文件校验值无效')
    result={'kind':kind,'capture_mode':capture,'review_status':status,'fact_ids':fact_ids,
            'page_route':_field(metadata.get('page_route',''),300,'页面路径'),
            'source_reference':_field(metadata.get('source_reference',''),600,'截图来源'),
            'source_sha256':source_hash,
            'purpose':_field(metadata.get('purpose',''),500,'画面用途'),
            'verification_scope':_field(metadata.get('verification_scope',''),700,'核验范围')}
    if kind=='miniapp_screenshot' and capture not in {'local_miniapp','published_miniapp'}:
        raise HTTPException(400,'小程序截图必须明确本地小程序或已发布小程序来源，不能使用官网代替')
    if status=='confirmed':
        if kind=='unknown' or capture=='unknown' or not all(result[key] for key in ('source_reference','purpose','verification_scope')) or not fact_ids:
            raise HTTPException(400,'确认截图需填写真实来源、用途、核验范围并关联已确认事实')
        if kind=='miniapp_screenshot' and not result['page_route']:
            raise HTTPException(400,'确认小程序截图需填写实际页面路径')
    return result


def bind_screenshot(data,screenshot_id,metadata,*,expected_sha256):
    """Bind a reviewed image to saved facts without certifying business outcomes.

    The caller must have inspected the exact image digest. This operation does
    not promote a fact's status and cannot make a website into a miniapp image.
    """
    from .brand_context import load_context
    confirmed={fact['id'] for fact in load_context(data)['facts'] if fact['status']=='confirmed'}
    evidence=_evidence_metadata(metadata,confirmed)
    if not isinstance(expected_sha256,str) or not _SHA.fullmatch(expected_sha256):
        raise HTTPException(400,'保存截图依据需要当前图片校验值')
    with _lock:
        folder=_root(data);records=_records(folder)
        item=next((record for record in records if record['id']==screenshot_id),None)
        if item is None:raise HTTPException(404,'截图不存在')
        path=resolve_screenshot(data,screenshot_id,expected_sha256)
        try:
            with Image.open(path) as image:
                if image.format!='PNG' or image.size!=(item['width'],item['height']):
                    raise ValueError('changed image')
                image.verify()
        except (OSError,ValueError,UnidentifiedImageError):
            raise HTTPException(409,'截图文件无法核验，请重新上传有效图片') from None
        updated={**item,'content_sha256':expected_sha256,'evidence':evidence,
                 'evidence_revision':int(item.get('evidence_revision',0))+1,
                 'evidence_updated_at':datetime.now(timezone.utc).isoformat()}
        if item.get('evidence')==evidence and item.get('content_sha256')==expected_sha256:return _public(item)
        stage=folder/('.manifest-'+uuid4().hex+'.tmp')
        try:
            stage.write_text(json.dumps([updated if value['id']==screenshot_id else value for value in records],ensure_ascii=False),encoding='utf-8')
            stage.replace(folder/'manifest.json')
        except OSError:
            raise HTTPException(503,'截图依据保存失败，原图片和原记录保留，请稍后重试') from None
        finally:stage.unlink(missing_ok=True)
        return _public(updated)


def production_assets(data,fact_ids=None):
    """Available, reviewed miniapp screenshots only; no local paths in prompts."""
    from .brand_context import load_context
    confirmed={fact['id'] for fact in load_context(data)['facts'] if fact['status']=='confirmed'}
    allowed=confirmed if fact_ids is None else confirmed & set(fact_ids)
    result=[]
    with _lock:
        for item in reversed(_records(_root(data))):
            evidence=item.get('evidence')
            if not isinstance(evidence,dict) or evidence.get('review_status')!='confirmed' or evidence.get('kind')!='miniapp_screenshot':continue
            try:
                metadata=_evidence_metadata(evidence,confirmed)
                related=[value for value in metadata['fact_ids'] if value in allowed]
                if not related or not _SHA.fullmatch(str(item.get('content_sha256',''))):continue
                path=resolve_screenshot(data,item['id'],item['content_sha256'])
                with Image.open(path) as image:
                    if image.format!='PNG' or image.size!=(item['width'],item['height']):continue
                    image.verify()
                result.append({**_public(item),'fact_ids':related})
                if len(result)>=12:break
            except (HTTPException,OSError,ValueError,TypeError,UnidentifiedImageError):continue
    return result


def _normalize(raw):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error',Image.DecompressionBombWarning)
            with Image.open(BytesIO(raw)) as image:
                if image.format not in {'PNG','JPEG','WEBP'} or getattr(image,'n_frames',1)!=1:
                    raise ValueError('unsupported')
                if image.width<1 or image.height<1 or image.width*image.height>MAX_PIXELS:
                    raise ValueError('pixels')
                image.load()
                image=ImageOps.exif_transpose(image)
                # Copy pixels into a fresh image so PNG chunks/EXIF/ICC metadata
                # cannot survive conversion, even when uploaded bytes are PNG.
                rgba=image.convert('RGBA')
                clean=Image.frombytes('RGBA',rgba.size,rgba.tobytes())
                buffer=BytesIO();clean.save(buffer,format='PNG',optimize=True)
                return buffer.getvalue(),clean.width,clean.height
    except (UnidentifiedImageError,OSError,ValueError,Image.DecompressionBombWarning,Image.DecompressionBombError):
        raise HTTPException(400,'请上传有效的静态 PNG、JPEG 或 WebP 截图，图片不超过 1600 万像素') from None


def _store(data, name, request_id, raw):
    try:
        identifier=UUID(request_id)
        if str(identifier)!=request_id:raise ValueError()
    except (ValueError,TypeError,AttributeError):
        raise HTTPException(400,'上传编号必须是标准 UUID') from None
    if not name.strip() or any(ord(char)<32 for char in name):
        raise HTTPException(400,'请填写截图名称，不要包含控制字符')
    if not 0<len(raw)<=MAX_BYTES:raise HTTPException(413,'截图大小须在 1 字节到 8 MB 之间')
    digest=hashlib.sha256(raw).hexdigest();sid=identifier.hex
    with _lock:
        folder=_root(data);records=_records(folder)
        existing=next((record for record in records if record['id']==sid),None)
        if existing:
            if existing['sha256']!=digest:
                raise HTTPException(409,'同一上传编号对应的图片已变化，请重新选择截图')
            resolve_screenshot(data,sid)
            return _public(existing)
        if len(records)>=MAX_IMAGES:raise HTTPException(409,'最多保存 64 张品牌截图')
        encoded,width,height=_normalize(raw)
        record={'id':sid,'name':name.strip(),'sha256':digest,'content_sha256':hashlib.sha256(encoded).hexdigest(),'width':width,'height':height,
                'created_at':datetime.now(timezone.utc).isoformat()}
        target=folder/(sid+'.png')
        if target.exists() or target.is_symlink():raise HTTPException(409,'上传编号存在未完成记录，请使用新的编号')
        staging=folder/('.'+uuid4().hex+'.tmp')
        manifest_stage=folder/('.manifest-'+uuid4().hex+'.tmp')
        try:
            staging.write_bytes(encoded)
            staging.replace(target)
            manifest_stage.write_text(json.dumps([*records,record],ensure_ascii=False),encoding='utf-8')
            manifest_stage.replace(folder/'manifest.json')
        except OSError:
            # Only the new, internally generated target is removed on failure.
            target.unlink(missing_ok=True)
            raise HTTPException(503,'截图保存失败，请稍后重试') from None
        finally:
            staging.unlink(missing_ok=True);manifest_stage.unlink(missing_ok=True)
        return _public(record)


def register_brand_media_routes(app,authorize,data):
    @app.get('/api/brand-context/screenshots',dependencies=[Depends(authorize)])
    def screenshots():
        return {'screenshots':list_screenshots(data)}

    @app.post('/api/brand-context/screenshots',dependencies=[Depends(authorize)])
    async def upload_screenshot(request:Request,name:str=Query(min_length=1,max_length=160),
                                request_id:str=Query(min_length=1,max_length=64)):
        length=request.headers.get('content-length')
        if length:
            try:
                if int(length)>MAX_BYTES:raise HTTPException(413,'截图最大 8 MB')
            except ValueError:raise HTTPException(400,'上传长度无效') from None
        content=bytearray()
        async for chunk in request.stream():
            if len(content)+len(chunk)>MAX_BYTES:raise HTTPException(413,'截图最大 8 MB')
            content.extend(chunk)
        item=await run_in_threadpool(_store,data,name,request_id,bytes(content))
        return {'screenshot':item}

    @app.get('/api/brand-context/screenshots/{screenshot_id}',dependencies=[Depends(authorize)])
    def screenshot_file(screenshot_id:str):
        return FileResponse(resolve_screenshot(data,screenshot_id),media_type='image/png',
                            headers={'Cache-Control':'no-store'},content_disposition_type='inline')

    @app.put('/api/brand-context/screenshots/{screenshot_id}/evidence',dependencies=[Depends(authorize)])
    async def save_screenshot_evidence(screenshot_id:str,request:Request):
        raw=bytearray()
        async for chunk in request.stream():
            if len(raw)+len(chunk)>12000:raise HTTPException(413,'截图依据内容过长')
            raw.extend(chunk)
        try:body=json.loads(raw)
        except (ValueError,UnicodeDecodeError):raise HTTPException(400,'截图依据格式无效') from None
        if not isinstance(body,dict) or set(body)!={'expected_sha256','evidence'}:raise HTTPException(400,'截图依据格式无效')
        item=await run_in_threadpool(bind_screenshot,data,screenshot_id,body['evidence'],expected_sha256=body['expected_sha256'])
        return {'screenshot':item}
