import copy
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from studio import director_media as dm


def evidence():
    value = {'version': 1, 'sources': [], 'visuals': [], 'speech': [], 'duplicates': []}
    for i, (description, text) in enumerate([
        ('室外入口，一人步行进入道路，树木和建筑。', '今天先从入口走进去，看看这条路是否好走。'),
        ('同一条道路的路面和路边植物，可见通行空间。', '这段路面比较平整，旁边还有树木遮阴。'),
        ('步行的人到达建筑入口，在门前停下。', '走到入口就到了，这一路需要注意的是脚下的台阶。')]):
        start = i*15.
        value['visuals'].append({'id': f'V0-{i}', 'source_index': 0, 'start':start,'end':start+14,
            'description':description,'scene':'室外','objects':['树木','道路'],'actions':['步行'],
            'visible_text':'','usable':True,'continuity':True,'problems':[]})
        value['speech'].append({'id':f'S0-{i}','source_index':0,'start':start+1,'end':start+8,
            'order':i,'text':text,'verified':False,'context_before':'','context_after':''})
    value['sha256'] = dm.digest(value)
    return value


def proposal(mode='vlog'):
    return {'status':'ready','mode':mode,'gaps':[],
        'theme':'从入口走到目的地，记录道路的实际情况',
        'viewpoint':'实地步行能发现入口和沿路需要注意的细节',
        'audience_value':'了解这段真实路线的路面与入口情况',
        'audience':'准备步行到达的人','purpose':'真实过程记录','style':'自然原声',
        'title':'从入口走进去','posting_copy':'跟着真实步行过程，看看沿路与入口的情况。',
        'candidates':[{'theme':'记录步行路线','support':5,'value':3,'clarity':5,'feasibility':5,
                       'reason':'连续原声和同一路线画面支撑','evidence_ids':['V0-0','V0-1','V0-2']}],
        'structure':{'opening':'为什么从入口开始走','development':'道路细节','ending':'到达入口并提醒台阶'},
        'scenes':[{'role':role,'purpose':purpose,'visual_id':f'V0-{i}','speech_ids':[f'S0-{i}'],
                   'narration':['沿着真实道路走一遍，可以观察入口前的通行条件。','沿路有树木，路面的情况值得留意，步行时看清脚下。','走到建筑入口，再看清台阶，就完成这一段步行观察。'][i],
                   'evidence_ids':[f'V0-{i}',f'S0-{i}'],'relation':'时间推进'}
                   for i,(role,purpose) in enumerate([('opening','提出观察问题'),('development','展示沿途细节'),('ending','到达并收束')])]}


def accepted_audit():
    return {'passed':True,'checks':{k:True for k in ('grounded','coherent','original_meaning','visual_alignment','title_honest','viewpoint_delivered','revision_satisfied')},
            'issues':[],'summary':'逐一核对原声完整句、画面区间与主题，未发现编造。','viewpoint_evidence':[]}


def selected_proposal():
    p=proposal()
    return {'status':'ready','mode':'vlog','chosen':0,
            'candidates':[{**p['candidates'][0],'ids':['T0-0-0','T0-1-1','T0-2-2']}]}


def written_proposal():
    p=proposal()
    p['scenes']=[{**s,'id':f'T0-{i}-{i}'} for i,s in enumerate(p['scenes'])]
    return p


def test_vlog_script_is_exact_original_quotes():
    ev = evidence(); raw = proposal(); raw['scenes'][0]['narration'] = '这段文字不能替换原声'
    plan = dm.validate_plan(raw, ev, {'revision':3}, {'mode':'vlog'})
    assert plan['actual_script'] == '\n'.join(s['text'] for s in ev['speech'])
    assert plan['scenes'][0]['source_in'] == pytest.approx(.88)
    assert plan['scenes'][0]['source_out'] == pytest.approx(8.2)


@pytest.mark.parametrize('mutation,code',[
    (lambda p: p['scenes'][1].update(visual_id='V0-0'), 'invalid_plan'),
    (lambda p: p['scenes'][1].update(speech_ids=['S0-0']), 'invalid_plan'),
    (lambda p: p['scenes'][1].update(evidence_ids=['F999']), 'invalid_plan'),
    (lambda p: p.update(structure={}), 'invalid_plan'),
    (lambda p: p['scenes'][0].update(role='development'), 'invalid_plan'),
])
def test_invalid_ranges_and_missing_evidence_block(mutation, code):
    raw=proposal(); mutation(raw)
    with pytest.raises(dm.DirectorMediaError) as found:
        dm.validate_plan(raw,evidence(),{}, {'mode':'vlog'})
    assert found.value.code == code


def test_quotes_cannot_skip_negation_or_reverse_same_source():
    ev=evidence(); ev['visuals'][0]['end']=45
    raw=proposal(); raw['scenes'][0]['speech_ids']=['S0-0','S0-2']
    with pytest.raises(dm.DirectorMediaError, match='跳过句子'):
        dm.validate_plan(raw,ev,{}, {'mode':'vlog'})
    raw=proposal(); raw['scenes'][0]['visual_id']='V0-2';raw['scenes'][0]['speech_ids']=['S0-2'];raw['scenes'][0]['evidence_ids']=['V0-2']
    raw['scenes'][2]['visual_id']='V0-0';raw['scenes'][2]['speech_ids']=['S0-0'];raw['scenes'][2]['evidence_ids']=['V0-0']
    with pytest.raises(dm.DirectorMediaError, match='倒置'):
        dm.validate_plan(raw,ev,{}, {'mode':'vlog'})


def test_narrated_claims_cannot_add_numbers():
    raw=proposal('narrated');raw['scenes'][0]['narration']='我们已经服务了100位客户，这次服务获得一致好评。'
    with pytest.raises(dm.DirectorMediaError) as found:
        dm.validate_plan(raw,evidence(),{}, {'mode':'story'})
    assert found.value.code == 'unsupported_claim'


@pytest.mark.parametrize('field,text',[
    ('title','从静安寺到青浦的收车日常'),
    ('viewpoint','独自完成跨区收车，体现灵活的工作方式。')])
def test_mention_does_not_prove_route_direction_or_completed_business(field,text):
    ev=evidence(); ev['speech'][0]['text']='我们公司在青浦，今天准备去客户那里收车。'
    ev['speech'][1]['text']='刚刚已经到了静安寺这边的路口。'
    ev['speech'][2]['text']='到客户小区门口了，等下进去聊托管。'
    raw=proposal();raw[field]=text
    with pytest.raises(dm.DirectorMediaError) as found:
        dm.validate_plan(raw,ev,{}, {})
    assert found.value.code=='unsupported_claim'


def test_generic_place_roles_are_not_mistaken_for_unverified_named_routes():
    raw=proposal();raw['title']='从公司到客户小区的真实记录'
    dm.validate_plan(raw,evidence(),{}, {})


def test_uncertain_codes_and_background_announcements_are_flagged_in_chinese_text():
    assert dm._speech_caution('今天收的是1S6')
    assert dm._speech_caution('乘客人列车马上就要进站了')
    assert not dm._speech_caution('今天同事比较忙，我自己出发。')


def test_audit_schema_retry_keeps_real_content_failure_and_logs_raw(tmp_path):
    plan=dm.validate_plan(proposal(),evidence(),{}, {})
    bad=accepted_audit();bad.update(passed=False,issues=[{'scene':'opening','reason':'缺少时间顺序依据'}])
    corrected=copy.deepcopy(bad);corrected['issues'][0]['scene']='1'
    replies=iter([bad,corrected]);calls=[]
    def provider(messages,tokens):
        calls.append(messages)
        return json.dumps(next(replies),ensure_ascii=False)
    result=dm.audit_content(plan,data=None,provider=provider,work_dir=tmp_path)
    assert result['passed'] is False
    assert result['issues']==[{'scene':1,'reason':'缺少时间顺序依据'}]
    assert len(calls)==2 and (tmp_path/'audit-0.response.txt').exists()


def test_requested_original_audio_cannot_silently_become_tts():
    with pytest.raises(dm.DirectorMediaError,match='连贯原声'):
        dm.validate_plan(proposal('narrated'),evidence(),{}, {'mode':'vlog'})


def test_requested_narration_cannot_silently_become_original_audio():
    with pytest.raises(dm.DirectorMediaError,match='旁白制作'):
        dm.validate_plan(proposal(),evidence(),{}, {'mode':'narrated'})


def test_only_long_inter_utterance_gaps_are_removed():
    scene={'source_in':0.,'source_out':15.,'speech':[
        {'start':.5,'end':2.,'text':'完整一句。'},
        {'start':2.8,'end':4.5,'text':'保留自然停顿。'},
        {'start':9.,'end':12.,'text':'下个完整句子。'}]}
    kept=dm._original_ranges(scene,[(4.6,8.9)])
    assert len(kept)==2
    assert kept[0]['start']==pytest.approx(.32)
    assert kept[0]['end']==pytest.approx(4.75)
    assert kept[1]['start']==pytest.approx(8.82)
    assert [q['text'] for r in kept for q in r['speech']]==[q['text'] for q in scene['speech']]
    assert len(dm._original_ranges(scene,[]))==1


def test_insufficient_material_is_an_actionable_error():
    with pytest.raises(dm.DirectorMediaError) as found:
        dm.validate_plan({'status':'insufficient','gaps':['缺少任何车辆和托管业务画面。']},evidence(),{}, {})
    assert found.value.code=='material_shortfall'
    assert found.value.details==['缺少任何车辆和托管业务画面。']


def test_vision_must_return_every_real_sample_and_cannot_mark_discontinuous_usable():
    row={'id':'V0-0','description':'道路上的行人','scene':'室外','objects':['道路'],
         'actions':['行走'],'visible_text':'','usable':True,'continuity':False,'problems':['中间换场景']}
    assert dm._validate_vision({'windows':[row]},['V0-0'])[0]['usable'] is False
    with pytest.raises(dm.DirectorMediaError):
        dm._validate_vision({'windows':[row]},['V0-0','V0-1'])


def test_content_audit_is_required_and_attempts_bounded(tmp_path):
    calls=[]
    def provider(messages,tokens):
        calls.append(tokens)
        if messages[0]['content'] == dm.AUDIT_PROMPT:
            bad=accepted_audit();bad['passed']=False;bad['issues']=[{'scene':1,'reason':'开头的问题未在结尾回应'}]
            return json.dumps(bad,ensure_ascii=False)
        return json.dumps(selected_proposal() if '你只负责选题' in messages[0]['content'] else written_proposal(),ensure_ascii=False)
    with patch.object(dm,'_provider',return_value=provider):
        with pytest.raises(dm.DirectorMediaError) as found:
            dm.plan_content(evidence(),'真实步行Vlog',{}, {'mode':'vlog'},data=tmp_path,work_dir=tmp_path/'plan')
    assert found.value.code=='content_check_failed'
    assert len(calls)==5
    assert (tmp_path/'plan/content-issues.json').exists()


def test_plan_freezes_content_and_modified_plan_cannot_render(tmp_path):
    def provider(messages,tokens):
        return json.dumps(accepted_audit() if messages[0]['content'] == dm.AUDIT_PROMPT else selected_proposal() if '你只负责选题' in messages[0]['content'] else written_proposal(),ensure_ascii=False)
    with patch.object(dm,'_provider',return_value=provider):
        plan=dm.plan_content(evidence(),'真实步行Vlog',{'revision':3},{'mode':'vlog'},data=tmp_path,work_dir=tmp_path/'plan')
    assert plan['content_check']['passed'] and plan['content_review']['passed']
    assert plan['planning_attempts']==1
    original=copy.deepcopy(plan)
    plan['actual_script']='篡改后的新稿'
    with pytest.raises(dm.DirectorMediaError) as found:
        dm.render_content(plan,[],data=tmp_path,work_dir=tmp_path/'render',settings={})
    assert found.value.code=='plan_changed'
    assert json.loads((tmp_path/'plan/content-plan.json').read_text(encoding='utf-8'))==original


def test_audit_false_check_cannot_claim_pass():
    plan=dm.validate_plan(proposal(),evidence(),{}, {})
    audit=accepted_audit();audit['checks']['visual_alignment']=False
    with pytest.raises(dm.DirectorMediaError) as error:
        dm.audit_content(plan,data=None,provider=lambda *a:json.dumps(audit,ensure_ascii=False))
    assert error.value.code=='audit_inconsistent'


def test_brand_implementation_details_not_promoted_to_claims():
    result=dm._brand_facts({'confirmed_facts':[{'text':'小程序的前端实现车辆申请表。'},
        {'text':'提交资料包括行驶证。'},{'text':'尚待确认的收益承诺','status':'needs_review'}]})
    assert result==[{'id':'F1','text':'提交资料包括行驶证。','category':''}]


def test_executable_catalog_only_exposes_complete_contiguous_original_ranges():
    ev=evidence();catalog=dm._executable_catalog(ev,30)
    speech={s['id']:s for s in ev['speech']}
    assert len(catalog['takes'])==3
    for take in catalog['takes']:
        rows=[speech[key] for key in take['speech_ids']]
        assert [r['order'] for r in rows]==list(range(rows[0]['order'],rows[0]['order']+len(rows)))
        assert take['text']==''.join(r['text'] for r in rows)
        assert take['seconds']==pytest.approx(take['end']-take['start'],abs=.02)
    ev['speech'][1]['text']='本次列车终点站请乘客先下后上'
    assert all('S0-1' not in t['speech_ids'] for t in dm._executable_catalog(ev,30)['takes'])


def test_selection_rejects_all_execution_issues_before_writing():
    catalog=dm._executable_catalog(evidence(),30)
    raw=selected_proposal();raw['candidates'][0]['ids']=['T0-0-0']*5
    with pytest.raises(dm.DirectorMediaError) as found:
        dm._select_plan(raw,catalog,{'mode':'vlog','target_duration':30})
    assert found.value.details[0]['issues']
    raw=selected_proposal();raw['candidates'][0]['ids']=['T0-2-2','T0-0-0']
    with pytest.raises(dm.DirectorMediaError) as found:
        dm._select_plan(raw,catalog,{'mode':'vlog','target_duration':10})
    assert len(found.value.details[0]['issues'])==2


def test_writer_cannot_change_the_locked_source_selection():
    catalog=dm._executable_catalog(evidence(),30)
    selected=dm._select_plan(selected_proposal(),catalog,{'mode':'vlog','target_duration':30})
    written=written_proposal();written['scenes'][0]['speech_ids']=['invented']
    expanded=dm._expand_written(written,selected)
    assert expanded['scenes'][0]['speech_ids']==['S0-0']
    written['scenes'].reverse()
    expanded=dm._expand_written(written,selected)
    assert [s['speech_ids'] for s in expanded['scenes']]==[['S0-0'],['S0-1'],['S0-2']]
    written['scenes'][0]['id']='invented'
    with pytest.raises(dm.DirectorMediaError,match='已锁定片段'):
        dm._expand_written(written,selected)


def test_vlog_writer_cannot_inject_new_narration_or_reorder_keyed_notes():
    selected=dm._select_plan(selected_proposal(),dm._executable_catalog(evidence(),30),{'mode':'vlog','target_duration':30})
    written=proposal();written.pop('scenes')
    written.update(actual_script='伪造台词',narration='假的配音',
        scene_notes={f'T0-{i}-{i}':{'purpose':'具体作用','relation':'时间推进','narration':'今天收的是1S6','source_in':999}
                     for i in (2,1,0)})
    expanded=dm._expand_written(written,selected)
    assert 'actual_script' not in expanded and 'narration' not in expanded
    assert all(s['narration']=='' for s in expanded['scenes'])
    plan=dm.validate_plan(expanded,evidence(),{}, {'mode':'vlog'})
    assert plan['actual_script']=='\n'.join(s['text'] for s in evidence()['speech'])
    assert [s['source_in'] for s in plan['scenes']]==pytest.approx([.88,15.88,30.88])


def test_visual_windows_preserve_crossing_utterance():
    analysis={'coverage':35,'segments':[{'start':12,'end':17,'text':'完整句子'}],
              'samples':[{'time':i,'score':.8,'usable':True} for i in range(36)]}
    windows=dm._windows(analysis)
    assert windows[0][1]==pytest.approx(17.15)
    assert all(not (a<12<b<17) for a,b in windows)


def test_real_original_audio_render_has_exact_cues_and_preserves_source(tmp_path):
    from studio import media
    import shutil
    if not shutil.which('ffmpeg'):
        pytest.skip('FFmpeg unavailable')
    paths=[]
    for i,color in enumerate(('red','blue','green')):
        path=tmp_path/f'source-{i}.mp4'
        media._run([media._tool('ffmpeg'),'-v','error','-y','-f','lavfi','-i',f'color=c={color}:s=320x240:r=30:d=3',
                    '-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=3','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac','-shortest',str(path)])
        paths.append(path)
    ev=evidence(); raw=proposal()
    for i in range(3):
        ev['visuals'][i].update(source_index=i,start=0.,end=3.)
        ev['speech'][i].update(source_index=i,start=.3,end=2.5,order=0)
        info=media.inspect_media(paths[i]);ev['sources'].append({'index':i,'fingerprint':dm.assembly._fingerprint(paths[i]),'info':info})
    ev['sha256']=dm.digest({k:v for k,v in ev.items() if k!='sha256'})
    plan=dm.validate_plan(raw,ev,{}, {'mode':'vlog','aspect':'original','music':False,'subtitles':True})
    plan['content_check']=accepted_audit();plan['content_review']=plan['content_check'];plan['plan_sha256']=dm.digest(plan)
    fingerprints=[dm.assembly._fingerprint(p) for p in paths]
    result=dm.render_content(plan,paths,data=tmp_path,work_dir=tmp_path/'render',settings={'encoder':'cpu','fps':30,'crf':22})
    assert Path(result['output_path']).is_file()
    assert Path(result['srt_path']).is_file()
    assert result['qc']['passed']
    assert [dm.assembly._fingerprint(p) for p in paths]==fingerprints
    assert result['duration']==pytest.approx(sum(s['timeline_end']-s['timeline_start'] for s in result['timeline']),abs=.1)
    assert len(result['timeline'])==3


def test_real_long_gap_removal_remaps_subtitles_without_deleting_words(tmp_path):
    from studio import media
    import shutil
    if not shutil.which('ffmpeg'):
        pytest.skip('FFmpeg unavailable')
    path=tmp_path/'source.mp4'
    media._run([media._tool('ffmpeg'),'-v','error','-y','-f','lavfi','-i','testsrc2=s=320x240:r=30:d=10',
        '-f','lavfi','-i',r"aevalsrc=if(between(t\,2.1\,6.9)\,0\,0.2*sin(2*PI*440*t)):s=48000:d=10",
        '-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac','-shortest',str(path)])
    scene={'source_index':0,'source_in':.1,'source_out':9.,'speech':[
        {'start':.3,'end':2.,'text':'第一句完整原话。'},
        {'start':7.,'end':8.5,'text':'第二句也完整保留。'}]}
    plan={'scenes':[scene],'options':{'aspect':'original'},'source_evidence':{'sources':[{'index':0,'info':media.inspect_media(path)}]}}
    work=tmp_path/'trim';work.mkdir()
    clips,cues,timeline=dm._trim_original([path],plan,work,{'encoder':'cpu','fps':30},None)
    assert media.inspect_media(clips[0])['duration']<4.2
    assert [c['text'] for c in cues]==['第一句完整原话。','第二句也完整保留。']
    assert cues[1]['start']<2.5
    assert timeline[0]['removed_pause_seconds']>4
    assert len(timeline[0]['retained_ranges'])==2


def test_real_narrated_render_uses_one_voice_and_exact_nonrepeating_ranges(tmp_path):
    from studio import media, narration
    import hashlib
    import math
    import shutil
    import struct
    import wave
    if not shutil.which('ffmpeg'):
        pytest.skip('FFmpeg unavailable')
    paths=[];ev=evidence()
    for i in range(3):
        path=tmp_path/f'narrated-source-{i}.mp4'
        media._run([media._tool('ffmpeg'),'-v','error','-y','-f','lavfi','-i','testsrc2=s=320x240:r=30:d=10',
                    '-an','-c:v','libx264','-pix_fmt','yuv420p',str(path)])
        paths.append(path)
        ev['visuals'][i].update(source_index=i,start=0.,end=10.)
        ev['speech'][i].update(source_index=i,start=.3,end=8.,order=0)
        ev['sources'].append({'index':i,'fingerprint':dm.assembly._fingerprint(path),'info':media.inspect_media(path)})
    ev['sha256']=dm.digest({k:v for k,v in ev.items() if k!='sha256'})
    plan=dm.validate_plan(proposal('narrated'),ev,{}, {'mode':'narrated','aspect':'original','music':False,'subtitles':False})
    plan['content_check']=accepted_audit();plan['content_review']=plan['content_check'];plan['plan_sha256']=dm.digest(plan)
    def voice(text,voice_id,output,**kwargs):
        rate=24000;duration=6.017
        with wave.open(str(output),'wb') as handle:
            handle.setnchannels(1);handle.setsampwidth(2);handle.setframerate(rate)
            handle.writeframes(b''.join(struct.pack('<h',int(8000*math.sin(2*math.pi*440*n/rate))) for n in range(round(duration*rate))))
        measured=narration.inspect_audio(output)
        sentences=narration.split_sentences(text)
        segments=[{'start':2*i,'end':2*i+1.9 if i<2 else measured['duration'],'text':sentence} for i,sentence in enumerate(sentences)]
        srt=output.with_suffix('.srt');srt.write_text(narration.make_srt(segments),encoding='utf-8')
        return {**measured,'segments':segments,'output_path':str(output),'srt_path':str(srt),
                'sha256':narration._sha(output),'srt_sha256':narration._sha(srt),
                'script_sha256':hashlib.sha256(text.strip().encode()).hexdigest(),'voice_id':voice_id,'style':kwargs['style'],'warnings':[]}
    with patch.object(narration,'synthesize',side_effect=voice) as synthesize:
        result=dm.render_content(plan,paths,data=tmp_path,work_dir=tmp_path/'narrated',settings={'encoder':'cpu','fps':30,'width':320,'height':240})
    synthesize.assert_called_once()
    assert result['qc']['passed']
    assert Path(result['srt_path']).is_file()
    assert dm.assembly._count_frames(Path(result['output_path']))==181
    assert all(s['source_out']<10 for s in result['timeline'])
    revised=copy.deepcopy(plan);revised['options']['music']=True
    revised['plan_sha256']=dm.digest({k:v for k,v in revised.items() if k!='plan_sha256'})
    with patch.object(narration,'synthesize',side_effect=AssertionError('must reuse validated voice')) as synthesize:
        second=dm.render_content(revised,paths,data=tmp_path,work_dir=tmp_path/'revised',
            settings={'encoder':'cpu','fps':30,'width':320,'height':240},prepared_narration=result['render_plan']['narration'])
    synthesize.assert_not_called()
    assert second['narration_reused'] is True
    assert second['qc']['passed']
    assert second['script_sha256']==result['script_sha256']
    bad=copy.deepcopy(result['render_plan']['narration']);bad['script_sha256']='wrong'
    with pytest.raises(dm.DirectorMediaError) as found:
        dm._checked_voice(bad,plan['actual_script'])
    assert found.value.code=='narration_changed'


def test_visual_recheck_uses_new_actual_frames_and_preserves_old_rejection(tmp_path):
    window={'id':'V0-0','source_index':0,'start':0.,'end':18.93,'sample_times':[4.73,14.2]}
    rejected={'id':'V0-0','description':'建筑与室外广场','scene':'室外','objects':['建筑'],
              'actions':[],'visible_text':'','usable':False,'continuity':False,'problems':['镜头转向另一环境']}
    source={'fingerprint':'source-a','segments':[{'start':0.,'end':4.8},{'start':4.8,'end':10.4},
            {'start':10.4,'end':13.78},{'start':13.78,'end':18.78}],'info':{}}
    calls=[]; frame_times=[]
    def provider(messages,tokens):
        calls.append(messages)
        return json.dumps({'windows':[{**rejected,'id':f'V0-0-{i}','continuity':True,'usable':True,
                           'description':'真实复核的建筑入口' if i==0 else '真实复核的广场座椅','problems':[]} for i in range(2)]})
    def image(path,timestamp,*args):
        frame_times.append(timestamp)
        return 'data:image/jpeg;base64,actualtest'
    with patch.object(dm,'_jpeg',side_effect=image),patch.object(dm.assembly,'_window_quality',return_value=.8):
        windows,observations,records=dm._recheck_discontinuous(Path('source.mp4'),[window],[rejected],source,tmp_path,tmp_path/'frames',provider)
        assert len(calls)==1 and len(frame_times)==4
        assert [w['end'] for w in windows]==[10.4,18.93]
        assert all(o['usable'] for o in observations)
        assert records[0]['original_observation']['usable'] is False
        assert records[0]['reused_verified_cache'] is False
        again=dm._recheck_discontinuous(Path('source.mp4'),[window],[rejected],source,tmp_path,tmp_path/'frames',provider)
        assert len(calls)==1 and len(frame_times)==4
        assert again[2][0]['reused_verified_cache'] is True
        source['fingerprint']='source-b'
        dm._recheck_discontinuous(Path('source.mp4'),[window],[rejected],source,tmp_path,tmp_path/'frames',provider)
        assert len(calls)==2 and len(frame_times)==8


def test_visual_recheck_budget_is_one_split_no_recursive_acceptance(tmp_path):
    windows=[{'id':f'V0-{i}','source_index':0,'start':i*10.,'end':i*10.+10.,
              'sample_times':[i*10.+2.5,i*10.+7.5]} for i in range(4)]
    observations=[{'id':w['id'],'usable':False,'continuity':False,'description':'场景不连续',
                   'scene':'室外','objects':[],'actions':[],'visible_text':'','problems':['不连续']} for w in windows]
    source={'fingerprint':'a','segments':[],'info':{}}
    def observe(path,children,*args):
        return [{**observations[0],'id':w['id']} for w in children]
    with patch.object(dm,'_observe_windows',side_effect=observe) as check,patch.object(dm.assembly,'_window_quality',return_value=.8):
        final,results,records=dm._recheck_discontinuous(Path('source.mp4'),windows,observations,source,tmp_path,tmp_path,None)
    assert check.call_count==4 and len(final)==8 and len(records)==4
    assert not any(o['usable'] for o in results)
    with pytest.raises(dm.DirectorMediaError,match='上限'):
        dm._recheck_discontinuous(Path('source.mp4'),windows+[windows[0]],observations+[observations[0]],source,tmp_path,tmp_path,None)


def test_visual_recheck_does_not_retry_blur_or_too_short_ranges(tmp_path):
    windows=[{'id':'V0-0','source_index':0,'start':0.,'end':3.9,'sample_times':[1.,3.]},
             {'id':'V0-1','source_index':0,'start':4.,'end':18.,'sample_times':[7.,14.]}]
    observations=[{'id':'V0-0','usable':False,'continuity':False},
                  {'id':'V0-1','usable':False,'continuity':True}]
    with patch.object(dm,'_observe_windows') as observe:
        result=dm._recheck_discontinuous(Path('source.mp4'),windows,observations,{'segments':[]},tmp_path,tmp_path,None)
    observe.assert_not_called()
    assert result==(windows,observations,[])


@pytest.mark.parametrize('title',['一人一车收车实录','跨城收车的路上','跨省工作日记'])
def test_travel_intention_does_not_prove_city_boundary_or_vehicle_possession(title):
    ev=evidence();ev['speech'][0]['text']='同事比较忙，我只能一个人乘地铁去市区收一台车。'
    raw=proposal();raw['title']=title
    with pytest.raises(dm.DirectorMediaError) as result:
        dm.validate_plan(raw,ev,{}, {})
    assert result.value.code=='unsupported_claim'
    assert result.value.details[0]['field']=='title'


def test_explicit_trip_description_can_support_city_boundary_and_driving():
    ev=evidence();ev['speech'][0]['text']='今天我一个人开车，跨城去客户那里。'
    raw=proposal();raw['title']='一人一车跨城出行'
    dm.validate_plan(raw,ev,{}, {})


def test_supported_provider_receives_locked_dynamic_writer_schema(tmp_path):
    schemas=[]
    def provider(messages,tokens,response_schema=None):
        schemas.append(response_schema)
        prompt=messages[0]['content']
        if prompt == dm.AUDIT_PROMPT: reply=accepted_audit()
        elif '你只负责选题' in prompt: reply=selected_proposal()
        else:
            reply=written_proposal()
            reply['scene_notes']={s['id']:{'purpose':s['purpose'],'relation':s['relation']} for s in reply.pop('scenes')}
        return json.dumps(reply,ensure_ascii=False)
    provider.supports_response_schema=True
    with patch.object(dm,'_provider',return_value=provider):
        dm.plan_content(evidence(),'原声过程记录',{}, {'mode':'vlog'},data=tmp_path,work_dir=tmp_path/'plan')
    assert len(schemas)==3 and all(schemas)
    notes=schemas[1]['properties']['scene_notes']
    assert notes['required']==['T0-0-0','T0-1-1','T0-2-2']
    assert notes['additionalProperties'] is False
    assert 'scenes' not in schemas[1]['properties']
    assert all('narration' not in note['properties'] for note in notes['properties'].values())


def test_duration_adaptation_preserves_whole_opening_and_ending_and_original_order():
    rows=[{'id':f'T{i}','seconds':seconds,'text':text,'speech_ids':[f'S{i}'],
           'start':i*20.,'end':i*20.+seconds} for i,(seconds,text) in enumerate([
               (8.,'今天准备出发，先说明为什么去。'),(13.,'完整中间段落一，不应从句中切开。'),
               (10.,'完整中间段落二，沿途看到的情况。'),(7.,'到了入口，接下来在这里等一会儿。')])]
    original=copy.deepcopy(rows)
    kept,record=dm._fit_original_budget(rows,30.)
    assert [r['id'] for r in kept]==['T0','T1','T3']
    assert record['final_seconds']==28. and record['original_seconds']==38.
    assert record['protected_ids']==['T0','T3']
    assert record['removed'][0]['original_text']==rows[2]['text']
    assert record['removed'][0]['id']=='T2'
    assert rows==original
    assert all(r in original for r in kept)
    assert kept[0]['speech_ids']==['S0'] and kept[-1]['speech_ids']==['S3']


def test_duration_budget_never_expands_target_or_cuts_endpoints():
    rows=[{'id':'first','seconds':15.1,'text':'完整开场'}, {'id':'last','seconds':15.,'text':'完整收束'}]
    kept,record=dm._fit_original_budget(rows,30.)
    assert kept is None and record['passed'] is False
    assert record['minimum_seconds']==30.1 and record['target_seconds']==30.
    kept,record=dm._fit_original_budget(rows,30.1)
    assert kept==rows and record['removed']==[]


def test_impossible_complete_endpoints_report_material_gap_without_second_model_math(tmp_path):
    raw=selected_proposal();raw['candidates'][0]['ids']=['T0-0-0','T0-2-2']
    catalog=dm._executable_catalog(evidence(),30)
    with patch.object(dm,'_provider',return_value=lambda *a,**k:json.dumps(raw)), \
         patch.object(dm,'_executable_catalog',return_value=catalog):
        with pytest.raises(dm.DirectorMediaError) as error:
            dm.plan_content(evidence(),'十秒原声过程',{}, {'mode':'vlog','target_duration':10},data=tmp_path,work_dir=tmp_path/'plan')
    assert error.value.code=='material_shortfall'
    assert error.value.details[0]['budget_blocked'] is True
    assert (tmp_path/'plan/content-issues.json').exists()
    assert not (tmp_path/'plan/selection-1.response.txt').exists()


def test_adapted_selection_is_rewritten_and_audited_using_only_remaining_complete_quotes(tmp_path):
    calls=[]
    def provider(messages,tokens):
        prompt=messages[0]['content'];payload=json.loads(messages[1]['content']);calls.append((prompt,payload))
        if '你只负责选题' in prompt:return json.dumps(selected_proposal())
        if prompt == dm.AUDIT_PROMPT:return json.dumps(accepted_audit())
        written=proposal();written.pop('scenes');written['scene_notes']={r['id']:{'purpose':'原声表达用途','relation':'时间推进'} for r in payload['shots']}
        return json.dumps(written)
    with patch.object(dm,'_provider',return_value=provider):
        result=dm.plan_content(evidence(),'十五秒过程记录',{}, {'mode':'vlog','target_duration':15},data=tmp_path,work_dir=tmp_path/'plan')
    assert len(calls)==3
    assert [r['id'] for r in calls[1][1]['shots']]==['T0-0-0','T0-2-2']
    assert len(calls[2][1]['scenes'])==2
    assert result['actual_script']==evidence()['speech'][0]['text']+'\n'+evidence()['speech'][2]['text']
    assert result['selection_record']['duration_adaptation']['removed'][0]['id']=='T0-1-1'
    assert json.loads((tmp_path/'plan/selection-0.json').read_text(encoding='utf-8'))['duration_adaptation']['final_seconds']<=15


def test_writer_structure_locked_take_ids_resolve_to_scene_purposes():
    selected=dm._select_plan(selected_proposal(),dm._executable_catalog(evidence(),30),{'mode':'vlog','target_duration':30})
    written=proposal();written.pop('scenes')
    written['scene_notes']={f'T0-{i}-{i}':{'purpose':purpose,'relation':'按行程推进'} for i,purpose in enumerate([
        '说明今天出发的原因','观察沿途路面','到达入口并提醒台阶'])}
    written['structure']={'opening':'T0-0-0','development':'T0-1-1','ending':'T0-2-2'}
    original=copy.deepcopy(written)
    result=dm._expand_written(written,selected)
    assert result['structure']=={'opening':'说明今天出发的原因','development':'观察沿途路面','ending':'到达入口并提醒台阶'}
    assert written==original
    written['structure']['opening']='说明出发原因，再看沿途情况'
    assert dm._expand_written(written,selected)['structure']['opening']=='说明出发原因，再看沿途情况'
    written['structure']['ending']='T9-0-4'
    with pytest.raises(dm.DirectorMediaError,match='未选中的素材'):
        dm._expand_written(written,selected)


@pytest.mark.parametrize('title',['从计划到等待','从想法到行动','从出发到抵达','从陌生到熟悉','从预约到现场等待'])
def test_narrative_progression_is_not_rejected_as_named_geography(title):
    raw=proposal();raw['title']=title
    dm.validate_plan(raw,evidence(),{}, {})


def test_public_claims_do_not_use_an_unused_sentence_from_same_visual_range():
    ev=evidence()
    ev['speech'].append({'id':'S0-extra','source_index':0,'start':10.,'end':12.,'order':99,
                        'text':'我一个人开车跨城去客户那里。'})
    raw=proposal();raw['title']='一人一车跨城出行'
    with pytest.raises(dm.DirectorMediaError) as error:
        dm.validate_plan(raw,ev,{}, {})
    assert error.value.code=='unsupported_claim'


def test_revision_writer_never_receives_removed_previous_viewpoint_or_script(tmp_path):
    old_fact='需要在现场等待确认客户是否到达'
    revision={'instruction':'缩短一点，开头更直接','previous':{'viewpoint':old_fact,'actual_script':'客户还没到，坐下等一下'},
              'previous_scenes':[{'role':'opening','visual_id':'V0-0','speech_ids':['S0-0'],'spoken_text':'旧版台词','purpose':old_fact}]}
    calls=[]
    def provider(messages,tokens):
        prompt=messages[0]['content'];payload=json.loads(messages[1]['content']);calls.append((prompt,payload))
        return json.dumps(accepted_audit() if prompt==dm.AUDIT_PROMPT else selected_proposal() if '你只负责选题' in prompt else written_proposal())
    with patch.object(dm,'_provider',return_value=provider):
        plan=dm.plan_content(evidence(),'真实过程记录',{}, {'mode':'vlog'},data=tmp_path,work_dir=tmp_path/'plan',revision=revision)
    assert len(calls)==3
    assert old_fact not in json.dumps(calls,ensure_ascii=False)
    assert calls[1][1]['revision']=={'instruction':revision['instruction']}
    assert 'previous' not in calls[0][1]['revision']
    assert plan['revision']==revision  # Kept for history, never supplied as current factual evidence.


def test_direct_opening_revision_removes_leading_complete_sentences_not_last_point():
    original={'id':'T0-0-3','visual_id':'V0-0','source_index':0,'speech_ids':['S0-0','S0-1','S0-2','S0-3'],
              'seconds':13.,'start':0.,'end':13.,'text':'前部铺垫。没人陪同。我自己出发。乘地铁去客户那里。'}
    tail_trimmed={**original,'id':'T0-0-2','speech_ids':original['speech_ids'][:-1],
                  'seconds':9.,'end':9.,'text':'前部铺垫。没人陪同。我自己出发。'}
    direct={**original,'id':'T0-1-3','speech_ids':original['speech_ids'][1:],
            'seconds':10.,'start':3.,'text':'没人陪同。我自己出发。乘地铁去客户那里。'}
    ending={'id':'T1-0-0','visual_id':'V1-0','source_index':1,'speech_ids':['S1-0'],
            'seconds':7.,'start':0.,'end':7.,'text':'到了客户小区，接下来进去聊细节。'}
    selection={'mode':'vlog','rows':[tail_trimmed,ending]}
    catalog={'takes':[original,tail_trimmed,direct,ending]}
    revision={'instruction':'开头更直接','previous_scenes':[{'visual_id':'V0-0','speech_ids':original['speech_ids']}]}
    changed=dm._apply_direct_opening(selection,catalog,revision,{'target_duration':20})
    assert changed['rows']==[direct,ending]
    assert changed['opening_revision']['removed_leading_speech_ids']==['S0-0']
    assert changed['rows'][0]['speech_ids'][-1]=='S0-3'
    assert selection['rows'][0]==tail_trimmed
    assert changed['duration_adaptation']['final_seconds']==17.
    assert not dm._direct_opening_options(catalog,{**revision,'instruction':'不要更直接，保留开头铺垫'})


def test_direct_opening_never_breaks_sentence_or_budget_to_apply_revision():
    old={'id':'T0-0-1','visual_id':'V0-0','source_index':0,'speech_ids':['S0-0','S0-1'],
         'seconds':12.,'start':0.,'end':12.,'text':'铺垫。完整目的。'}
    direct={**old,'id':'T0-1-1','speech_ids':['S0-1'],'seconds':10.,'start':2.,'text':'完整目的。'}
    ending={'id':'T1-0-0','visual_id':'V1-0','source_index':1,'speech_ids':['S1-0'],'seconds':8.,'start':0.,'end':8.,'text':'到达后收束。'}
    selection={'mode':'vlog','rows':[old,ending]};catalog={'takes':[old,direct,ending]}
    revision={'instruction':'开头更直接','previous_scenes':[{'visual_id':'V0-0','speech_ids':old['speech_ids']}]}
    changed=dm._apply_direct_opening(selection,catalog,revision,{'target_duration':15})
    assert changed['rows']==selection['rows'] and not changed['opening_revision']['applied']


def test_story_cannot_pass_when_viewpoint_exists_only_in_metadata():
    plan=dm.validate_plan(proposal('narrated'),evidence(),{}, {'mode':'story'})
    audit=accepted_audit()
    result=dm._validated_audit(audit,len(plan['scenes']),plan)
    assert result['passed'] is False
    assert result['checks']['viewpoint_delivered'] is False
    assert any('实际旁白' in item['reason'] for item in result['issues'])
    # A fabricated citation to the metadata does not satisfy actual script delivery.
    audit['viewpoint_evidence']=[{'scene':1,'quote':plan['viewpoint']}]
    with pytest.raises(dm.DirectorMediaError,match='不在当前成片脚本'):
        dm._validated_audit(audit,len(plan['scenes']),plan)


def test_vlog_can_deliver_value_through_real_process_without_invented_summary_speech():
    plan=dm.validate_plan(proposal(),evidence(),{}, {'mode':'vlog'})
    result=dm._validated_audit(accepted_audit(),len(plan['scenes']),plan)
    assert result['passed'] and result['viewpoint_evidence']==[]
    assert plan['actual_script']=='\n'.join(s['text'] for s in evidence()['speech'])


def test_story_actual_narration_contains_supported_method_and_type_reaches_writer_and_audit(tmp_path):
    final_advice='步行时可以先看路面，再留意入口的台阶。'
    calls=[]
    def provider(messages,tokens):
        prompt=messages[0]['content'];payload=json.loads(messages[1]['content']);calls.append((prompt,payload))
        if '你只负责选题' in prompt:
            selected=selected_proposal();selected['mode']='narrated';selected['candidates'][0]['ids']=['V0-0','V0-1','V0-2']
            return json.dumps(selected)
        if prompt==dm.AUDIT_PROMPT:
            audit=accepted_audit();audit['viewpoint_evidence']=[{'scene':3,'quote':final_advice}]
            return json.dumps(audit)
        written=proposal('narrated')
        written['viewpoint']='步行需要同时留意沿路的路面与入口的台阶'
        written['audience_value']='获得先看路面、再留意入口台阶的观察顺序'
        for i,scene in enumerate(written['scenes']):
            scene['id']=f'V0-{i}';scene['fact_ids']=[]
        written['scenes'][2]['narration']=final_advice
        return json.dumps(written)
    with patch.object(dm,'_provider',return_value=provider):
        plan=dm.plan_content(evidence(),'解释步行时怎么观察这段路',{}, {'mode':'story'},data=tmp_path,work_dir=tmp_path/'plan')
    assert len(calls)==3
    assert calls[1][1]['production_kind']=='story' and calls[1][1]['mode']=='narrated'
    assert calls[2][1]['production_kind']=='story'
    assert calls[2][1]['actual_script']==plan['actual_script']
    assert final_advice in plan['actual_script']
    assert plan['content_review']['viewpoint_evidence']==[{'scene':3,'quote':final_advice}]
    assert plan['content_review']['checks']['viewpoint_delivered']


def test_story_audit_cannot_approve_missing_delivery_even_if_other_checks_pass():
    plan=dm.validate_plan(proposal('narrated'),evidence(),{}, {'mode':'story'})
    audit=accepted_audit();audit['passed']=False;audit['checks']['viewpoint_delivered']=False
    audit['issues']=[{'scene':3,'reason':'观点承诺观察方法，但结尾只复述到达，没有讲出观察顺序。'}]
    result=dm._validated_audit(audit,len(plan['scenes']),plan)
    assert result['passed'] is False
    missing=accepted_audit();missing['checks'].pop('viewpoint_delivered')
    with pytest.raises(dm.DirectorMediaError,match='完整的核对记录'):
        dm._validated_audit(missing,len(plan['scenes']),plan)


@pytest.mark.parametrize('requested,expected,family',[('vlog','vlog','T'),('story','narrated','V'),('narrated','narrated','V'),('salon','narrated','V')])
def test_selection_schema_cannot_mix_mode_and_identifier_families(requested,expected,family):
    catalog=dm._executable_catalog(evidence(),30)
    schema=dm._selection_schema(catalog,{'mode':requested})
    assert schema['properties']['mode']['enum']==[expected]
    ids=schema['properties']['candidates']['items']['properties']['ids']['items']['enum']
    assert ids and all(ident.startswith(family) for ident in ids)
    auto=dm._selection_schema(catalog,{'mode':'auto'})
    for branch in auto['oneOf']:
        mode=branch['properties']['mode']['enum'][0]
        ids=branch['properties']['candidates']['items']['properties']['ids']['items']['enum']
        assert all(ident.startswith('T' if mode=='vlog' else 'V') for ident in ids)


def test_story_writing_repair_keeps_verified_selection_and_repairs_actual_script(tmp_path):
    advice='步行时可以先看路面，再留意入口的台阶。'
    selected=selected_proposal();selected['mode']='narrated';selected['candidates'][0]['ids']=['V0-0','V0-1','V0-2']
    calls=[];counts={'selection':0,'writing':0,'audit':0}
    def provider(messages,tokens,response_schema=None):
        prompt=messages[0]['content'];payload=json.loads(messages[1]['content']);calls.append((prompt,payload,response_schema))
        if '你只负责选题' in prompt:
            counts['selection']+=1
            assert 'takes' not in payload['material']
            return json.dumps(selected)
        if prompt==dm.AUDIT_PROMPT:
            counts['audit']+=1;audit=accepted_audit()
            if counts['audit']==1:
                audit['passed']=False;audit['checks']['viewpoint_delivered']=False
                audit['issues']=[{'scene':3,'reason':'观点承诺观察方法，但实际结尾只说到达，未说出方法。'}]
            else:
                audit['viewpoint_evidence']=[{'scene':3,'quote':advice}]
            return json.dumps(audit)
        counts['writing']+=1
        written=proposal('narrated')
        for i,scene in enumerate(written['scenes']):scene.update(id=f'V0-{i}',fact_ids=[])
        written['scenes'][2]['narration']='走到建筑入口，这一段路已经走完了。' if counts['writing']==1 else advice
        return json.dumps(written)
    provider.supports_response_schema=True
    with patch.object(dm,'_provider',return_value=provider):
        result=dm.plan_content(evidence(),'根据这段路讲一个观察方法',{}, {'mode':'story'},data=tmp_path,work_dir=tmp_path/'plan')
    assert counts=={'selection':1,'writing':2,'audit':2}
    writers=[payload for prompt,payload,_ in calls if prompt==dm.WRITE_PROMPT]
    assert [s['id'] for s in writers[0]['shots']]==[s['id'] for s in writers[1]['shots']]
    assert writers[1]['previous_draft']['scenes'][2]['narration']=='走到建筑入口，这一段路已经走完了。'
    assert writers[1]['previous_issues'][0]['stage']=='writing_or_content_review'
    assert advice in result['actual_script'] and result['content_review']['passed']
    assert result['selection_record']['reused_for_writing_repair'] is True
    assert result['planning_attempts']==2 and result['planning_repair_history'][0]['code']=='content_check_failed'
    assert (tmp_path/'plan/audit-0/audit-0.response.txt').exists()
    assert (tmp_path/'plan/content-issues.json').exists()
    assert not (tmp_path/'plan/selection-1.response.txt').exists()


def test_invalid_selection_is_not_locked_and_can_be_reselected_once(tmp_path):
    counts={'selection':0,'writing':0,'audit':0}
    def provider(messages,tokens):
        prompt=messages[0]['content']
        if '你只负责选题' in prompt:
            counts['selection']+=1;selected=selected_proposal()
            if counts['selection']==1:selected['candidates'][0]['ids']=['V0-0','V0-1']
            return json.dumps(selected)
        if prompt==dm.AUDIT_PROMPT:
            counts['audit']+=1;return json.dumps(accepted_audit())
        counts['writing']+=1;return json.dumps(written_proposal())
    with patch.object(dm,'_provider',return_value=provider):
        result=dm.plan_content(evidence(),'原声记录',{}, {'mode':'vlog'},data=tmp_path,work_dir=tmp_path/'plan')
    assert counts=={'selection':2,'writing':1,'audit':1}
    assert result['selection_record']['selection_attempt']==1
    assert result['planning_repair_history'][0]['stage']=='selection'


@pytest.mark.parametrize('copy_text',[
    '单人收车，从地铁到小区，聊聊行程与等待的安排。',
    '从公司到小区，记录一次出行。',
    '从公交到物业，沿途看清入口。',
    '从地铁站台到客户小区，整理出行安排。',
    '从我们的公司到客户小区。',
    '从静安寺到公司，看看沿途。'])
def test_generic_places_and_transport_are_not_named_route_claims(copy_text):
    raw=proposal();raw['posting_copy']=copy_text
    dm.validate_plan(raw,evidence(),{}, {})


def test_explicit_named_route_allows_equivalent_wording_not_exact_whole_caption():
    ev=evidence();ev['speech'][0]['text']='今天从静安寺出发，坐地铁去青浦。'
    raw=proposal();raw['title']='从静安寺到青浦的出行'
    dm.validate_plan(raw,ev,{}, {})
    raw['title']='从青浦到静安寺的出行'
    with pytest.raises(dm.DirectorMediaError) as issue:
        dm.validate_plan(raw,ev,{}, {})
    assert issue.value.code=='unsupported_claim'


def test_story_revision_reaches_independent_audit_before_acceptance(tmp_path):
    instruction='去掉旧的开头，把结尾明确说成观察方法，不要只改主题。'
    advice='步行时可以先看路面，再留意入口的台阶。'
    counts={'writer':0,'audit':0,'selection':0};payloads=[]
    def provider(messages,tokens):
        prompt=messages[0]['content'];payload=json.loads(messages[1]['content']);payloads.append((prompt,payload))
        if '你只负责选题' in prompt:
            counts['selection']+=1;selected=selected_proposal();selected['mode']='narrated'
            selected['candidates'][0]['ids']=['V0-0','V0-1','V0-2'];return json.dumps(selected)
        if prompt==dm.AUDIT_PROMPT:
            counts['audit']+=1
            assert payload['revision_instruction']==instruction
            audit=accepted_audit();audit['viewpoint_evidence']=[{'scene':3,'quote':advice}]
            if counts['audit']==1:
                audit['passed']=False;audit['checks']['revision_satisfied']=False
                audit['issues']=[{'scene':1,'reason':'最新要求删除旧开头，但实际第一句仍未删除。'}]
            return json.dumps(audit)
        counts['writer']+=1
        assert next(iter(payload))=='revision'
        written=proposal('narrated')
        for i,scene in enumerate(written['scenes']):scene.update(id=f'V0-{i}',fact_ids=[])
        written['scenes'][2]['narration']=advice
        if counts['writer']==2:written['scenes'][0]['narration']='沿着这条路走进去，可以观察入口前的通行空间。'
        return json.dumps(written)
    with patch.object(dm,'_provider',return_value=provider):
        plan=dm.plan_content(evidence(),'讲解步行观察',{}, {'mode':'story'},data=tmp_path,work_dir=tmp_path/'plan',
                             revision={'instruction':instruction,'previous':{'viewpoint':'旧观点'}})
    assert counts=={'writer':2,'audit':2,'selection':1}
    assert plan['content_review']['checks']['revision_satisfied'] is True
    assert plan['planning_repair_history'][0]['code']=='content_check_failed'
    assert '通行空间' in plan['actual_script'] and advice in plan['actual_script']


def test_narrated_auditor_receives_cited_original_speech_not_just_rewritten_voice():
    plan=dm.validate_plan(proposal('narrated'),evidence(),{}, {'mode':'story'})
    assert plan['scenes'][0]['speech']==[]
    package=dm._audit_package(plan)
    assert package['scenes'][0]['source_speech'][0]['text']==evidence()['speech'][0]['text']
    assert package['scenes'][0]['source_speech'][0]['start']==1.
    assert package['scenes'][0]['spoken_text']!=package['scenes'][0]['source_speech'][0]['text']
    plan['scenes'][0]['evidence_ids'].append('S0-1')
    assert [s['id'] for s in dm._audit_package(plan)['scenes'][0]['source_speech']]==['S0-0']


def _actual_revision_review_case():
    ev=evidence()
    original=['今天同事比较忙，我只能一个人乘地铁去市区收车。',
              '总算到客户小区这里了，接下来进小区，跟客户聊一下托管的细节。',
              '客户还没到，叫我在物业这边坐一会儿。']
    spoken=['同事都忙，只能自己一个人坐地铁去市区收车。',
            '终于到了客户小区，准备进去聊聊托管的具体细节。',
            '客户还没到，先在物业等会儿，安排拜访时可以把路程、沟通和等待缓冲一起考虑。']
    for q,text in zip(ev['speech'],original):q['text']=text
    raw=proposal('narrated');raw.update(theme='独自拜访客户的行程与等待',viewpoint='拜访时行程与等待缓冲需统筹考虑',
        audience_value='为独自外勤提供实用的行程管理思路',title='独自收车路上的计划与等待',posting_copy='独自拜访客户，看看行程与等待。')
    for row,text in zip(raw['scenes'],spoken):row['narration']=text
    plan=dm.validate_plan(raw,ev,{}, {'mode':'story'})
    plan['revision']={'instruction':'结尾建议具体讲清：安排拜访时可以把路程、沟通和等待缓冲一起考虑。建议不要写成已经发生的事实。'}
    return plan


def test_disputed_style_and_plan_objections_get_one_evidence_based_review(tmp_path):
    plan=_actual_revision_review_case();calls=[]
    first=accepted_audit();first.update(passed=False)
    first['checks'].update(original_meaning=False,title_honest=False)
    first['issues']=[
        {'scene':3,'reason':'标题未覆盖核心行程统筹观点，易误导为纯路上记录，建议调整标题。'},
        {'scene':2,'reason':'准备聊细节虽为计划语气允许，但业务结果无视觉证据，存在轻微风险。'},
        {'scene':3,'reason':'安排拜访时可以把路程沟通等待缓冲一起考虑属于建议，符合故事模式要求，未判为编造。'}]
    first['viewpoint_evidence']=[{'scene':3,'quote':'安排拜访时可以把路程、沟通和等待缓冲一起考虑'}]
    final=accepted_audit();final['viewpoint_evidence']=first['viewpoint_evidence']
    final['suggestions']=['标题可再精炼，但当前标题未承诺不存在的成果。']
    final['summary']='原素材明确说接下来进小区聊托管细节，旁白保留计划语气；结尾实际说出建议，标题未夸大结果。'
    replies=iter([first,final])
    def provider(messages,tokens):
        calls.append(messages)
        return json.dumps(next(replies),ensure_ascii=False)
    report=dm.audit_content(plan,data=None,provider=provider,work_dir=tmp_path)
    assert report['passed'] and report['adjudication']['performed']
    assert report['adjudication']['initial_issues']==first['issues']
    assert len(calls)==2 and calls[1][0]['content']==dm.AUDIT_ADJUDICATION_PROMPT
    packet=json.loads(calls[1][1]['content'])
    assert '跟客户聊一下托管的细节' in packet['production']['scenes'][1]['source_speech'][0]['text']
    assert packet['previous_review']['passed'] is False
    assert (tmp_path/'audit-adjudication.response.txt').exists() and (tmp_path/'adjudication-record.json').exists()


def test_dispute_review_keeps_real_blocker_and_cannot_recurse(tmp_path):
    plan=_actual_revision_review_case()
    bad=accepted_audit();bad['passed']=False;bad['checks']['grounded']=False
    bad['issues']=[{'scene':2,'reason':'已经签约成功可能让人误以为业务已完成，但原声只有准备去谈。'}]
    bad['viewpoint_evidence']=[{'scene':3,'quote':'安排拜访时可以把路程、沟通和等待缓冲一起考虑'}]
    calls=[]
    def provider(messages,tokens):calls.append(messages);return json.dumps(bad,ensure_ascii=False)
    result=dm.audit_content(plan,data=None,provider=provider,work_dir=tmp_path)
    assert not result['passed'] and result['issues']==bad['issues']
    assert len(calls)==2 and result['adjudication']['performed']


def test_plan_tense_objections_are_dropped_when_the_script_stays_a_plan(tmp_path):
    plan=_actual_revision_review_case()
    review=accepted_audit();review['passed']=False
    review['checks'].update(grounded=False,original_meaning=False)
    review['issues']=[
        {'scene':2,'reason':"script claims '接下来就进小区' (future plan) as if already happened or confirmed action, but source keeps the plan."},
        {'scene':2,'reason':"script claims '跟对方聊聊托管细节' as fact, but source_speech only states the same plan."}]
    review['viewpoint_evidence']=[{'scene':3,'quote':'安排拜访时可以把路程、沟通和等待缓冲一起考虑'}]
    result=dm.audit_content(plan,data=None,provider=lambda *args: json.dumps(review),work_dir=tmp_path)
    assert result['passed'] is True
    assert result['issues']==[]
    assert len(result['dismissed_plan_state_issues'])==2
    assert result['checks']['grounded'] and result['checks']['original_meaning']


def test_plan_misread_filter_keeps_real_state_upgrade_and_missing_viewpoint():
    plan=_actual_revision_review_case()
    promoted=copy.deepcopy(plan)
    promoted['scenes'][2]['spoken_text']='客户还没到，我就在物业坐着。安排拜访时可以把路程、沟通和等待缓冲一起考虑。'
    sitting={'scene':3,'reason':"script claims '我就在物业坐着' as fact, but source only says 叫我坐一会儿."}
    assert dm._plan_state_false_positive(sitting,promoted) is False
    review=accepted_audit();review['passed']=False
    review['checks'].update(grounded=False,viewpoint_delivered=False)
    review['issues']=[
        {'scene':2,'reason':"script claims '接下来就进小区' (future plan) as if already happened"},
        {'scene':0,'reason':'观点型视频未提供任何实际旁白表达核心认识或方法的依据，不能仅凭主题字段通过。'}]
    result=dm._reconcile_plan_state_objections(dm._validated_audit(review,3,plan),plan)
    assert result['passed'] is False and result['checks']['grounded'] is False
    assert any('实际旁白' in item['reason'] for item in result['issues'])
    assert len(result['dismissed_plan_state_issues'])==1


def test_structure_cannot_claim_a_route_was_already_planned():
    raw=proposal('narrated');raw['structure']['development']='先规划好路线，再看沿途路面。'
    with pytest.raises(dm.DirectorMediaError) as issue:
        dm.validate_plan(raw,evidence(),{}, {'mode':'story'})
    assert issue.value.code=='unsupported_claim'
    assert any('规划好' in item['claim'] for item in issue.value.details)


def test_clear_factual_failure_is_not_automatically_reconsidered(tmp_path):
    plan=_actual_revision_review_case()
    bad=accepted_audit();bad['passed']=False;bad['checks']['grounded']=False
    bad['issues']=[{'scene':2,'reason':'台词说已经签约成功，但原素材明确只准备进小区聊细节，没有签约证据。'}]
    calls=[]
    def provider(messages,tokens):calls.append(messages);return json.dumps(bad,ensure_ascii=False)
    result=dm.audit_content(plan,data=None,provider=provider,work_dir=tmp_path)
    assert not result['passed'] and len(calls)==1


def test_nonblocking_style_suggestions_remain_visible_without_forcing_rewrite(tmp_path):
    plan=_actual_revision_review_case();review=accepted_audit()
    review['viewpoint_evidence']=[{'scene':3,'quote':'安排拜访时可以把路程、沟通和等待缓冲一起考虑'}]
    review['suggestions']=['标题可以更短，当前标题仍然准确。']
    result=dm.audit_content(plan,data=None,provider=lambda *a:json.dumps(review),work_dir=tmp_path)
    assert result['passed'] and result['suggestions']==review['suggestions']
    assert 'adjudication' not in result


def test_audit_quote_punctuation_maps_to_exact_original_spoken_text():
    spoken='客户还没到，先在物业等会儿，安排拜访时可以把路程、沟通和等待缓冲一起考虑。'
    model_quote='安排拜访时，可以把路程、沟通和等待缓冲一起考虑。'
    original='安排拜访时可以把路程、沟通和等待缓冲一起考虑。'
    assert dm._locate_spoken_quote(spoken,model_quote)==original
    assert dm._locate_spoken_quote(spoken,'安排拜访时 可以把路程沟通和等待缓冲一起考虑')==original
    plan=_actual_revision_review_case();audit=accepted_audit()
    audit['viewpoint_evidence']=[{'scene':3,'quote':model_quote}]
    checked=dm._validated_audit(audit,3,plan)
    assert checked['passed'] and checked['viewpoint_evidence']==[{'scene':3,'quote':original}]
    assert checked['viewpoint_evidence'][0]['quote'] in plan['scenes'][2]['spoken_text']


@pytest.mark.parametrize('spoken,quote',[
    ('安排拜访时可以把路程一起考虑。','安排拜访时可以把路程一起安排。'),
    ('不能把等待说成已经交付。','能把等待说成已经交付。'),
    ('并不是客户已经到了。','客户已经到了。'),
    ('不建议现在就承诺收益。','建议现在就承诺收益。'),
    ('收入是1000元。','收入是100元。'),
    ('收入是1000元。','100元。'),
    ('预计需要十五分钟。','五分钟。'),
    ('数值是1.5。','数值是15。'),
    ('数值是-3。','数值是3。'),
    ('数值是-3。','3。'),
    ('数值是1.5。','5。'),
    ('数值是3%。','3。'),
    ('这里没有任何完成交付的承诺。','已经完成交付。'),
])
def test_quote_alignment_does_not_ignore_words_numbers_or_negation(spoken,quote):
    assert dm._locate_spoken_quote(spoken,quote) is None


def test_quote_alignment_preserves_negation_when_it_is_in_the_quote():
    spoken='客户还没到。不建议把等待说成完成交付。'
    assert dm._locate_spoken_quote(spoken,'不建议把等待，说成完成交付。')=='不建议把等待说成完成交付。'


def _approved_revision_parent():
    plan=_actual_revision_review_case()
    plan['scenes'][1]['spoken_text']='先确认目的明确，准备进楼聊聊托管的细节。'
    plan['actual_script']='\n'.join(row['spoken_text'] for row in plan['scenes'])
    plan['script_sha256']=dm.hashlib.sha256(plan['actual_script'].encode()).hexdigest()
    review=accepted_audit();review['viewpoint_evidence']=[{'scene':3,'quote':'安排拜访时可以把路程、沟通和等待缓冲一起考虑。'}]
    plan['content_check']=review;plan['content_review']=review;plan['plan_sha256']=dm.digest(plan)
    return plan


def test_literal_local_revision_preserves_unrelated_scenes_sources_metadata_and_parent(tmp_path):
    parent=_approved_revision_parent();original=copy.deepcopy(parent);calls=[]
    instruction='删掉“先确认目的明确”；把“进楼”改成“进小区”；结尾改成：安排拜访时，可以把路程、沟通和等待缓冲一起考虑。其他内容保持不变。'
    def provider(messages,tokens):
        calls.append(messages)
        assert messages[0]['content']==dm.AUDIT_PROMPT
        packet=json.loads(messages[1]['content'])
        assert packet['revision_scope']['parent_review']['passed']
        assert packet['fact_ledger'][1]['entries'][-1]['state']=='intended'
        review=accepted_audit();review['viewpoint_evidence']=[{'scene':3,'quote':'安排拜访时，可以把路程、沟通和等待缓冲一起考虑。'}]
        return json.dumps(review)
    with patch.object(dm,'_provider',return_value=provider):
        revised=dm.revise_content(parent,instruction,data=tmp_path,work_dir=tmp_path/'revision')
    assert len(calls)==1 and parent==original
    assert revised['scenes'][0]==parent['scenes'][0]
    assert revised['scenes'][1]['spoken_text']=='准备进小区聊聊托管的细节。'
    assert '先确认目的明确' not in revised['actual_script'] and '进楼' not in revised['actual_script']
    assert all(item['passed'] for item in revised['revision_patch']['operation_checks'])
    for key in ('theme','viewpoint','audience_value','title','posting_copy','structure','source_evidence'):
        assert revised[key]==parent[key]
    for old,new in zip(parent['scenes'],revised['scenes']):
        assert {k:v for k,v in old.items() if k!='spoken_text'}=={k:v for k,v in new.items() if k!='spoken_text'}
    assert revised['parent_plan_sha256']==parent['plan_sha256']
    assert revised['plan_sha256']==dm.digest({k:v for k,v in revised.items() if k!='plan_sha256'})


def test_literal_opening_change_does_not_replan_brand_or_other_scenes(tmp_path):
    parent=_approved_revision_parent()
    def provider(messages,tokens):
        review=accepted_audit();review['viewpoint_evidence']=[{'scene':3,'quote':'安排拜访时可以把路程、沟通和等待缓冲一起考虑。'}]
        return json.dumps(review)
    with patch.object(dm,'_provider',return_value=provider):
        result=dm.revise_content(parent,'把开头改成：托管之前，先把规则问清楚。其他内容保持不变。',data=tmp_path,work_dir=tmp_path)
    assert result['scenes'][0]['spoken_text']=='托管之前，先把规则问清楚。'
    assert result['scenes'][1:]==parent['scenes'][1:]


def test_fact_ledger_separates_future_plan_received_arrangement_and_observed_state():
    ledger=dm.build_fact_ledger(_approved_revision_parent())
    arrival=ledger[1]['entries']
    assert next(row for row in arrival if '总算到' in row['text'])['state']=='reported_current_or_completed'
    assert next(row for row in arrival if '接下来' in row['text'])['state']=='intended'
    assert next(row for row in arrival if '聊一下' in row['text'])['state']=='intended'
    waiting=ledger[2]['entries']
    assert waiting[0]['state']=='not_happened' and waiting[1]['state']=='received_instruction'


def test_local_writer_cannot_promote_requested_sitting_to_observed_state(tmp_path):
    parent=_approved_revision_parent();calls=[]
    def provider(messages,tokens):
        calls.append(messages)
        return json.dumps({'patches':[{'scene':3,'text':'客户还没到，我就在物业坐着。安排拜访时可以把路程和等待一起考虑。','reason':'简短'}]})
    with patch.object(dm,'_provider',return_value=provider):
        with pytest.raises(dm.DirectorMediaError) as error:
            dm.revise_content(parent,'结尾更自然一点',data=tmp_path,work_dir=tmp_path)
    assert error.value.code=='unsupported_claim' and len(calls)==1


def test_explicit_unsupported_business_result_never_reaches_writer_or_audit(tmp_path):
    parent=_approved_revision_parent()
    with patch.object(dm,'_provider') as factory:
        provider=factory.return_value
        with pytest.raises(dm.DirectorMediaError) as error:
            dm.revise_content(parent,'把结尾改成：本次已经签约成功，保底收益一万元。其他内容保持不变。',data=tmp_path,work_dir=tmp_path)
    assert error.value.code=='unsupported_claim';provider.assert_not_called()


def test_inconsistent_seven_checks_never_become_pass_by_code(tmp_path):
    parent=_approved_revision_parent();bad=accepted_audit();bad['passed']=False;calls=[]
    def provider(messages,tokens):calls.append(messages);return json.dumps(bad)
    with pytest.raises(dm.DirectorMediaError) as error:
        dm.audit_content(parent,data=None,provider=provider,work_dir=tmp_path)
    assert error.value.code=='audit_inconsistent' and len(calls)==2
    assert (tmp_path/'audit-0.response.txt').exists() and (tmp_path/'audit-1.response.txt').exists()


def test_reviser_refuses_unsigned_parent_and_structural_mode_changes(tmp_path):
    parent=_approved_revision_parent();parent['title']='被篡改'
    with pytest.raises(dm.DirectorMediaError) as issue:
        dm.revise_content(parent,'开头更直接',data=tmp_path,work_dir=tmp_path)
    assert issue.value.code=='plan_changed'
    parent=_approved_revision_parent()
    with pytest.raises(dm.DirectorMediaError) as issue:
        dm.revise_content(parent,'缩短一点',data=tmp_path,work_dir=tmp_path,options={**parent['options'],'target_duration':20})
    assert issue.value.code=='unsupported_revision'


def test_state_transition_guard_applies_beyond_sitting_example():
    ledger={'entries':[{'text':'请我明天提交申请','state':'received_instruction'},
                       {'text':'准备联系客户','state':'intended'}]}
    for new in ('我已经提交申请。','我已经联系过客户。'):
        with pytest.raises(dm.DirectorMediaError) as error:
            dm._guard_revision_facts('收到申请安排，准备联系客户。',new,ledger,[])
        assert error.value.code=='unsupported_claim'
    dm._guard_revision_facts('收到申请安排。','接下来准备提交申请，然后联系客户。',ledger,[])


def test_empty_semantic_patch_cannot_be_claimed_as_finished_revision(tmp_path):
    parent=_approved_revision_parent();calls=[]
    def provider(messages,tokens):calls.append(messages);return json.dumps({'patches':[]})
    with patch.object(dm,'_provider',return_value=provider):
        with pytest.raises(dm.DirectorMediaError) as error:
            dm.revise_content(parent,'开头更直接，其他段落保持不变',data=tmp_path,work_dir=tmp_path)
    assert error.value.code=='revision_no_change' and len(calls)==2


def test_semantic_opening_patch_cannot_edit_other_paragraphs(tmp_path):
    parent=_approved_revision_parent();calls=[]
    def provider(messages,tokens):
        calls.append(messages)
        if messages[0]['content']==dm.REVISION_PROMPT:
            packet=json.loads(messages[1]['content']);assert packet['allowed_scenes']==[1]
            return json.dumps({'patches':[{'scene':1,'text':'托管之前，先把规则问清楚。','reason':'直接点出主题'}]})
        result=accepted_audit();result['viewpoint_evidence']=[{'scene':3,'quote':'安排拜访时可以把路程、沟通和等待缓冲一起考虑。'}]
        return json.dumps(result)
    with patch.object(dm,'_provider',return_value=provider):
        child=dm.revise_content(parent,'开头更直接，先点出托管前需要问清规则，其他段落保持不变。',data=tmp_path,work_dir=tmp_path)
    assert len(calls)==2 and child['scenes'][1:]==parent['scenes'][1:]
    assert child['scenes'][0]['spoken_text']=='托管之前，先把规则问清楚。'


def test_brand_retrieval_prioritizes_bound_stable_fact_and_keeps_source():
    facts=[{'id':f'fact-{i}','text':f'办理说明第{i}项，以确认资料为准。','source':'source-'+str(i)} for i in range(25)]
    facts[-1]['source']='verified-source/'+'x'*600
    context={'confirmed_facts':facts,'brand_assets':[{'fact_ids':['fact-24']}]}
    selected=dm._brand_facts(context,query='办理规则与小程序')
    assert selected[0]['id']=='fact-24' and selected[0]['source_id']=='fact-24'
    assert selected[0]['source']==facts[-1]['source']
    assert len(selected)<=12 and len(dm._prompt_facts(selected)[0]['source'])<=160
    assert dm._brand_facts({**context,'brand_usage':'off'})==[]


def _brand_evidence():
    value=evidence();value.pop('sha256')
    value['visuals'].append({'id':'B0-0','source_index':1,'start':0.,'end':18.,'description':'真实汽车托管问答首页，可见咨询输入框',
        'scene':'小程序界面','objects':['咨询输入框'],'actions':[],'visible_text':'汽车托管问答',
        'usable':True,'continuity':True,'kind':'brand_screenshot','brand_media_id':'asset-1',
        'brand_asset':{'id':'asset-1','sha256':'a'*64,'path':'frozen.png','fact_ids':['public-entry']},'fact_ids':['public-entry']})
    value['sha256']=dm.digest(value);return value


def test_brand_screenshot_and_stable_fact_survive_catalog_and_plan():
    ev=_brand_evidence();catalog=dm._executable_catalog(ev,45)
    branded=next(row for row in catalog['visuals'] if row['id']=='B0-0')
    assert branded['kind']=='brand_screenshot' and branded['brand_asset']['sha256']=='a'*64
    assert all(take['visual_id']!='B0-0' for take in catalog['takes'])
    context={'confirmed_facts':[{'id':'public-entry','text':'汽车托管问答首页提供咨询输入入口。','source':'verified-app-page'}],
             'brand_assets':[{'fact_ids':['public-entry']}]}
    raw=proposal('narrated');raw['scenes'][2].update(visual_id='B0-0',speech_ids=[],evidence_ids=['B0-0','public-entry'],
        narration='办理托管之前，可以先在问答首页了解相关规则。')
    plan=dm.validate_plan(raw,ev,context,{'mode':'story'})
    scene=plan['scenes'][2]
    assert scene['kind']=='brand_screenshot' and scene['brand_media_id']=='asset-1'
    assert scene['brand_asset']['sha256']=='a'*64 and plan['brand_facts'][0]['id']=='public-entry'
    raw['scenes'][2]['evidence_ids']=['B0-0']
    with pytest.raises(dm.DirectorMediaError,match='关联至少一条'):
        dm.validate_plan(raw,ev,context,{'mode':'story'})


def test_real_ten_second_ending_replacement_does_not_append_old_conclusion(tmp_path):
    parent=_approved_revision_parent()
    parent['scenes'][2].update(source_in=0.,source_out=10.4,
        spoken_text='客户未到需等待，现场观察环境也能辅助判断，建议预留缓冲时间。')
    parent['actual_script']='\n'.join(row['spoken_text'] for row in parent['scenes'])
    parent['script_sha256']=dm.hashlib.sha256(parent['actual_script'].encode()).hexdigest()
    parent.pop('plan_sha256');parent['plan_sha256']=dm.digest(parent)
    desired='安排拜访时，可以把路程、沟通和等待缓冲一起考虑。'
    def provider(messages,tokens):
        result=accepted_audit();result['viewpoint_evidence']=[{'scene':3,'quote':desired}]
        return json.dumps(result)
    with patch.object(dm,'_provider',return_value=provider):
        child=dm.revise_content(parent,'结尾改成：'+desired+'其他内容保持不变。',data=tmp_path,work_dir=tmp_path)
    assert child['scenes'][2]['spoken_text']==desired
    assert '辅助判断' not in child['actual_script']
    assert child['scenes'][:2]==parent['scenes'][:2]
    assert child['scenes'][2]['source_out']==10.4
    assert len(desired)<=int(10.4*4)
    assert child['revision_patch']['operations'][0]['scope']=='whole_scene'


def test_explicit_advice_only_edit_keeps_existing_factual_prefix():
    scenes=[{'spoken_text':'客户还没到，先在物业等会儿，建议预留缓冲时间。'}]
    ops=dm._literal_revision_operations('结尾的建议改成：安排拜访时，可以把等待缓冲一起考虑。')
    assert ops[0]['scope']=='advice_only'
    changed,_=dm._apply_literal_operations(scenes,ops)
    assert changed[0]['spoken_text']=='客户还没到，先在物业等会儿。安排拜访时，可以把等待缓冲一起考虑。'
