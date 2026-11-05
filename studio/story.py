"""Story-led montage; selected footage supports measured narration and real app images."""
import json
import math
import os
from pathlib import Path
from . import media, creative, narration, narrated, vlog
from .asset_insights import fingerprint
from .captions import _run as run_process, CaptionError


def _source_evidence(path, index, data, work, stop_event=None):
    info=media.inspect_media(path)
    coverage=min(300.0,info['duration'])
    scope=f'原声机器转写，未经人工校对；仅分析前 {coverage:.1f} 秒（最多300秒），未进行视觉语义识别。'
    insight={};cached=False
    if info['has_audio']:
        try:
            candidate=json.loads((Path(data)/'asset-insights'/fingerprint(Path(path))/'result.json').read_text(encoding='utf-8'))
            if candidate.get('status')=='ready' and isinstance(candidate.get('transcript'),str):
                insight=candidate;cached=True
        except (OSError,ValueError):pass
        if not cached:
            from .captions import configuration_status
            config=configuration_status()
            if not config['configured']:
                raise RuntimeError('所选素材还没有原声分析，且 GPU 语音模型未就绪；不能凭文件名编造故事背景')
            folder=Path(work)/f'source-evidence-{index}';folder.mkdir(parents=True,exist_ok=True)
            env=dict(os.environ,HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',TOKENIZERS_PARALLELISM='false')
            try:
                run_process([config['python'],'-m','studio.asset_insights_model',str(path),str(folder),config['model']],
                            timeout=600,log_path=folder/'evidence.log',stop_event=stop_event,env=env)
            except CaptionError as exc:
                raise RuntimeError('素材原声分析未完成，请稍后重试；未继续猜测素材内容') from exc
            insight=json.loads((folder/'speech.json').read_text(encoding='utf-8'))
            if not isinstance(insight.get('transcript'),str) or not isinstance(insight.get('summary'),str):
                raise RuntimeError('原声分析结果不完整，未继续编造内容')
    else:
        scope='此素材没有音轨；未进行视觉语义识别，只作为候选画面。'
    transcript=insight.get('transcript','')
    return {'index':index,'duration':info['duration'],'speech_summary':insight.get('summary',''),
            'speech':transcript[:6000],'speech_scope':scope,'speech_status':
            'recognized_unverified' if transcript else 'no_clear_speech' if info['has_audio'] else 'no_audio',
            'speech_text_truncated':len(transcript)>6000,'speech_cached':cached}


def _scene_timing(scenes,audio,fps):
    """Round cumulative boundaries once, never independently round scene lengths."""
    if not math.isfinite(fps) or not 10<=fps<=60:raise RuntimeError('故事帧率无效')
    segments=audio['segments'];cursor=0;audio_clock=0.0;frame_clock=0;timing=[]
    for scene in scenes:
        count=len(narration.split_sentences(scene['narration']))
        if count<1 or cursor+count>len(segments):raise RuntimeError('旁白时间轴与故事段落不匹配')
        cursor+=count
        end=float(segments[cursor]['start']) if cursor<len(segments) else float(audio['duration'])
        if not math.isfinite(end) or end<=audio_clock:raise RuntimeError('旁白段落时间戳无效')
        boundary=round(end*fps) if cursor<len(segments) else math.ceil(end*fps-1e-8)
        if boundary<=frame_clock:raise RuntimeError('故事段落不足一帧，不能生成猜测画面时间')
        timing.append({'audio_start':audio_clock,'audio_end':end,'start_frame':frame_clock,
                       'end_frame':boundary,'frames':boundary-frame_clock,
                       'timeline_start':frame_clock/fps,'timeline_end':boundary/fps})
        audio_clock=end;frame_clock=boundary
    if cursor!=len(segments):raise RuntimeError('旁白段落数量不一致')
    return timing


def _exact_visual_plan(shots,timing,aspect,settings):
    base=creative.build_creation_plan(shots,'',{'aspect':aspect,'opening_title':'','music':False,
        'keep_original_audio':False,'pace':'preserve','target_duration':None},
        {**settings,'normalize_audio':False})
    fps=base['settings']['fps']
    for index,expected in enumerate(timing):
        actual=[clip for clip in base['clips'] if clip['source_index']==index]
        if (sum(clip['frames'] for clip in actual)!=expected['frames'] or not actual
                or abs(actual[0]['timeline_start']-expected['start_frame']/fps)>1e-5
                or abs(actual[-1]['timeline_end']-expected['end_frame']/fps)>1e-5):
            raise RuntimeError('场景画面帧数未与旁白边界对齐，未按比例调整镜头')
    return base

def build_story_plan(paths, brief, options, settings, *, work_dir, data, stop_event=None, on_stage=None):
    from .brand_context import model_context
    from .brand_media import list_screenshots, resolve_screenshot
    from .story_model import validate
    work=Path(work_dir);work.mkdir(parents=True,exist_ok=True)
    def stage(s):
        if on_stage:on_stage(s)
    stage('transcribing')
    source_info=[_source_evidence(p,i,data,work,stop_event) for i,p in enumerate(paths)]
    screens=list_screenshots(data)
    topic='\n'.join([brief,*[item['speech_summary'] for item in source_info if isinstance(item.get('speech_summary'),str)]])
    context={'brief':brief,'target_seconds':options.get('target_duration') or 45,'brand_context':model_context(data,query=topic),
             'sources':source_info,'screenshots':[{'id':s['id'],'name':s['name']} for s in screens[:12]]}
    request={'context':context,'output':str(work/'storyboard.json')}
    (work/'story-request.json').write_text(json.dumps(request,ensure_ascii=False),encoding='utf-8')
    stage('planning_story')
    env=dict(os.environ,HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',TOKENIZERS_PARALLELISM='false')
    try:
        run_process([env['CUTFLOW_IDEA_PYTHON'],'-m','studio.story_model',str(work/'story-request.json')],
                    timeout=300,log_path=work/'story-model.log',stop_event=stop_event,env=env)
    except CaptionError as exc:
        raise RuntimeError('故事策划未完成或未通过内容检查，请补充业务知识或重试；原素材保持完整') from exc
    story=validate(json.loads((work/'storyboard.json').read_text(encoding='utf-8')),len(paths),{s['id'] for s in screens},context=context)
    script='\n'.join(s['narration'].strip() for s in story['scenes'])
    stage('preparing_voice')
    audio=narration.synthesize(script,'serena',work/'story-voice.wav',style='warm',stop_event=stop_event)
    stage('planning_visuals')
    prepared=vlog.prepare_sources(paths,work,settings)
    aspect=options.get('aspect','9:16');w,h={'9:16':(1080,1920),'16:9':(1920,1080),'1:1':(1080,1080),'original':(1080,1920)}[aspect]
    codec=['-c:v','h264_nvenc','-preset','p4','-cq','20'] if settings.get('encoder')=='nvenc' else ['-c:v','libx264','-preset','fast','-crf','20']
    fps=float(settings.get('fps',30));timing=_scene_timing(story['scenes'],audio,fps)
    stage('planning_visuals');shots=[]
    for i,(scene,clock) in enumerate(zip(story['scenes'],timing)):
        duration=clock['frames']/fps
        shot=work/f'scene-{i}.mp4'
        args=[media._tool('ffmpeg'),'-v','error','-nostdin','-y']
        if scene.get('screenshot_id'):
            image=resolve_screenshot(data,scene['screenshot_id'])
            args+=['-loop','1','-framerate',str(fps),'-i',str(image)]
        else:
            source=prepared[scene['source_index']];info=media.inspect_media(source)
            start=min(scene['start'],max(0,info['duration']-1))
            args+=['-stream_loop','-1','-ss',str(start),*media.INPUT_OPTIONS,'-i',str(source)]
        vf=f'scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=0x152b25,setsar=1,fps={fps},format=yuv420p'
        media._run(args+['-an','-frames:v',str(clock['frames']),'-vf',vf,*codec,str(shot)],max(180,duration*8))
        scene.update(clock);shots.append(str(shot))
    plan=narrated.build_narrated_plan(shots,script,'serena',
        {'aspect':aspect,'music':options.get('music',True),'subtitles':options.get('subtitles',True),
         'voice_style':'warm','source_audio':'mute','visual_fill':'loop','opening_title':options.get('opening_title','')},
        settings,work_dir=work,stop_event=stop_event,prepared_narration=audio)
    # build_narrated_plan validates the supplied voice hashes but its generic
    # proportional allocator is inappropriate for already timed story scenes.
    exact=_exact_visual_plan(shots,timing,aspect,settings)
    plan.update(visual_plan=exact,settings=exact['settings'],duration=timing[-1]['end_frame']/fps,
                clips=[{**clip,'visual_cycle':0} for clip in exact['clips']],loop_visuals=False,
                storyboard=story,brand_context=context['brand_context'],story_mode=True,
                source_evidence_scope=[s['speech_scope'] for s in source_info])
    plan['warnings']=[w for w in plan['warnings'] if '按选择顺序' not in w and '比例取开头' not in w]
    plan['warnings'].append('根据原声摘要与品牌知识策划，使用独立旁白；没有对原片人物重新对口型。')
    return plan
