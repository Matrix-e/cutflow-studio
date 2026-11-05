"""Run bounded, real GPU acceptance jobs for the six existing creation modes.

Run on the GPU with the active release's Python. Credentials stay in memory and
are never logged. Existing jobs, inputs and configurations are never changed.
No approval, publication or external paid service call is performed.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from difflib import SequenceMatcher
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))

MODES = ('edit','vlog','story','narrated_promo','brand_promo','talking_head')
SCRIPT = '今天，一个人出发去见客户。路程中的观察，也是真实工作的一部分。'
NARRATED = {'story','narrated_promo','brand_promo','talking_head'}


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary=path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    temporary.replace(path)


def fingerprint(path):
    result=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):result.update(block)
    return result.hexdigest()


def normalize(text):
    return ''.join(re.findall(r'[a-z0-9\u3400-\u9fff]',str(text).lower()))


def load_server_environment(data):
    """Read only the active local service's model paths; never print its env."""
    process=json.loads((data/'processes.json').read_text(encoding='utf-8'))
    pid=int(process['server']);proc=Path('/proc')/str(pid)
    command=(proc/'cmdline').read_bytes().replace(b'\x00',b' ')
    if b'uvicorn' not in command or b'studio.server:app' not in command:
        raise RuntimeError('Active service process could not be verified')
    allowed=('CUTFLOW_TTS_', 'CUTFLOW_CAPTION_', 'CUTFLOW_IDEA_', 'CUTFLOW_ASSET_',
             'CUTFLOW_PRESENTER_', 'CUTFLOW_STORY_')
    for part in (proc/'environ').read_bytes().split(b'\x00'):
        key,sep,value=part.partition(b'=')
        if not sep:continue
        key=key.decode('utf-8',errors='replace')
        if key.startswith(allowed) or key in {'CUTFLOW_DATA','CUTFLOW_INBOX','HF_HOME','TMPDIR','PATH','CUDA_VISIBLE_DEVICES'}:
            os.environ[key]=value.decode('utf-8',errors='replace')
    return {'server_pid':pid,'release':process.get('release'),'model_environment_loaded':True}


class API:
    def __init__(self,base,token):
        parsed=urllib.parse.urlsplit(base)
        if parsed.scheme!='http' or parsed.hostname not in {'127.0.0.1','localhost','::1'} or parsed.username or parsed.password:
            raise ValueError('Audit API must be a loopback HTTP address')
        self.base=base.rstrip('/');self.token=token
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self,path,body=None):
        raw=json.dumps(body,ensure_ascii=False).encode('utf-8') if body is not None else None
        request=urllib.request.Request(self.base+path,data=raw,
            headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json'},
            method='POST' if body is not None else 'GET')
        try:
            with self.opener.open(request,timeout=30) as response:
                return response.status,json.loads(response.read(4*1024*1024).decode('utf-8'))
        except urllib.error.HTTPError as error:
            try:detail=json.loads(error.read(65536).decode('utf-8'))
            except (ValueError,UnicodeError):detail={'detail':'API returned an error'}
            return error.code,detail

    def download_hash(self,path,timeout=180):
        request=urllib.request.Request(self.base+path,headers={'Authorization':'Bearer '+self.token})
        digest=hashlib.sha256();size=0;deadline=time.monotonic()+timeout
        with self.opener.open(request,timeout=30) as response:
            for chunk in iter(lambda:response.read(1024*1024),b''):
                if time.monotonic()>deadline:raise TimeoutError('Download verification timed out')
                digest.update(chunk);size+=len(chunk)
            return {'http_status':response.status,'size':size,'sha256':digest.hexdigest(),
                    'content_type':response.headers.get('Content-Type')}


def find_source(assets,number):
    pattern=re.compile(r'video\s*[（(]'+str(number)+r'[）)]',re.I)
    candidates=[item for item in assets if pattern.search(str(item.get('name',''))+' '+str(item.get('source','')))]
    return sorted(candidates,key=lambda item:str(item.get('source','')))[0]['source'] if candidates else None


def make_payload(mode,source,extras,voice,portrait,run_id):
    common={'mode':mode,'sources':[] if mode=='talking_head' else [source],
            'brief':'保留真实出发过程，用现场画面呈现一次见客户前的工作记录。',
            'title':'入口验收 · '+mode+' · 真实出发记录',
            'options':{'aspect':'9:16','music':True,'subtitles':True},
            'idempotency_key':'director-'+run_id+'-publicmode-'+mode}
    if mode=='edit':
        common['options'].update(target_duration=12,pace='preserve',keep_original_audio=True)
    elif mode=='vlog':
        common['sources']=list(dict.fromkeys([extras[0] or source,source,extras[1] or source]))
        common['options']['keep_original_audio']=True
    else:
        common['spoken_script']=SCRIPT
        common['options']['target_duration']=None
        if mode!='story':
            common.update(voice_id=voice,visual_fill='reject')
            common['options'].update(source_audio='mute',voice_style='warm')
        if mode=='brand_promo':common.update(subtype='salon_recap',brand={'name':'Super Ecar'})
        if mode=='talking_head':
            common['presenter']={'kind':'public','portrait_id':portrait['id'],'consent_confirmed':True}
            common['voice_id']=portrait.get('recommended_voice_id') or voice
    return common


def local_job(data,identifier):
    with sqlite3.connect(f'file:{(data/"studio.sqlite3").as_posix()}?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        row=db.execute('SELECT id,status,output_path,result,plan,settings FROM jobs WHERE id=?',(identifier,)).fetchone()
    if not row:raise RuntimeError('Job missing from durable store')
    record=dict(row)
    for key in ('result','plan','settings'):record[key]=json.loads(record[key]) if record[key] else None
    return record


def wait_until_idle(api,timeout=180):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        code,jobs=api.request('/api/jobs?limit=100')
        if code!=200:raise RuntimeError('Cannot verify render queue state')
        active=[j for j in jobs.get('jobs',[]) if j['status'] in {'queued','analyzing','rendering','publishing'}]
        code,productions=api.request('/api/productions?limit=100')
        if code==200:active += [j for j in productions.get('productions',[]) if j['status'] in {'queued','running'}]
        if not active:return True
        time.sleep(3)
    return False


def inspect_output(api,data,identifier,mode,work):
    from studio import media,captions
    record=local_job(data,identifier)
    output=Path(record['output_path'] or '')
    if not output.is_file() or not output.resolve().is_relative_to(data.resolve()):
        raise RuntimeError('Rendered output missing or outside application data')
    info=media.inspect_media(output);checks=[]
    def check(name,passed,detail):checks.append({'name':name,'passed':bool(passed),'detail':detail})
    process=media._run([media._tool('ffmpeg'),'-v','error','-nostdin','-xerror',*media.INPUT_OPTIONS,
                       '-i',str(output),'-f','null','-'],max(120,info['duration']*5))
    check('full_decode',process.returncode==0,'实际成片从头到尾完整解码。')
    check('format',info['video_codec']=='h264' and info['pixel_format']=='yuv420p' and info['has_audio'],
          {'codec':info['video_codec'],'pixels':info['pixel_format'],'audio':info['has_audio']})
    check('audio_tail',abs(info['audio_duration']-info['duration'])<.25,
          {'video_seconds':info['duration'],'audio_seconds':info['audio_duration']})
    download=api.download_hash(f'/api/jobs/{identifier}/video')
    check('download',download['size']==output.stat().st_size and download['sha256']==fingerprint(output),download)
    # Re-recognize the rendered audio, not the requested text or old source file.
    if not wait_until_idle(api):raise RuntimeError('Another task is using the GPU; actual audio review was not started')
    transcript=captions._transcribe(output,work,info)
    text=''.join(segment.get('text','') for segment in transcript.get('segments',[]))
    check('actual_speech',bool(normalize(text)),{'recognized_text':text,'segments':len(transcript.get('segments',[]))})
    if mode in NARRATED:
        ratio=SequenceMatcher(None,normalize(SCRIPT),normalize(text),autojunk=False).ratio()
        check('locked_script_audio',ratio>=.78,{'similarity':ratio,'expected':SCRIPT,'observed':text})
    srt=(record.get('result') or {}).get('srt_path')
    if not srt and output.with_suffix('.srt').is_file():srt=str(output.with_suffix('.srt'))
    check('subtitle_file',bool(srt and Path(srt).is_file() and Path(srt).read_text(encoding='utf-8').strip()),
          '交付字幕文件存在且非空。' if srt else '未找到字幕文件。')
    return {'passed':all(item['passed'] for item in checks),'checks':checks,'output_path':str(output),
            'duration':info['duration'],'dimensions':[info['display_width'],info['display_height']],
            'srt_path':srt,'server_qc':(record.get('result') or {}).get('qc'),
            'limitations':['此处验证入口、实际声音、解码、下载和锁稿；主观审美与嘴型仍需播放抽查。']+
            (['原有入口按素材顺序或文字相关度处理，本项通过不等于已具备新总监流程的画面语义策划。'] if mode!='talking_head' else [])}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data',type=Path,default=Path('/root/autodl-tmp/cutflow-studio/data'))
    parser.add_argument('--api',default='http://127.0.0.1:8765')
    parser.add_argument('--run-id',default='v011')
    parser.add_argument('--modes',nargs='+',choices=MODES,default=list(MODES))
    parser.add_argument('--mode-timeout',type=int,default=600)
    args=parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,50}',args.run_id):parser.error('Invalid run id')
    if not 30<=args.mode_timeout<=1200:parser.error('Mode timeout must be 30–1200 seconds')
    data=args.data.resolve();report_path=data/'director-publicmode-audit.json'
    context=load_server_environment(data)
    token=(data/'access-token.txt').read_text(encoding='utf-8').strip()
    api=API(args.api,token);del token
    _,config=api.request('/api/config');_,status=api.request('/api/status')
    code,library=api.request('/api/assets')
    if code!=200:raise RuntimeError('Material library could not be read')
    assets=library.get('assets') or []
    source=find_source(assets,2);extras=(find_source(assets,5),find_source(assets,6))
    _,catalog=api.request('/api/presenters')
    public=[p for p in catalog.get('presenters',[]) if p.get('kind')=='public' and p.get('ready') is True]
    portrait=public[0] if public else None
    capabilities=(config.get('production') or {}).get('modes') or {}
    voice=next((v['id'] for v in (config.get('production') or {}).get('voices',[]) if v['id']=='serena'),None)
    report={'started_at':now(),'version':status.get('version'),'environment':context,'run_id':args.run_id,
            'sources':{'video2':source,'video5':extras[0],'video6':extras[1]},'modes':{},
            'external_publication_performed':False,'scope':'实际旧入口任务、双提交去重、最终成片完整解码、下载与重新识别声音。'}
    try:
        prior=json.loads(report_path.read_text(encoding='utf-8'))
        if prior.get('run_id')==args.run_id:
            report['modes']={key:value for key,value in prior.get('modes',{}).items() if key not in args.modes}
            report['started_at']=prior.get('started_at',report['started_at'])
    except (OSError,ValueError):pass
    root=data/'publicmode-audit'/args.run_id;root.mkdir(parents=True,exist_ok=True)
    for mode in args.modes:
        result={'status':'checking','started_at':now(),'configured':capabilities.get(mode,{})};report['modes'][mode]=result
        atomic_json(report_path,report)
        blockers=[]
        if capabilities.get(mode,{}).get('ready') is not True:blockers.append(capabilities.get(mode,{}).get('reason') or '入口依赖未配置')
        if mode!='talking_head' and not source:blockers.append('未找到真实 video(2) 原素材')
        if mode in NARRATED and not voice:blockers.append('中文音色目录缺失')
        if mode=='talking_head' and portrait is None:blockers.append('没有已验证且有授权记录的公共人物')
        if blockers:
            result.update(status='blocked',blockers=blockers,finished_at=now());atomic_json(report_path,report)
            print(json.dumps({'mode':mode,'status':'blocked','blockers':blockers},ensure_ascii=False),flush=True);continue
        try:
            payload=make_payload(mode,source,extras,voice,portrait,args.run_id)
            atomic_json(root/(mode+'-request.json'),payload)
            first_code,first=api.request('/api/creations',payload)
            if first_code not in (200,201) or not first.get('job'):
                result.update(status='blocked',http_status=first_code,blockers=[first.get('detail','创建任务失败')]);continue
            identifier=first['job']['id'];result['job_id']=identifier
            second_code,second=api.request('/api/creations',payload)
            result['duplicate_submission']={'passed':second_code in (200,201) and second.get('job',{}).get('id')==identifier,
                                            'same_job_id':second.get('job',{}).get('id')==identifier,'http_status':second_code}
            atomic_json(report_path,report)
            deadline=time.monotonic()+args.mode_timeout;job=first['job']
            while time.monotonic()<deadline:
                code,detail=api.request('/api/jobs/'+identifier)
                if code!=200:raise RuntimeError('Could not restore task status')
                job=detail['job'];result.update(job_status=job['status'],stage=(job.get('production') or {}).get('stage'))
                atomic_json(report_path,report)
                if job['status'] in {'review','approved','published','failed','withdrawn'}:break
                time.sleep(3)
            if job['status'] not in {'review','approved','published'}:
                result.update(status='failed' if job['status']=='failed' else 'incomplete',
                              blockers=[job.get('error') or '未在限时内完成；保留任务，不再创建重复任务。']);continue
            work=root/mode;work.mkdir(exist_ok=True)
            validation=inspect_output(api,data,identifier,mode,work);result['validation']=validation
            result['status']='verified' if validation['passed'] and result['duplicate_submission']['passed'] else 'failed_validation'
        except Exception as error:
            result.update(status='failed_validation',blockers=[str(error)[:600]])
        finally:
            result['finished_at']=now();atomic_json(report_path,report)
            print(json.dumps({'mode':mode,'status':result['status'],'job_id':result.get('job_id'),
                              'blockers':result.get('blockers',[])},ensure_ascii=False),flush=True)
    report['finished_at']=now();report['all_verified']=all(x['status']=='verified' for x in report['modes'].values())
    atomic_json(report_path,report)
    print(json.dumps({'report':str(report_path),'all_verified':report['all_verified']},ensure_ascii=False),flush=True)
    return 0 if report['all_verified'] else 2


if __name__=='__main__':
    raise SystemExit(main())
