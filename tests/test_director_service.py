import copy
import hashlib
import json
import threading
import time
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Header
from fastapi.testclient import TestClient
import pytest

from studio.director_service import DirectorService, DirectorServiceError, MAX_ATTEMPTS, MAX_SECONDS
from studio.director_routes import register_director_routes, ProductionOptions


def source_validator(inbox, relative):
    path=(inbox/relative).resolve()
    if not path.is_relative_to(inbox.resolve()) or not path.is_file():raise ValueError('unsafe')
    return path


class Pipeline:
    def __init__(self):
        self.calls=[]; self.blockers=[]; self.failure=None; self.quality_pass=True
        self.plan_inputs=[]; self.quality_inputs=[]
    def preflight(self,paths,mode,options,**kwargs):
        self.calls.append('preflight')
        return {'ready':not self.blockers,'checks':self.blockers,'blockers':self.blockers}
    def analyze(self,paths,**kwargs):
        self.calls.append('analysis')
        if self.failure=='analysis':raise DirectorServiceError('analysis_unavailable')
        from studio.director_media import ANALYSIS_VERSION, ANALYSIS_STRATEGY
        return {'sources':[{'index':i,'name':p.name} for i,p in enumerate(paths)],'speech':[{'text':'事实来自现场。'}],
                'analysis_version':ANALYSIS_VERSION,'analysis_strategy':ANALYSIS_STRATEGY}
    def plan(self,evidence,brief,brand,options,**kwargs):
        self.calls.append('plan');self.plan_inputs.append(copy.deepcopy((brief,brand,options,kwargs.get('revision'))))
        return {'theme':'走访现场','viewpoint':'观察现场再判断','audience_value':'获得现场细节',
                'title':'现场走访','actual_script':'事实来自现场。','structure':['开头','细节','结尾'],
                'mode':'vlog','scenes':[{'source_index':0,'source_in':0,'source_out':4,'spoken_text':'事实来自现场。'}],
                'content_check':{'passed':True},'plan_sha256':'signed-by-planner'}
    def render(self,plan,paths,**kwargs):
        self.calls.append('render')
        assert plan.get('plan_sha256')
        assert 'version_id' not in plan  # Signed plan remains byte-for-byte immutable.
        folder=kwargs['work_dir'];folder.mkdir(exist_ok=True)
        video=folder/'finished.mp4';video.write_bytes(b'checked media placeholder for queue unit test')
        srt=folder/'finished.srt';srt.write_text('1\n00:00:00,000 --> 00:00:04,000\n事实来自现场。\n',encoding='utf-8')
        return {'output_path':str(video),'srt_path':str(srt),'qc':{'passed':True},'timeline':plan['scenes']}
    def revise(self,parent,instruction,**kwargs):
        self.calls.append('revise')
        child=copy.deepcopy(parent)
        child['actual_script']='修改后仍来自现场。'
        child['scenes'][0]['spoken_text']=child['actual_script']
        child['revision_patch']={'instruction':instruction,'scope':'local_text'}
        child['parent_plan_sha256']=parent['plan_sha256']
        child['plan_sha256']='signed-by-reviser'
        return child
    def quality(self,output,plan,**kwargs):
        self.calls.append('quality');self.quality_inputs.append(copy.deepcopy(plan))
        return {'passed':self.quality_pass,'checks':[{'name':'unit_test_callback','passed':self.quality_pass}],
                'issues':[] if self.quality_pass else [{'code':'scene_mismatch','message':'画面不符合内容','repairable':False}]}
    def deliver(self,record,output,plan,render,quality,folder):
        self.calls.append('delivery')
        result=[]
        for kind in ('video','subtitles','cover','copy','record','package'):
            path=folder/(kind+'.test');path.write_bytes(b'artifact')
            result.append({'kind':kind,'path':str(path),'name':path.name,'size':path.stat().st_size,
                           'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
        return result


@pytest.fixture
def setup(tmp_path):
    inbox=tmp_path/'inbox';inbox.mkdir();(inbox/'real.mp4').write_bytes(b'input')
    pipeline=Pipeline();data=tmp_path/'data'
    service=DirectorService(data,inbox,validate_source=source_validator,
        brand_snapshot=lambda query:{'revision':7,'facts':[{'text':'已确认知识'}]},settings_loader=lambda:{'fps':30},
        preflight=pipeline.preflight,analyzer=pipeline.analyze,planner=pipeline.plan,
        reviser=pipeline.revise,
        renderer=pipeline.render,quality_checker=pipeline.quality,deliverer=pipeline.deliver,
        repairer=lambda *args,**kwargs:None)
    request={'sources':['real.mp4'],'goal':'做一条真实走访视频','mode':'auto','title':'',
             'options':{'aspect':'9:16','subtitles':True,'music':True,'target_duration':45},
             'parent_id':None,'revision':None}
    return service,pipeline,request


def test_one_submit_completes_artifacts_and_versions(setup):
    service,pipeline,request=setup;record=service.create(uuid4(),request)
    service.process(record);result=service.get(record['id']);public=service.public(result)
    assert result['status']=='completed'
    assert pipeline.calls==['preflight','analysis','plan','render','quality','delivery']
    assert len(public['artifacts'])==6 and public['brand_context_revision']==7
    assert public['content_plan']['actual_script']=='事实来自现场。'
    assert pipeline.quality_inputs[0]['plan_sha256']=='signed-by-planner'
    assert pipeline.quality_inputs[0]['render_report']['srt_path'].endswith('.srt')
    assert not any('path' in item for item in public['artifacts'])


def test_brand_selection_is_bounded_before_queue_creation():
    from pydantic import ValidationError
    identifiers=[format(i,'032x') for i in range(5)]
    assert ProductionOptions(brand_screenshot_ids=identifiers[:4]).brand_screenshot_ids==identifiers[:4]
    for invalid in (identifiers,[identifiers[0],identifiers[0]],['../outside.png']):
        with pytest.raises(ValidationError):ProductionOptions(brand_screenshot_ids=invalid)


def test_local_revision_uses_locked_parent_not_new_topic_and_is_idempotent(setup):
    service,pipeline,request=setup
    def narrated_plan(*args,**kwargs):
        return {**pipeline.plan(*args,**kwargs),'mode':'narrated','options':{'mode':'auto','target_duration':45}}
    service.planner=narrated_plan
    parent=service.create(uuid4(),request);service.process(parent);parent=service.get(parent['id'])
    saved=copy.deepcopy(parent);pipeline.calls.clear();seen=[]
    def revise(plan,instruction,**kwargs):
        seen.append((copy.deepcopy(plan),instruction,copy.deepcopy(kwargs['brand_context']),kwargs['options']['mode']))
        # Even a mutating injected worker must not alter the persisted parent.
        result=pipeline.revise(plan,instruction,**kwargs);plan['title']='worker mutation'
        return result
    service.reviser=revise;identifier=uuid4()
    child=service.revise(parent['id'],identifier,'把“现场”改为“走访现场”，其它不动。')
    service.process(child);child=service.get(child['id'])
    assert child['status']=='completed',child['error']
    assert pipeline.calls==['preflight','revise','render','quality','delivery']
    assert child['snapshot']['revision_scope']=='local_text'
    assert seen[0][0]==saved['plan'] and seen[0][2]==saved['snapshot']['brand_context'] and seen[0][3]=='auto'
    assert service.get(parent['id'])==saved
    assert service.revise(parent['id'],identifier,'把“现场”改为“走访现场”，其它不动。')['id']==child['id']
    assert len(service.list())==2


def test_duration_revision_retains_structural_planning_and_analysis_cache(setup):
    service,pipeline,request=setup;parent=service.create(uuid4(),request);service.process(parent)
    pipeline.calls.clear()
    child=service.revise(parent['id'],uuid4(),'缩短到20秒，保留现场原声。');service.process(child)
    result=service.get(child['id'])
    assert result['status']=='completed',result['error']
    assert result['snapshot']['revision_scope']=='structural'
    assert pipeline.calls==['preflight','plan','render','quality','delivery']
    assert pipeline.plan_inputs[-1][2]['mode']=='vlog' and pipeline.plan_inputs[-1][2]['target_duration']==20


def test_required_brand_assets_block_before_any_model_or_render(setup):
    service,pipeline,request=setup;request['options']['brand_usage']='required'
    record=service.create(uuid4(),request);service.process(record);result=service.get(record['id'])
    assert result['status']=='blocked'
    assert result['error']['code']=='brand_media_unavailable'
    assert result['preflight']['blockers'][0]['id']=='brand_assets'
    assert not pipeline.calls and result['request']==request


def test_brand_snapshot_stays_frozen_through_revision_and_detects_tampering(setup,monkeypatch):
    from PIL import Image
    from studio import director_brand
    service,pipeline,request=setup
    def freeze(data,context,folder,options):
        result=copy.deepcopy(context);folder=Path(folder)/'brand-assets';folder.mkdir(parents=True,exist_ok=True)
        path=folder/('a'*32+'.png');Image.new('RGB',(100,120),'green').save(path)
        result['brand_assets']=[{'id':'a'*32,'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'width':100,'height':120}]
        return result
    monkeypatch.setattr(director_brand,'snapshot_brand_assets',freeze)
    parent=service.create(uuid4(),request);service.process(parent);parent=service.get(parent['id'])
    original=copy.deepcopy(parent['snapshot']['brand_context'])
    monkeypatch.setattr(director_brand,'snapshot_brand_assets',lambda *a,**k:pytest.fail('ordinary revision must inherit frozen images'))
    service.brand_snapshot=lambda query:pytest.fail('revision must not query changed live knowledge')
    child=service.revise(parent['id'],uuid4(),'措辞自然一点');service.process(child)
    assert service.get(child['id'])['status']=='completed'
    assert child['snapshot']['brand_context']==original
    asset=Path(original['brand_assets'][0]['path']);Image.new('RGB',(100,120),'red').save(asset)
    with pytest.raises(DirectorServiceError) as error:service.validate_snapshot(service.get(child['id']))
    assert error.value.code=='source_changed'


def test_voice_preparation_callback_keeps_specific_stage_and_chinese_label(setup):
    service,pipeline,request=setup;record=service.create(uuid4(),request)
    observed=[]
    def render(plan,paths,**kwargs):
        kwargs['on_stage']('preparing_voice')
        observed.append(service.public(service.get(record['id'])))
        return pipeline.render(plan,paths,**kwargs)
    service.renderer=render
    service.process(record)
    assert len(observed)==1
    assert observed[0]['stage']=='preparing_voice'
    assert observed[0]['stage_label']=='准备独白与声音'
    assert observed[0]['events'][-1]['message']=='准备独白与声音'
    assert service.get(record['id'])['status']=='completed'


def test_same_request_replays_without_creating_or_processing_again(setup):
    service,pipeline,request=setup;identifier=uuid4();record=service.create(identifier,request)
    service.process(record);before=len(pipeline.calls)
    replay=service.create(identifier,request);service.process(replay)
    assert replay['id']==record['id'] and len(pipeline.calls)==before and len(service.list())==1
    changed=copy.deepcopy(request);changed['goal']='其他目标'
    with pytest.raises(HTTPException) as error:service.create(identifier,changed)
    assert error.value.status_code==409


def test_source_change_blocks_before_model_or_render(setup):
    service,pipeline,request=setup;record=service.create(uuid4(),request)
    (service.inbox/'real.mp4').write_bytes(b'changed source')
    service.process(record);result=service.get(record['id'])
    assert result['status']=='blocked' and result['error']['code']=='source_changed'
    assert not pipeline.calls


def test_all_preflight_blockers_preserved_no_partial_model_work(setup):
    service,pipeline,request=setup
    pipeline.blockers=[{'id':'vision','status':'blocked','detail':'视觉服务缺失'},
                       {'id':'storage','status':'blocked','detail':'存储不足'}]
    record=service.create(uuid4(),request);service.process(record);result=service.get(record['id'])
    assert result['status']=='blocked'
    assert result['error']['details']==pipeline.blockers and pipeline.calls==['preflight']
    assert not result['artifacts']


def test_quality_failure_never_becomes_downloadable(setup):
    service,pipeline,request=setup;pipeline.quality_pass=False
    record=service.create(uuid4(),request);service.process(record);result=service.get(record['id'])
    assert result['status']=='failed' and result['error']['code']=='quality_failed'
    assert 'delivery' not in pipeline.calls and 'render-result' in result['checkpoints']
    assert service.public(result)['artifacts']==[]
    with pytest.raises(HTTPException) as error:service.artifact_path(record['id'],'video')
    assert error.value.status_code==409


def test_recovery_uses_completed_analysis_plan_render_without_regeneration(setup):
    service,pipeline,request=setup;record=service.create(uuid4(),request)
    service.process(record);completed=service.get(record['id'])
    points=completed['checkpoints'];points.pop('quality')
    service.update(record['id'],status='running',stage='quality',artifacts=None,quality=None,
                   checkpoints=points,started_at=None)
    pipeline.calls.clear();service.recover();service.process(service.get(record['id']))
    assert service.get(record['id'])['status']=='completed'
    assert pipeline.calls==['preflight','quality','delivery']


def test_checkpoint_tampering_does_not_get_used(setup):
    service,pipeline,request=setup;record=service.create(uuid4(),request);service.process(record)
    (service.root/record['id']/'analysis.json').write_text('{}')
    service.update(record['id'],status='queued');pipeline.calls.clear();service.process(service.get(record['id']))
    result=service.get(record['id'])
    assert result['status']=='failed' and result['error']['code']=='checkpoint_changed'
    assert pipeline.calls==['preflight']


def test_retry_is_idempotent_and_bounded(setup):
    service,pipeline,request=setup;pipeline.failure='analysis'
    record=service.create(uuid4(),request)
    for _ in range(MAX_ATTEMPTS):service.process(service.get(record['id']))
    result=service.get(record['id'])
    assert result['status']=='failed' and result['attempts']==MAX_ATTEMPTS
    with pytest.raises(HTTPException):service.retry(record['id'],uuid4())
    assert pipeline.calls.count('analysis')==MAX_ATTEMPTS


def test_user_retry_request_does_not_repeat_after_completion(setup):
    service,pipeline,request=setup;pipeline.blockers=[{'id':'voice','status':'blocked'}]
    record=service.create(uuid4(),request);service.process(record);retry_id=uuid4()
    service.retry(record['id'],retry_id);pipeline.blockers=[];service.process(service.get(record['id']))
    count=len(pipeline.calls)
    result=service.retry(record['id'],retry_id)
    assert result['status']=='completed' and len(pipeline.calls)==count


def test_sentence_revision_preserves_original_and_reuses_material_analysis(setup):
    service,pipeline,request=setup;record=service.create(uuid4(),request);service.process(record)
    original=service.get(record['id']);pipeline.calls.clear()
    revised=service.revise(record['id'],uuid4(),'缩短到一分钟，保留更多现场原声，不要音乐')
    service.process(revised);result=service.get(revised['id'])
    assert result['status']=='completed' and result['request']['parent_id']==record['id']
    assert result['request']['options']['target_duration']==60
    assert result['request']['options']['music'] is False and result['request']['mode']=='vlog'
    assert 'analysis' not in pipeline.calls and 'plan' in pipeline.calls and 'render' in pipeline.calls
    assert service.get(record['id'])['plan']==original['plan']
    assert pipeline.plan_inputs[-1][-1]['instruction']=='缩短到一分钟，保留更多现场原声，不要音乐'


def test_artifact_paths_cannot_escape_production_folder(setup,tmp_path):
    service,pipeline,request=setup;record=service.create(uuid4(),request);service.process(record)
    external=tmp_path/'outside.mp4';external.write_bytes(b'outside')
    service.update(record['id'],artifacts=[{'kind':'video','path':str(external),'size':7,'name':'bad.mp4'}])
    with pytest.raises(HTTPException) as error:service.artifact_path(record['id'],'video')
    assert error.value.status_code==409


def test_queue_bound_and_discovery_scope_are_explicit(setup):
    service,pipeline,request=setup
    for i in range(15):(service.inbox/f'raw-{i}.mp4').write_bytes(b'raw')
    sources,scope=service.choose_sources([])
    assert len(sources)==12 and scope['scope_limited'] is True and scope['library_available']==16
    for _ in range(3):service.create(uuid4(),request)
    with pytest.raises(HTTPException) as error:service.create(uuid4(),request)
    assert error.value.status_code==429


def test_routes_auth_validation_replay_and_download(setup):
    original,pipeline,request=setup;app=FastAPI()
    def auth(authorization:str|None=Header(default=None)):
        if authorization!='Bearer test':raise HTTPException(401,'auth')
    service=register_director_routes(app,auth,original.inbox,original.data/'api',source_validator=source_validator,
        preflight=pipeline.preflight,analyzer=pipeline.analyze,planner=pipeline.plan,renderer=pipeline.render,
        quality_checker=pipeline.quality,deliverer=pipeline.deliver,repairer=lambda *a,**k:None)
    client=TestClient(app);headers={'Authorization':'Bearer test'}
    assert client.get('/api/productions').status_code==401
    request.pop('parent_id');request.pop('revision');request['request_id']=str(uuid4())
    submitted=client.post('/api/productions',headers=headers,json=request)
    assert submitted.status_code==200
    identifier=submitted.json()['production']['id'];service.process(service.get(identifier))
    again=client.post('/api/productions',headers=headers,json=request)
    assert again.json()['production']['id']==identifier
    assert client.get(f'/api/productions/{identifier}/video',headers=headers).status_code==200
    assert client.get(f'/api/productions/{identifier}/artifacts/record',headers=headers).status_code==200
    request['request_id']=str(uuid4());request['options']['music']='yes'
    assert client.post('/api/productions',headers=headers,json=request).status_code==422


def test_repair_runs_once_then_quality_rechecks_actual_new_file(setup):
    service,pipeline,request=setup;calls=[]
    def quality(output,plan,**kwargs):
        calls.append(output.name)
        return {'passed':output.name=='repaired.mp4','issues':[] if output.name=='repaired.mp4'
                else [{'code':'audible_sound','repairable':True,'message':'人声过低'}]}
    def repair(output,plan,quality,**kwargs):
        assert kwargs['render_result']['output_path']==str(output)
        assert kwargs['paths']==[service.inbox/'real.mp4']
        assert kwargs['options']['subtitles'] is True
        folder=kwargs['work_dir'];folder.mkdir(exist_ok=True)
        fixed=folder/'repaired.mp4';fixed.write_bytes(b'repaired')
        return {**plan['render_report'],'output_path':str(fixed)}
    service.quality_checker=quality;service.repairer=repair
    record=service.create(uuid4(),request);service.process(record);result=service.get(record['id'])
    assert calls==['finished.mp4','repaired.mp4'] and result['status']=='completed'
    assert result['repair_attempts']==1


def test_failed_repair_never_loops_or_bypasses_quality(setup):
    service,pipeline,request=setup;repairs=[]
    service.quality_checker=lambda *a,**k:{'passed':False,'issues':[{'code':'audible_sound','repairable':True}]}
    def repair(output,plan,quality,**kwargs):
        repairs.append(1)
        return plan['render_report']
    service.repairer=repair
    record=service.create(uuid4(),request);service.process(record)
    assert service.get(record['id'])['status']=='failed' and len(repairs)==1
    service.retry(record['id'],uuid4());service.process(service.get(record['id']))
    assert service.get(record['id'])['status']=='failed' and len(repairs)==1


def test_presentation_only_revision_reuses_plan_without_calling_model(setup):
    service,pipeline,request=setup;record=service.create(uuid4(),request);service.process(record)
    original=service.get(record['id']);pipeline.calls.clear()
    revised=service.revise(record['id'],uuid4(),'去掉音乐，关闭字幕')
    service.process(revised);result=service.get(revised['id'])
    assert result['status']=='completed'
    assert 'analysis' not in pipeline.calls and 'plan' not in pipeline.calls
    assert result['plan']['actual_script']==original['plan']['actual_script']
    assert result['plan']['scenes']==original['plan']['scenes']
    assert result['plan']['options']['music'] is False and result['plan']['options']['subtitles'] is False
    assert result['plan']['plan_sha256']!=original['plan']['plan_sha256']
    assert service.get(record['id'])['plan']==original['plan']


def test_presenter_not_silently_processed_as_material_montage(setup):
    service,pipeline,request=setup;request['mode']='presenter';request['sources']=[]
    record=service.create(uuid4(),request);service.process(record);result=service.get(record['id'])
    assert result['status']=='blocked' and pipeline.calls==['preflight']
    assert any(item['id']=='production_mode' for item in result['preflight']['blockers'])


def test_same_size_output_tamper_is_rejected(setup):
    service,pipeline,request=setup;record=service.create(uuid4(),request);service.process(record)
    result=service.get(record['id']);path=Path(result['artifacts'][0]['path'])
    path.write_bytes(b'changed!')
    with pytest.raises(HTTPException) as error:service.artifact_path(record['id'],'video')
    assert error.value.status_code==409


def test_insufficient_material_preserves_analysis_and_reports_concrete_gap(setup):
    service,pipeline,request=setup
    def insufficient(*args,**kwargs):
        raise DirectorServiceError('material_shortfall','这些素材不足以说明活动已经结束。',
                                   [{'scene':2,'missing':'活动结束或总结的真实画面和原声'}])
    service.planner=insufficient
    record=service.create(uuid4(),request);service.process(record);result=service.get(record['id'])
    assert result['status']=='blocked' and 'analysis' in result['checkpoints']
    assert result['error']['details'][0]['missing']=='活动结束或总结的真实画面和原声'
    assert 'render' not in pipeline.calls and not result['artifacts']


def test_total_time_budget_prevents_more_expensive_work_or_retry(setup):
    service,pipeline,request=setup;record=service.create(uuid4(),request)
    service.update(record['id'],elapsed_seconds=MAX_SECONDS)
    service.process(service.get(record['id']));result=service.get(record['id'])
    assert result['status']=='failed' and result['error']['code']=='deadline_exceeded'
    assert not pipeline.calls and service.public(result)['can_retry'] is False
    with pytest.raises(HTTPException) as error:service.retry(record['id'],uuid4())
    assert error.value.status_code==409


def test_revision_submission_is_idempotent_and_different_instruction_conflicts(setup):
    service,pipeline,request=setup;record=service.create(uuid4(),request);service.process(record)
    identifier=uuid4();first=service.revise(record['id'],identifier,'开头更直接')
    second=service.revise(record['id'],identifier,'开头更直接')
    assert first['id']==second['id'] and len(service.list())==2
    with pytest.raises(HTTPException) as error:service.revise(record['id'],identifier,'缩短到30秒')
    assert error.value.status_code==409


def test_new_revision_rejects_changed_originals_but_replay_is_stable(setup):
    service,pipeline,request=setup;record=service.create(uuid4(),request);service.process(record)
    identifier=uuid4();child=service.revise(record['id'],identifier,'开头更直接')
    (service.inbox/'real.mp4').write_bytes(b'changed originals')
    assert service.revise(record['id'],identifier,'开头更直接')['id']==child['id']
    with pytest.raises(HTTPException) as error:service.revise(record['id'],uuid4(),'开头更直接')
    assert error.value.status_code==409


def test_restart_rechecks_dependencies_and_keeps_completed_checkpoints(setup):
    service,pipeline,request=setup;record=service.create(uuid4(),request);service.process(record)
    before=copy.deepcopy(service.get(record['id'])['checkpoints'])
    service.update(record['id'],status='running',stage='quality',started_at=None)
    pipeline.calls.clear();pipeline.blockers=[{'id':'vision','status':'blocked','detail':'服务暂不可用'}]
    service.recover();service.process(service.get(record['id']));result=service.get(record['id'])
    assert result['status']=='blocked' and result['checkpoints']==before
    assert pipeline.calls==['preflight']
    pipeline.blockers=[];service.retry(record['id'],uuid4());service.process(service.get(record['id']))
    assert service.get(record['id'])['status']=='completed'
    assert pipeline.calls==['preflight','preflight','delivery']


def test_new_routes_require_auth_and_reject_unsafe_source_paths(setup):
    original,pipeline,request=setup;app=FastAPI()
    def auth(authorization:str|None=Header(default=None)):
        if authorization!='Bearer test':raise HTTPException(401,'auth')
    service=register_director_routes(app,auth,original.inbox,original.data/'authapi',source_validator=source_validator,
        preflight=pipeline.preflight,analyzer=pipeline.analyze,planner=pipeline.plan,renderer=pipeline.render,
        quality_checker=pipeline.quality,deliverer=pipeline.deliver,repairer=lambda *a,**k:None)
    client=TestClient(app);identifier=str(uuid4())
    get_paths=['/api/productions','/api/productions/preflight',f'/api/productions/{identifier}',
               f'/api/productions/{identifier}/video',f'/api/productions/{identifier}/artifacts/package']
    for path in get_paths:assert client.get(path).status_code==401
    for path,payload in [(f'/api/productions/{identifier}/retry',{'request_id':identifier}),
                         (f'/api/productions/{identifier}/revise',{'request_id':identifier,'instruction':'修改开头'}),
                         ('/api/productions',{'request_id':identifier,'sources':['real.mp4']})]:
        assert client.post(path,json=payload).status_code==401
    payload={'request_id':identifier,'sources':['../outside.mp4'],'goal':'现场记录'}
    result=client.post('/api/productions',json=payload,headers={'Authorization':'Bearer test'})
    assert result.status_code==400 and not service.list()


def test_restart_does_not_charge_offline_hours_or_clear_prior_usage(setup):
    service,pipeline,request=setup;record=service.create(uuid4(),request)
    service.update(record['id'],status='running',attempts=2,elapsed_seconds=123.5,
                   started_at=time.time()-86400)
    service.recover();recovered=service.get(record['id'])
    assert recovered['status']=='queued' and recovered['started_at'] is None
    assert recovered['elapsed_seconds']==123.5 and recovered['attempts']==2
    pipeline.blockers=[{'id':'asr','status':'blocked','detail':'服务暂时不可用'}]
    service.process(recovered);result=service.get(record['id'])
    assert result['status']=='blocked' and result['attempts']==3
    assert 123.5 <= result['elapsed_seconds'] < 130
    assert result['error']['code']=='preflight_failed'


def test_running_work_persists_usage_before_a_stage_returns(setup,monkeypatch):
    import studio.director_service as module
    service,pipeline,request=setup;record=service.create(uuid4(),request)
    service.update(record['id'],elapsed_seconds=73)
    monkeypatch.setattr(module,'USAGE_HEARTBEAT_SECONDS',.02)
    entered=threading.Event();release=threading.Event()
    def analyzing(paths,**kwargs):
        entered.set()
        assert release.wait(3)
        return pipeline.analyze(paths,**kwargs)
    service.analyzer=analyzing
    worker=threading.Thread(target=service.process,args=(record,))
    worker.start()
    try:
        assert entered.wait(2)
        deadline=time.monotonic()+2
        while time.monotonic()<deadline:
            active=service.get(record['id'])
            if active['elapsed_seconds']>73.02:break
            release.wait(.01)
        assert active['status']=='running' and active['elapsed_seconds']>73.02
        assert active['stage']=='understanding'
    finally:
        release.set();worker.join(3)
    assert not worker.is_alive()
    result=service.get(record['id'])
    assert result['status']=='completed' and result['elapsed_seconds']>=active['elapsed_seconds']


def test_crash_recovery_cannot_bypass_attempt_limit(setup):
    service,pipeline,request=setup;record=service.create(uuid4(),request)
    service.update(record['id'],status='running',attempts=MAX_ATTEMPTS,
                   elapsed_seconds=100,started_at=time.time()-86400)
    service.recover();service.process(service.get(record['id']));result=service.get(record['id'])
    assert result['status']=='failed' and result['attempts']==MAX_ATTEMPTS
    assert result['elapsed_seconds']==100 and not pipeline.calls
    assert result['error']['code']=='attempts_exhausted'
    assert service.public(result)['can_retry'] is False


def test_old_analysis_is_archived_and_downstream_checkpoints_rebuilt(setup):
    service,pipeline,request=setup;record=service.create(uuid4(),request);service.process(record)
    original=service.get(record['id'])
    old_analysis=service.load_checkpoint(original,'analysis');old_analysis.pop('analysis_version')
    service.checkpoint(record['id'],'analysis',old_analysis)
    service.update(record['id'],status='queued',stage='planning')
    pipeline.calls.clear();service.process(service.get(record['id']));updated=service.get(record['id'])
    assert updated['status']=='completed'
    assert pipeline.calls==['preflight','analysis','plan','render','quality','delivery']
    history=list((service.root/record['id']/'history').glob('analysis-*'))
    assert len(history)==1
    assert json.loads((history[0]/'analysis.json').read_text(encoding='utf-8'))==old_analysis
    assert json.loads((history[0]/'content-plan.json').read_text(encoding='utf-8'))==original['plan']
    assert any(history[0].glob('output_path-*'))
    assert service.analysis_current(service.load_checkpoint(updated,'analysis'))


def test_analysis_upgrade_never_mutates_completed_parent(setup):
    service,pipeline,request=setup;record=service.create(uuid4(),request);service.process(record)
    old_analysis=service.load_checkpoint(service.get(record['id']),'analysis');old_analysis['analysis_version']=0
    service.checkpoint(record['id'],'analysis',old_analysis)
    original=service.get(record['id']);pipeline.calls.clear()
    service.process(original)
    assert not pipeline.calls and service.get(record['id'])==original
    child=service.revise(record['id'],uuid4(),'关闭字幕')
    service.process(child);updated=service.get(child['id'])
    assert updated['status']=='completed'
    assert pipeline.calls==['preflight','analysis','plan','render','quality','delivery']
    assert service.get(record['id'])==original
    assert not (service.root/record['id']/'history').exists()
