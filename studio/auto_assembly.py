"""Locked-script assembly from timed speech and bounded technical frame samples.

This is deliberately not a visual-language model. Speech overlap is a textual
selection signal, and silent material is only technically screened B-roll.
The prepared visual master and measured narration are durable review artifacts.
"""
from __future__ import annotations

from collections import Counter
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import re
from uuid import uuid4

from . import captions, creative, media, narrated, narration

MAX_ANALYSIS_SECONDS = 600.0
MAX_SAMPLES = 120
SAMPLE_WIDTH, SAMPLE_HEIGHT = 160, 90
TONE_MAP = ('zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,'
            'tonemap=hable:desat=2,zscale=t=bt709:m=bt709:r=tv,format=yuv420p')


class AutoAssemblyError(RuntimeError):
    def __init__(self, message, code='assembly_failed'):
        super().__init__(message)
        self.code = code


def _check_cancel(stop_event):
    if stop_event is not None and stop_event.is_set():
        raise AutoAssemblyError('自动选材已取消', 'cancelled')


def _run(command, folder, name, timeout, stop_event=None, env=None):
    _check_cancel(stop_event)
    try:
        captions._run(command, timeout=timeout, log_path=Path(folder)/(name+'.log'),
                      stop_event=stop_event, env=env)
    except captions.CaptionError as exc:
        _check_cancel(stop_event)
        raise AutoAssemblyError('素材分析或镜头准备未完成，请查看任务日志', 'analysis_unavailable') from exc


def _atomic_json(path, value):
    temporary = path.with_name(path.name+'.'+uuid4().hex+'.tmp')
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding='utf-8')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _fingerprint(path):
    stat = path.stat()
    key = f'assembly-v1:{path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:{stat.st_ctime_ns}'
    return hashlib.sha256(key.encode('utf-8')).hexdigest()


def _audio_alignment(info):
    offset = float(info.get('audio_start') or 0)-float(info.get('video_start') or 0)
    result = 'asetpts=PTS-STARTPTS'
    if offset > 0:
        result += f',adelay={round(offset*1000)}:all=1'
    elif offset < 0:
        result += f',atrim=start={-offset:.6f},asetpts=PTS-STARTPTS'
    return result


def _timed_speech(path, info, duration, folder, stop_event):
    if not info['has_audio']:
        return [], 'no_audio'
    config = captions.configuration_status()
    if not config['configured']:
        raise AutoAssemblyError('素材语音识别服务尚未就绪', 'analysis_unavailable')
    audio, output = folder/'source-audio.wav', folder/'speech.json'
    _run([media._tool('ffmpeg'), '-v', 'error', '-nostdin', '-y', *media.INPUT_OPTIONS,
          '-i', str(path), '-map', '0:a:0', '-vn', '-af',
          _audio_alignment(info)+f',apad=whole_dur={duration:.6f},atrim=duration={duration:.6f}',
          '-t', str(duration), '-ar', '16000', '-ac', '1', '-c:a', 'pcm_s16le', str(audio)],
         folder, 'extract-audio', max(120, duration*2), stop_event)
    request = folder/'speech-request.json'
    _atomic_json(request, {'audio':str(audio), 'model':config['model'], 'output':str(output)})
    env = dict(os.environ, HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
               TOKENIZERS_PARALLELISM='false', PYTHONUTF8='1')
    # The disposable caption worker checks >=5 GiB free VRAM before loading.
    # It exits before TTS begins, leaving the resident story service untouched.
    _run([config['python'], '-m', 'studio.caption_model', str(request)], folder,
         'speech-recognition', min(3600, max(600, duration*8)), stop_event, env)
    try:
        report = json.loads(output.read_text(encoding='utf-8'))
        if (report.get('clock') != 'final_video_seconds' or
                abs(float(report['duration'])-duration) > .15 or not isinstance(report.get('segments'), list)):
            raise ValueError('clock')
        segments = captions._validated_segments(report, duration) if report['segments'] else []
    except (OSError, ValueError, TypeError, KeyError, captions.CaptionError) as exc:
        raise AutoAssemblyError('素材识别缺少可靠时间戳，未猜测镜头区间', 'analysis_unavailable') from exc
    return segments, 'recognized_unverified' if segments else 'no_clear_speech'


def _frame_quality(pixels, resolution):
    count = len(pixels)
    mean = sum(pixels)/count
    variance = sum((p-mean)**2 for p in pixels)/count
    extreme = sum(p < 8 or p > 247 for p in pixels)/count
    edge = sum(abs(pixels[i]-pixels[i-1]) for i in range(1, count) if i % SAMPLE_WIDTH)/count
    reason = 'flat_frame' if variance < 6.25 else ('exposure' if extreme > .94 and (mean < 24 or mean > 235) else None)
    exposure = min(1.0, max(0.0, 1-abs(mean-128)/160))
    score = .30*min(1.0, resolution/1080)+.35*exposure+.35*min(1.0, edge/9)
    return {'score':round(score,4), 'usable':reason is None, 'reason':reason,
            'mean_luma':round(mean,2), 'luma_stddev':round(math.sqrt(variance),2), 'edge_proxy':round(edge,2)}


def _sample_quality(path, info, duration, folder, stop_event):
    spacing = max(1.0, duration/MAX_SAMPLES)
    raw = folder/'frames.gray'
    filters = f'setpts=PTS-STARTPTS,fps=fps={1/spacing:.9f}:start_time=0'
    if info.get('color_transfer') in ('smpte2084','arib-std-b67'):
        filters += ','+TONE_MAP
    filters += f',scale={SAMPLE_WIDTH}:{SAMPLE_HEIGHT},format=gray'
    _run([media._tool('ffmpeg'), '-v', 'error', '-nostdin', '-y', '-filter_threads', '2',
          *media.INPUT_OPTIONS, '-i', str(path), '-an', '-t', str(duration), '-vf', filters,
          '-frames:v', str(MAX_SAMPLES), '-pix_fmt', 'gray', '-f', 'rawvideo', str(raw)],
         folder, 'frame-samples', max(180, duration*3), stop_event)
    size = SAMPLE_WIDTH*SAMPLE_HEIGHT
    if not raw.is_file() or not 0 < raw.stat().st_size <= size*MAX_SAMPLES:
        raise AutoAssemblyError('素材画面抽样失败', 'analysis_unavailable')
    blob = raw.read_bytes()
    if len(blob) % size:
        raise AutoAssemblyError('素材画面抽样不完整', 'analysis_unavailable')
    resolution = min(info['display_width'], info['display_height'])
    return [{**_frame_quality(blob[i:i+size], resolution), 'time':round(i/size*spacing,6)}
            for i in range(0,len(blob),size)], spacing


def _analyze_source(path, index, data, work, stop_event):
    info = media.inspect_media(path)
    if info.get('field_order') not in (None,'unknown','progressive'):
        raise AutoAssemblyError('所选视频需要先转为逐行画面', 'invalid_request')
    coverage = min(MAX_ANALYSIS_SECONDS, info['duration'])
    key = _fingerprint(path)
    cache_dir = Path(data)/'assembly-analysis'; cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir/(key+'.json')
    try:
        cached = json.loads(cache_file.read_text(encoding='utf-8'))
        if (cached.get('version') == 1 and cached.get('fingerprint') == key
                and cached.get('clock') == 'source_video_seconds' and cached.get('samples')
                and isinstance(cached.get('segments'),list) and cached.get('coverage') == coverage):
            return {**cached,'source_index':index,'info':info,'cached':True}
    except (OSError,ValueError,TypeError):
        pass
    folder = work/f'analysis-{index:02}'; folder.mkdir(parents=True,exist_ok=True)
    samples, spacing = _sample_quality(path, info, coverage, folder, stop_event)
    if not any(s['usable'] for s in samples):
        segments, status = [], 'not_analyzed_unusable_visuals'
    else:
        segments, status = _timed_speech(path, info, coverage, folder, stop_event)
    result = {'version':1,'fingerprint':key,'source_index':index,'info':info,'coverage':coverage,
              'analyzed_ranges':[[0,coverage]],'clock':'source_video_seconds','segments':segments,
              'speech_status':status,'human_verified':False,'samples':samples,'sample_spacing':spacing,
              'visual_semantics':False,'scope':f'前 {coverage:.1f} 秒；原声机器转写未经校对；有限抽帧仅检查曝光、平坦画面和细节代理，不识别人物或场景。'}
    if _fingerprint(path) != key:
        raise AutoAssemblyError('分析期间素材发生变化，请重新选择', 'invalid_request')
    _atomic_json(cache_file,result)
    return {**result,'cached':False}


def _tokens(text):
    words = re.findall(r'[a-z0-9]+|[\u4e00-\u9fff]+', str(text).lower())
    result = set()
    for word in words:
        if re.fullmatch(r'[\u4e00-\u9fff]+',word):
            result.update(word[i:i+2] for i in range(len(word)-1))
        elif len(word)>1:
            result.add(word)
    return result-{'我们','你们','他们','这个','那个','可以','就是','一个','没有','然后','所以','时候','什么','这样','自己','不是'}


def _relevance(query, text):
    a,b = _tokens(query),_tokens(text)
    return len(a&b)/max(1,math.sqrt(len(a)*len(b)))


def _narration_units(audio, fps):
    total = math.ceil(float(audio['duration'])*fps-1e-8)
    segments = audio.get('segments',[])
    if not segments or total<=0:
        raise AutoAssemblyError('配音没有可用句子时间轴', 'narration_failed')
    previous = 0.0
    for segment in segments:
        start,end = float(segment['start']),float(segment['end'])
        if not (math.isfinite(start) and math.isfinite(end) and 0<=start<end<=audio['duration']+.001
                and start>=previous-.001 and isinstance(segment.get('text'),str)):
            raise AutoAssemblyError('配音句子时间轴无效', 'narration_failed')
        previous=end
    minimum, maximum = round(3*fps),round(6*fps)
    count=max(1, min(max(math.ceil(total/maximum),round(total/(4.5*fps))),total//minimum))
    sentence_boundaries=[round(s['start']*fps) for s in segments[1:]]
    boundaries=[0]
    for index in range(1,count):
        left=max(boundaries[-1]+minimum,total-(count-index)*maximum)
        right=min(boundaries[-1]+maximum,total-(count-index)*minimum)
        ideal=round(index*total/count)
        candidates=[n for n in sentence_boundaries if left<=n<=right]
        boundary=min(candidates,key=lambda n:abs(n-ideal)) if candidates else min(right,max(left,ideal))
        boundaries.append(boundary)
    boundaries.append(total)
    units=[]
    for start,end in zip(boundaries,boundaries[1:]):
        words=[s['text'] for s in segments if s['end']>start/fps and s['start']<end/fps]
        units.append({'start_frame':start,'end_frame':end,'frames':end-start,
                      'timeline_start':start/fps,'timeline_end':end/fps,'narration':' '.join(words)})
    return units


def _free_intervals(end_frame, occupied):
    cursor=0
    for start,end in sorted(occupied):
        if start>cursor:yield cursor,start
        cursor=max(cursor,end)
    if cursor<end_frame:yield cursor,end_frame


def _window_quality(source, start, end):
    samples=[s for s in source['samples'] if start<=s['time']<end]
    if not samples:
        samples=[min(source['samples'],key=lambda s:abs(s['time']-(start+end)/2))]
    if any(not s['usable'] for s in samples):return None
    return sum(s['score'] for s in samples)/len(samples)


def _choose_shots(analyses, audio, fps):
    units=_narration_units(audio,fps)
    occupied={s['source_index']:[] for s in analyses}
    usage=Counter();shots=[];previous=None
    # Leave one source-frame margin; FPS conversion must not require tail holds.
    limits={s['source_index']:max(0,math.floor((s['coverage']-max(1/fps,1/max(1,s['info'].get('fps') or fps)))*fps)) for s in analyses}
    if not any(any(x['usable'] for x in s['samples']) for s in analyses):
        raise AutoAssemblyError('没有通过画面检查的素材，请增加清晰的有效画面', 'no_usable_material')
    if sum(limits.values())<sum(u['frames'] for u in units):
        raise AutoAssemblyError('有效素材时长不足以覆盖完整配音，请增加素材或缩短讲稿', 'material_shortfall')
    for unit in units:
        best=None
        for source in analyses:
            index=source['source_index'];frames=unit['frames']
            for left,right in _free_intervals(limits[index],occupied[index]):
                if right-left<frames:continue
                # One-second search grid plus native speech boundaries; no fake scene labels.
                starts={left,right-frames}
                starts.update(range(left,right-frames+1,max(1,round(fps))))
                starts.update(max(left,min(right-frames,math.floor((s['start']-.1)*fps))) for s in source['segments']
                              if left/fps<=s['start']<right/fps)
                for start_frame in sorted(starts):
                    start,end=start_frame/fps,(start_frame+frames)/fps
                    quality=_window_quality(source,start,end)
                    if quality is None:continue
                    evidence=[s['text'] for s in source['segments'] if s['end']>start and s['start']<end]
                    text=' '.join(evidence);relevance=_relevance(unit['narration'],text)
                    boundary=1.0 if any(abs(s['start']-start)<.25 for s in source['segments']) else .5
                    score=.55*relevance+.30*quality+.15*boundary
                    score-=.10*(index==previous)+.16*usage[index]/max(1,sum(usage.values()))
                    # A modest edge penalty avoids mechanical reuse of every file's opening.
                    score-=.02 if start_frame==0 else 0
                    candidate=(score,-usage[index],-index,-start_frame)
                    if best is None or candidate>best[0]:
                        best=(candidate,source,start_frame,quality,relevance,text)
        if best is None:
            raise AutoAssemblyError('剩余有效片段不足，未循环画面或用定格补齐，请增加素材', 'material_shortfall')
        rank,source,start_frame,quality,relevance,text=best
        index=source['source_index'];end_frame=start_frame+unit['frames']
        occupied[index].append((start_frame,end_frame));usage[index]+=unit['frames'];previous=index
        basis='speech_text_match' if relevance>=.06 else 'technical_broll'
        shots.append({**unit,'source_index':index,'source_in':start_frame/fps,'source_out':end_frame/fps,
                      'selection_basis':basis,'selection_note':
                      '原声文字与讲稿相关；不代表画面内容已识别' if basis=='speech_text_match' else '技术质量可用的补充画面；未确认画面语义',
                      'score':round(max(0,min(1,rank[0]))*100,1),
                      'technical_score':round(quality*100,1),'text_relevance':round(relevance,4),
                      'source_speech':text[:1000]})
    return shots


def _count_frames(path):
    result=media._run([media._tool('ffprobe'),'-v','error',*media.INPUT_OPTIONS,'-select_streams','v:0',
                      '-count_frames','-show_entries','stream=nb_read_frames','-of','json',str(path)],300)
    try:return int(json.loads(result.stdout)['streams'][0]['nb_read_frames'])
    except (ValueError,KeyError,IndexError,TypeError) as exc:
        raise AutoAssemblyError('镜头帧数验证失败','render_failed') from exc


def _shot_thumbnail(path, frames, fps, stop_event=None):
    """Optional real frame from the middle of the already validated shot."""
    thumbnail=Path(path).with_suffix('.thumbnail.jpg')
    try:
        _check_cancel(stop_event)
        thumbnail.unlink(missing_ok=True)
        if frames<1 or fps<=0 or not Path(path).is_file():return None
        middle=min(frames-1,frames//2)
        # Decode the prepared clip, not a guessed position in its original file.
        filters=f"select='eq(n,{middle})',scale='min(320,iw)':-2"
        _run([media._tool('ffmpeg'),'-v','error','-nostdin','-y','-filter_threads','1',
              *media.INPUT_OPTIONS,'-i',str(path),'-map','0:v:0','-an','-vf',filters,
              '-frames:v','1','-q:v','4',str(thumbnail)],
             Path(path).parent,Path(path).stem+'-thumbnail',45,stop_event)
        if not thumbnail.is_file() or not 4<thumbnail.stat().st_size<=512*1024:return None
        jpeg=thumbnail.read_bytes()
        if not (jpeg.startswith(b'\xff\xd8') and jpeg.endswith(b'\xff\xd9')):return None
        return 'data:image/jpeg;base64,'+base64.b64encode(jpeg).decode('ascii')
    except (OSError,AutoAssemblyError,media.MediaError):
        # A failed optional preview must not invalidate a checked visual plan.
        # Cancellation remains a cancellation, including during image extraction.
        _check_cancel(stop_event)
        return None


def _prepare_visuals(paths, analyses, shots, work, options, settings, stop_event):
    cfg=creative._encoding({**settings,'normalize_audio':False},
        {'aspect':options.get('aspect','9:16'),'pace':'preserve'})
    width,height=media._dimensions(analyses[0]['info'],cfg);fps=cfg['fps']
    cfg.update(width=width,height=height)
    encoder='h264_nvenc' if cfg['encoder']=='nvenc' and min(width,height)>=256 and media._nvenc_available() else 'libx264'
    codec=['-c:v',encoder]+(['-preset','p4','-cq',str(int(cfg['crf'])),'-b:v','0'] if encoder=='h264_nvenc'
        else ['-preset','fast','-crf',str(int(cfg['crf'])),'-threads','4'])
    folder=work/'shots';folder.mkdir(parents=True,exist_ok=True)
    entries=[]
    for index,shot in enumerate(shots):
        _check_cancel(stop_event)
        source=paths[shot['source_index']];info=analyses[shot['source_index']]['info']
        target=folder/f'shot-{index:03}.mp4';duration=shot['frames']/fps
        filters=f'trim=duration={duration:.9f},setpts=PTS-STARTPTS'
        if info.get('color_transfer') in ('smpte2084','arib-std-b67'):filters+=','+TONE_MAP
        filters+=(f',scale=trunc(iw*sar/2)*2:ih,setsar=1,scale={width}:{height}:force_original_aspect_ratio=decrease,'
                  f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps={fps},format=yuv420p')
        # No stream_loop and no tpad: shortage is a failure, never an invisible hold.
        _run([media._tool('ffmpeg'),'-v','error','-nostdin','-xerror','-y','-filter_threads','2',
              '-ss',f'{shot["source_in"]:.9f}',*media.INPUT_OPTIONS,'-i',str(source),'-map','0:v:0','-an',
              '-vf',filters,'-frames:v',str(shot['frames']),*codec,'-color_primaries','bt709',
              '-color_trc','bt709','-colorspace','bt709','-map_metadata','-1','-movflags','+faststart',str(target)],
             folder,f'shot-{index:03}',max(180,duration*12),stop_event)
        if _count_frames(target)!=shot['frames']:
            raise AutoAssemblyError('选中区间实际帧数不足，未补帧或循环，请重新选材','render_failed')
        shot['prepared_path']=str(target)
        thumbnail=_shot_thumbnail(target,shot['frames'],fps,stop_event)
        if thumbnail is not None:shot['thumbnail']=thumbnail
        entries.extend([f"file '{target.name}'",f'duration {duration:.9f}'])
    manifest=folder/'concat.txt';manifest.write_text('\n'.join(entries)+'\n',encoding='utf-8')
    master=work/'selected-visuals.mp4'
    _run([media._tool('ffmpeg'),'-v','error','-nostdin','-y','-protocol_whitelist','file','-f','concat',
          '-safe','1','-i',str(manifest),'-map','0:v:0','-an','-c:v','copy','-movflags','+faststart',str(master)],
         work,'visual-master',300,stop_event)
    expected=sum(s['frames'] for s in shots)
    if _count_frames(master)!=expected or abs(media.inspect_media(master)['duration']-expected/fps)>.5/fps:
        raise AutoAssemblyError('拼接画面与锁定时间轴不一致','render_failed')
    return master,cfg


def build_auto_assembly(paths, spoken_script, options, settings, *, work_dir, data,
                        stop_event=None, on_stage=None):
    """Prepare exactly one reusable, full-script narrated composition."""
    options,settings=dict(options or {}),dict(settings or {})
    voice_id=options.get('voice_id','serena');style=options.get('voice_style','warm')
    try:narration._validate(spoken_script,voice_id,style)
    except narration.NarrationError as exc:
        raise AutoAssemblyError('讲稿或配音选项无效','invalid_request') from exc
    if not isinstance(paths,(list,tuple)) or not 1<=len(paths)<=12:
        raise AutoAssemblyError('请选择 1–12 段视频素材','invalid_request')
    paths=[Path(p).resolve() for p in paths]
    if len(set(paths))!=len(paths) or any(not p.is_file() for p in paths):
        raise AutoAssemblyError('素材不存在或被重复选择','invalid_request')
    work=Path(work_dir).resolve();work.mkdir(parents=True,exist_ok=True)
    def stage(name):
        _check_cancel(stop_event)
        if on_stage:on_stage(name)
    fps=media._settings(settings)['fps']
    stage('transcribing')
    analyses=[]
    for index,path in enumerate(paths):
        stage('transcribing')
        analyses.append(_analyze_source(path,index,data,work,stop_event))
    if not any(any(frame['usable'] for frame in source['samples']) for source in analyses):
        raise AutoAssemblyError('没有通过画面检查的素材，请增加清晰有效的画面','no_usable_material')
    stage('preparing_voice')
    try:
        audio=narration.synthesize(spoken_script,voice_id,work/'narration.wav',style=style,stop_event=stop_event)
    except narration.NarrationError as exc:
        _check_cancel(stop_event)
        raise AutoAssemblyError('完整讲稿配音未完成，请重试','narration_failed') from exc
    stage('planning_visuals')
    shots=_choose_shots(analyses,audio,fps)
    master,cfg=_prepare_visuals(paths,analyses,shots,work,options,settings,stop_event)
    for source,path in zip(analyses,paths):
        if _fingerprint(path)!=source['fingerprint']:
            raise AutoAssemblyError('选材期间素材发生变化，请重新选材','invalid_request')
    # The generic builder validates the already measured voice and hashes. A
    # single, exact-length master prevents its proportional-source allocator
    # from moving any of the reviewed scene boundaries.
    plan=narrated.build_narrated_plan([str(master)],spoken_script,voice_id,
        {'aspect':options.get('aspect','9:16'),'music':options.get('music',True),
         'subtitles':options.get('subtitles',True),'voice_style':style,'source_audio':'mute',
         'visual_fill':'reject','opening_title':options.get('opening_title','')},
        cfg,work_dir=work,stop_event=stop_event,prepared_narration=audio)
    expected=sum(s['frames'] for s in shots)
    if plan['loop_visuals'] or sum(c['frames'] for c in plan['clips'])!=expected:
        raise AutoAssemblyError('最终画面计划帧数不一致，未继续制作','render_failed')
    matched=sum(s['selection_basis']=='speech_text_match' for s in shots)
    used=len({s['source_index'] for s in shots})
    summary=f'完整讲稿 {plan["duration"]:.1f} 秒，选用 {used} 段素材的 {len(shots)} 个不重复镜头；其中 {matched} 个参考原声文字相关性。'
    plan['assembly']={'version':1,'script_sha256':hashlib.sha256(spoken_script.encode('utf-8')).hexdigest(),
        'script':spoken_script,'summary':summary,'shots':shots,'sources_analysis':analyses,
        'stats':{'shot_count':len(shots),'used_sources':used,'input_sources':len(paths),
                 'matched_shots':matched,'technical_broll_shots':len(shots)-matched,
                 'total_frames':expected,'duration':expected/fps,'repeated_seconds':0,
                 'freeze_seconds':0,'script_rewritten':False,'narration_generation_count':1},
        'visual_master':str(master),'visual_master_sha256':narration._sha(master)}
    plan['clips']=[{**s,'source':str(paths[s['source_index']]),'duration':s['frames']/fps,'visual_cycle':0} for s in shots]
    plan['warnings']=[w for w in plan['warnings'] if '按选择顺序' not in w and '比例取开头' not in w]
    plan['warnings'].append('镜头依据原声文字与有限技术抽样选择；无声或不相关片段仅作补画面，未进行画面语义识别。')
    plan['visual_plan']['warnings']=[w for w in plan['visual_plan']['warnings'] if '按素材选择顺序' not in w]
    _atomic_json(work/'assembly.json',plan['assembly'])
    return plan
