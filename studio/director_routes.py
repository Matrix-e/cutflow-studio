"""Authenticated API for a complete, persistent automatic production."""
from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

from .director_service import DirectorService


class ProductionOptions(BaseModel):
    model_config = ConfigDict(extra='forbid')
    aspect: Literal['9:16','16:9','1:1','original'] = '9:16'
    subtitles: StrictBool = True
    music: StrictBool = True
    target_duration: float | None = Field(default=45, ge=5, le=180, allow_inf_nan=False)
    audience: str = Field(default='', max_length=500)
    platform: str = Field(default='微信视频号', max_length=100)
    voice_style: Literal['professional','warm','energetic'] = 'warm'
    presenter_id: str | None = Field(default=None, max_length=120, pattern=r'^[A-Za-z0-9_-]+$')
    consent_confirmed: StrictBool = False
    brand_usage: Literal['auto','off','required'] = 'auto'
    brand_screenshot_ids: list[Annotated[str, Field(pattern=r'^[0-9a-f]{32}$')]] = Field(default_factory=list,max_length=4)

    @field_validator('brand_screenshot_ids')
    @classmethod
    def unique_screenshots(cls,value):
        if len(set(value))!=len(value):raise ValueError('同一截图只需选择一次')
        return value

    @field_validator('target_duration', mode='before')
    @classmethod
    def numeric_duration(cls, value):
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int,float))):
            raise ValueError('目标时长必须为秒数')
        return value


class NewProduction(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: UUID
    sources: list[Annotated[str, Field(min_length=1,max_length=4096)]] = Field(default_factory=list,max_length=12)
    goal: str = Field(default='',max_length=4000)
    mode: Literal['auto','vlog','story','narrated','presenter'] = 'auto'
    options: ProductionOptions = Field(default_factory=ProductionOptions)
    title: str = Field(default='',max_length=160)

    @field_validator('goal','title')
    @classmethod
    def clean_text(cls, value):
        if any(ord(c)<32 and c not in '\n\r\t' for c in value):raise ValueError('文字中包含无效控制字符')
        return value.strip()


class ReviseProduction(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: UUID
    instruction: str = Field(min_length=1,max_length=2000)
    options: ProductionOptions | None = None

    @field_validator('instruction')
    @classmethod
    def nonempty(cls, value):
        value=value.strip()
        if not value or any(ord(c)<32 and c not in '\n\r\t' for c in value):raise ValueError('请填写具体修改意见')
        return value


class RetryProduction(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: UUID


def register_director_routes(app, authorize, inbox, data, *, source_validator,
                            source_allowed=None, brand_snapshot=None, settings_loader=None,
                            preflight=None, quality_checker=None, **service_options):
    service=DirectorService(data,inbox,validate_source=source_validator,source_allowed=source_allowed,
                           brand_snapshot=brand_snapshot,settings_loader=settings_loader,
                           preflight=preflight,quality_checker=quality_checker,**service_options)

    @app.get('/api/productions/preflight',dependencies=[Depends(authorize)])
    def production_preflight(mode:Literal['auto','vlog','story','narrated','presenter']='auto'):
        sources,_=service.choose_sources([]) if mode!='presenter' else ([],{})
        paths=[]
        for source in sources:
            try:paths.append(source_validator(service.inbox,source))
            except (OSError,ValueError,HTTPException):pass
        return {'preflight':service.preflight(mode,ProductionOptions().model_dump(),paths,active=False)}

    @app.get('/api/productions',dependencies=[Depends(authorize)])
    def productions(limit:int=Query(default=40,ge=1,le=100)):
        return {'productions':[service.public(row) for row in service.list(limit)]}

    @app.post('/api/productions',dependencies=[Depends(authorize)])
    def new_production(body:NewProduction):
        request=body.model_dump(mode='json',exclude={'request_id'})
        request['goal']=request['goal'] or '根据这些真实素材，选择最有内容价值且素材能支撑的主题，制作一条表达清楚、保留真实细节的视频。'
        request['parent_id']=None;request['revision']=None
        return {'production':service.public(service.create(body.request_id,request))}

    @app.get('/api/productions/{identifier}',dependencies=[Depends(authorize)])
    def get_production(identifier:UUID):
        record=service.get(str(identifier))
        if not record:raise HTTPException(404,'制作任务不存在。')
        return {'production':service.public(record)}

    @app.post('/api/productions/{identifier}/revise',dependencies=[Depends(authorize)])
    def revise_production(identifier:UUID,body:ReviseProduction):
        options=body.options.model_dump(exclude_unset=True) if body.options else None
        return {'production':service.public(service.revise(str(identifier),body.request_id,body.instruction,options))}

    @app.post('/api/productions/{identifier}/retry',dependencies=[Depends(authorize)])
    def retry_production(identifier:UUID,body:RetryProduction):
        return {'production':service.public(service.retry(str(identifier),body.request_id))}

    @app.get('/api/productions/{identifier}/video',dependencies=[Depends(authorize)])
    def production_video(identifier:UUID,download:bool=False):
        path,item=service.artifact_path(str(identifier),'video')
        return FileResponse(path,media_type='video/mp4',filename=item['name'],
                            content_disposition_type='attachment' if download else 'inline')

    @app.get('/api/productions/{identifier}/artifacts/{kind}',dependencies=[Depends(authorize)])
    def production_artifact(identifier:UUID,kind:Literal['subtitles','cover','copy','record','package']):
        path,item=service.artifact_path(str(identifier),kind)
        content_type={'subtitles':'application/x-subrip','cover':'image/png','copy':'text/plain; charset=utf-8',
                      'record':'application/json','package':'application/zip'}[kind]
        return FileResponse(path,media_type=content_type,filename=item['name'],content_disposition_type='attachment')

    return service
