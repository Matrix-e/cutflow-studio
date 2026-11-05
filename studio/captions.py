"""Optional post-render subtitles, retaining final audio and the uncaptioned cut."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from uuid import uuid4

from . import media


class CaptionError(RuntimeError):
    pass


def configuration_status():
    executable=os.environ.get('CUTFLOW_CAPTION_PYTHON',os.environ.get('CUTFLOW_IDEA_PYTHON',''))
    model=os.environ.get('CUTFLOW_CAPTION_MODEL_DIR',os.environ.get('CUTFLOW_ASSET_WHISPER_MODEL',
        '/root/autodl-tmp/cutflow-studio/models/whisper-qa'))
    ready=bool(executable and Path(executable).is_file() and (Path(model)/'config.json').is_file())
    return {'configured':ready,'python':executable,'model':model,'max_duration_seconds':3600}


def _run(command, *, timeout, log_path, stop_event=None, env=None):
    if stop_event is not None and stop_event.is_set():
        raise CaptionError('字幕制作已取消')
    with Path(log_path).open('wb') as log:
        process=subprocess.Popen(command, stdin=subprocess.DEVNULL,stdout=log,stderr=log,
            cwd=Path(__file__).resolve().parent.parent,env=env,
            creationflags=0x08000000 if os.name=='nt' else 0)
        try:
            deadline=time.monotonic()+timeout
            while process.poll() is None:
                if stop_event is not None:
                    if stop_event.wait(.2):raise CaptionError('字幕制作已取消')
                else:time.sleep(.2)
                if time.monotonic()>deadline:raise CaptionError('字幕制作超时，请缩短视频后重试')
            if process.returncode:
                raise CaptionError('字幕处理失败；原成片未替换，请查看任务字幕日志')
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)


def _validated_segments(report, duration):
    if (not isinstance(report,dict) or report.get('clock')!='final_video_seconds'
            or not isinstance(report.get('segments'),list)):
        raise CaptionError('字幕识别结果格式无效')
    try:recorded=float(report['duration'])
    except (KeyError,TypeError,ValueError):raise CaptionError('字幕识别时长缺失') from None
    if not math.isfinite(recorded) or abs(recorded-duration)>.15:
        raise CaptionError('字幕时间轴与最终成片不一致')
    result=[];previous=0.0
    for item in report['segments']:
        try:
            start,end=float(item['start']),float(item['end'])
            text=' '.join(item['text'].split())
        except (KeyError,TypeError,ValueError,AttributeError):raise CaptionError('字幕句子无效') from None
        if (not all(math.isfinite(v) for v in (start,end)) or start<previous-.001
                or start<0 or end<=start or end>duration+.12 or not text or len(text)>500):
            raise CaptionError('字幕时间戳或内容未通过检查')
        end=min(duration,end)
        if end<=start:raise CaptionError('字幕句子超出成片')
        result.append({'start':start,'end':end,'text':text});previous=end
    if not result:
        raise CaptionError('未识别到可用语音，未生成虚构字幕；可关闭字幕后制作')
    return result


def _transcribe(source, folder, info, stop_event=None):
    config=configuration_status()
    if not config['configured']:
        raise CaptionError('原声字幕模型尚未配置完成')
    audio=folder/'final-audio.wav'
    _run([media._tool('ffmpeg'),'-v','error','-nostdin','-y',*media.INPUT_OPTIONS,
          '-i',str(source),'-map','0:a:0','-vn','-af',
          f'asetpts=PTS-STARTPTS,apad=whole_dur={info["duration"]}',
          '-t',str(info['duration']),'-ar','16000','-ac','1','-c:a','pcm_s16le',str(audio)],
         timeout=max(120,info['duration']*2),log_path=folder/'extract.log',stop_event=stop_event)
    request=folder/'request.json';output=folder/'transcript.json'
    request.write_text(json.dumps({'audio':str(audio),'model':config['model'],'output':str(output)}),encoding='utf-8')
    env=dict(os.environ,HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',TOKENIZERS_PARALLELISM='false',PYTHONUTF8='1')
    _run([config['python'],'-m','studio.caption_model',str(request)],
         timeout=min(3600,max(600,info['duration']*8)),log_path=folder/'recognition.log',stop_event=stop_event,env=env)
    return json.loads(output.read_text(encoding='utf-8'))


def _check_caption_output(path, original, folder, stop_event=None):
    current=media.inspect_media(path)
    tolerance=max(.08,2/max(1,original['fps']))
    checks=[{'name':'duration','passed':abs(current['duration']-original['duration'])<=tolerance},
            {'name':'dimensions','passed':(current['width'],current['height'])==(original['width'],original['height'])},
            {'name':'delivery_format','passed':current['video_codec']=='h264' and current['pixel_format']=='yuv420p'},
            {'name':'audio_preserved','passed':current['has_audio'] and current['audio_codec']==original['audio_codec']
                and current['audio_sample_rate']==original['audio_sample_rate']
                and current['audio_channels']==original['audio_channels']}]
    _run([media._tool('ffmpeg'),'-v','error','-nostdin','-xerror',*media.INPUT_OPTIONS,
          '-i',str(path),'-f','null','-'],timeout=max(180,original['duration']*3),
         log_path=folder/'decode.log',stop_event=stop_event)
    checks.append({'name':'full_decode','passed':True})
    return {'passed':all(check['passed'] for check in checks),'checks':checks}


def add_captions(output_path, work_dir, settings=None, stop_event=None):
    """Burn timed ASR text into the final cut; replace only after full decode QC.

    The caller invokes this only when subtitles=True and merges this report into
    its existing render report. Existing audio is stream-copied unchanged.
    """
    source=Path(output_path).resolve(strict=True)
    info=media.inspect_media(source)
    if not info['has_audio']:
        raise CaptionError('这条成片没有音轨，无法识别字幕')
    if info['duration']>3600:
        raise CaptionError('自动字幕暂支持一小时以内的成片，请按章节拆分')
    if info['audio_start'] is not None and abs(info['audio_start']-info['video_start'])>.08:
        raise CaptionError('成片音画起点不一致，字幕暂未生成')
    folder=Path(work_dir).resolve()/('captions-'+uuid4().hex);folder.mkdir(parents=True)
    original_stat=source.stat()
    report=_transcribe(source,folder,info,stop_event)
    segments=_validated_segments(report,info['duration'])
    srt=folder/'captions.srt'
    srt.write_text('\n\n'.join(f'{index+1}\n{media._format_srt(s["start"])} --> {media._format_srt(s["end"])}\n{s["text"]}'
                  for index,s in enumerate(segments))+'\n',encoding='utf-8')
    from .narrated import _subtitle_filter
    video_filter=_subtitle_filter(srt,info['display_width'],info['display_height'],folder/'captions.ass')
    encoder=(settings or {}).get('encoder','cpu')
    codec=['-c:v','h264_nvenc','-preset','p4','-cq','20'] if encoder=='nvenc' else ['-c:v','libx264','-preset','fast','-crf','20','-threads','4']
    with tempfile.TemporaryDirectory(prefix='.captions-',dir=source.parent) as temporary:
        staged=Path(temporary)/'captioned.mp4'
        _run([media._tool('ffmpeg'),'-v','error','-nostdin','-y','-filter_threads','2',*media.INPUT_OPTIONS,
              '-i',str(source),'-map','0:v:0','-map','0:a:0','-vf',video_filter,*codec,
              '-pix_fmt','yuv420p','-c:a','copy','-movflags','+faststart',str(staged)],
             timeout=max(300,info['duration']*8),log_path=folder/'burn.log',stop_event=stop_event)
        qc=_check_caption_output(staged,info,folder,stop_event)
        if not qc['passed']:raise CaptionError('烧录字幕后的成片未通过检查，原成片保持不变')
        if stop_event is not None and stop_event.is_set():raise CaptionError('字幕制作已取消')
        now=source.stat()
        if (now.st_size,now.st_mtime_ns)!=(original_stat.st_size,original_stat.st_mtime_ns):
            raise CaptionError('字幕制作期间成片发生变化，未覆盖新版本')
        backup=folder/'uncaptioned.mp4'
        shutil.copy2(source,backup)
        staged.replace(source)
    metadata=media.inspect_media(source)
    result={'output_path':str(source),'duration':metadata['duration'],'metadata':metadata,'qc':qc,
            'srt_path':str(srt),'transcript_path':str(folder/'transcript.json'),'subtitles_burned':True,
            'uncaptioned_path':str(backup),'subtitle_count':len(segments),
            'warnings':['字幕由原声自动识别，专有名词和断句请在成片中复核。']}
    (folder/'result.json').write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8')
    return result
