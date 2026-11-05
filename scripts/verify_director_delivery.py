"""Audit real director downloads and one sentence revision on an idle GPU.

This script creates ONE version using a stable request UUID, never publishes,
never edits stored records and never retries a failed production automatically.
Run only after the chosen parent has actually completed. Bearer credentials stay
in memory. The output distinguishes technical verification from editorial review.
"""
from __future__ import annotations

import argparse
from difflib import SequenceMatcher
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
import time
import urllib.request
from uuid import UUID, uuid5, NAMESPACE_URL
import zipfile

from verify_public_modes import API, ROOT, atomic_json, fingerprint, normalize, now

KINDS=('video','subtitles','cover','copy','record','package')
EXTENSIONS={'video':'.mp4','subtitles':'.srt','cover':'.png','copy':'.txt','record':'.json','package':'.zip'}
INSTRUCTION='缩短到20秒，开头更直接，保留现场原声'


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),
                                     allow_nan=False).encode('utf-8')).hexdigest()


def immutable_snapshot(production):
    # Runtime timestamps/usage may settle immediately after delivery. Content,
    # source selection, quality decisions and artifact bytes must never change.
    fields=('id','status','goal','mode','title','sources','scope','options','parent_id',
            'revision','created_at','content_plan','quality','artifacts','brand_context_revision')
    return {key:production.get(key) for key in fields}


def get_production(api,identifier):
    code,body=api.request('/api/productions/'+identifier)
    if code!=200 or not isinstance(body.get('production'),dict):
        raise RuntimeError(f'Cannot read production {identifier}: HTTP {code}')
    return body['production']


def production_ids(api):
    code,body=api.request('/api/productions?limit=100')
    if code!=200:raise RuntimeError('Cannot read durable task list')
    return {item['id']:item for item in body.get('productions',[])}


def wait_for_production(api,identifier,timeout,on_update):
    deadline=time.monotonic()+timeout;previous=None
    while True:
        item=get_production(api,identifier)
        signature=(item.get('status'),item.get('stage'),item.get('attempts'),item.get('updated_at'))
        if signature!=previous:on_update(item);previous=signature
        if item['status'] in {'completed','failed','blocked'}:return item
        if time.monotonic()>=deadline:raise TimeoutError('Bounded wait expired; task was left intact')
        time.sleep(min(3,max(0,deadline-time.monotonic())))


def download(api,identifier,item,destination):
    expected=(f'/api/productions/{identifier}/video' if item['kind']=='video'
              else f'/api/productions/{identifier}/artifacts/{item["kind"]}')
    if item.get('url')!=expected:raise ValueError('Unexpected artifact endpoint')
    destination.parent.mkdir(parents=True,exist_ok=True)
    temporary=destination.with_suffix(destination.suffix+'.part')
    request=urllib.request.Request(api.base+expected,headers={'Authorization':'Bearer '+api.token})
    digest=hashlib.sha256();size=0;deadline=time.monotonic()+240
    try:
        with api.opener.open(request,timeout=30) as response,temporary.open('wb') as stream:
            for chunk in iter(lambda:response.read(1024*1024),b''):
                if time.monotonic()>deadline:raise TimeoutError('Artifact download exceeded four minutes')
                size+=len(chunk)
                if size>int(item['size']):raise RuntimeError('Download larger than committed artifact')
                digest.update(chunk);stream.write(chunk)
            result={'http_status':response.status,'content_type':response.headers.get('Content-Type'),
                    'size':size,'sha256':digest.hexdigest(),'file':str(destination)}
        if size!=item['size'] or result['sha256']!=item['sha256']:
            raise RuntimeError('Downloaded bytes do not match committed artifact')
        temporary.replace(destination)
        return result
    except Exception:
        # Keep the partial evidence for diagnosis; never touch the real artifact.
        raise


def read_captions(path):
    timing=re.compile(r'(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})')
    segments=[]
    for block in re.split(r'\r?\n\s*\r?\n',path.read_text(encoding='utf-8-sig').strip()):
        match=timing.search(block)
        if not match:raise ValueError('Subtitle entry has no readable timing')
        parts=[int(value) for value in match.groups()]
        start=parts[0]*3600+parts[1]*60+parts[2]+parts[3]/1000
        end=parts[4]*3600+parts[5]*60+parts[6]+parts[7]/1000
        text=block[match.end():].strip()
        if not text or end<=start or start<0:raise ValueError('Invalid subtitle timing or text')
        segments.append({'start':start,'end':end,'text':text})
    if not segments:raise ValueError('Subtitle artifact is empty')
    return segments


def verify_downloads(api,production,folder):
    from PIL import Image
    from studio import media
    artifacts={item['kind']:item for item in production.get('artifacts',[])}
    if set(artifacts)!=set(KINDS):raise RuntimeError('Completed production does not expose exactly six deliverables')
    folder.mkdir(parents=True,exist_ok=True)
    needed=sum(item['size'] for item in artifacts.values())
    if shutil.disk_usage(folder).free<needed*1.15+128*1024*1024:
        raise RuntimeError('Insufficient free space to retain the six downloaded deliverables')
    result={'files':{},'checks':[]}
    def check(name,passed,detail):
        result['checks'].append({'name':name,'passed':bool(passed),'detail':detail})
    for kind in KINDS:
        result['files'][kind]=download(api,production['id'],artifacts[kind],folder/(kind+EXTENSIONS[kind]))
    info=media.inspect_media(folder/'video.mp4')
    result['media']={key:info.get(key) for key in ('duration','width','height','video_codec','pixel_format','has_audio','audio_duration')}
    media._run([media._tool('ffmpeg'),'-v','error','-nostdin','-xerror',*media.INPUT_OPTIONS,
                '-i',str(folder/'video.mp4'),'-f','null','-'],max(120,min(600,info['duration']*5)))
    check('downloaded_video_full_decode',True,'从下载文件首帧至结束实际完整解码。')
    check('playback_format',info.get('video_codec')=='h264' and info.get('pixel_format')=='yuv420p' and info.get('has_audio'),result['media'])
    segments=read_captions(folder/'subtitles.srt')
    check('subtitle_timing',all(s['end']<=info['duration']+.25 for s in segments)
          and all(b['start']>=a['start'] for a,b in zip(segments,segments[1:])),
          {'entries':len(segments),'last_end':segments[-1]['end'],'video_duration':info['duration']})
    script=production['content_plan'].get('actual_script','')
    agreement=SequenceMatcher(None,normalize(script),normalize(''.join(s['text'] for s in segments)),autojunk=False).ratio()
    check('subtitle_matches_locked_script',agreement>=.9,{'normalized_text_agreement':round(agreement,4)})
    with Image.open(folder/'cover.png') as cover:
        dimensions=cover.size;cover.verify()
    check('cover_decodes',dimensions==(info['width'],info['height']),{'dimensions':list(dimensions)})
    copy=(folder/'copy.txt').read_text(encoding='utf-8-sig').strip()
    check('title_and_posting_copy',copy.splitlines()[0]==production['title'] and len(copy.splitlines())>=2,
          {'text':copy})
    record=json.loads((folder/'record.json').read_text(encoding='utf-8'))
    check('version_record',record.get('version_id')==production['id']
          and record.get('parent_id')==production.get('parent_id')
          and record.get('render_sha256')==artifacts['video']['sha256']
          and record.get('content_plan')==production['content_plan']
          and record.get('quality')==production['quality']
          and record.get('publication_performed') is False,
          {'version_id':record.get('version_id'),'parent_id':record.get('parent_id'),'publication_performed':record.get('publication_performed')})
    archive_checks=[]
    with zipfile.ZipFile(folder/'package.zip') as archive:
        expected={artifacts[k]['name']:artifacts[k]['sha256'] for k in KINDS if k!='package'}
        names=archive.namelist()
        if len(names)!=len(set(names)) or set(names)!=set(expected):
            raise RuntimeError('Delivery package entries do not match the five individual deliverables')
        for name in names:
            digest=hashlib.sha256()
            with archive.open(name) as stream:
                for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
            archive_checks.append({'name':name,'matched':digest.hexdigest()==expected[name]})
    check('package_matches_individual_downloads',all(item['matched'] for item in archive_checks),archive_checks)
    check('actual_output_quality_record',production.get('quality',{}).get('passed') is True,
          {'checks':production.get('quality',{}).get('checks'),'issues':production.get('quality',{}).get('issues')})
    result['passed']=all(item['passed'] for item in result['checks'])
    return result


def first_line(production):
    scenes=(production.get('content_plan') or {}).get('scenes') or []
    return str((scenes[0].get('spoken_text') or scenes[0].get('narration') or '') if scenes else '')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--production-id',required=True,type=UUID)
    parser.add_argument('--data',type=Path,default=Path('/root/autodl-tmp/cutflow-studio/data'))
    parser.add_argument('--api',default='http://127.0.0.1:8765')
    parser.add_argument('--token-file',type=Path)
    parser.add_argument('--run-id',default='v011')
    parser.add_argument('--timeout',type=int,default=1800,help='Bounded seconds per production (30–3600)')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    if not 30<=args.timeout<=3600:parser.error('--timeout must be between 30 and 3600')
    identifier=str(args.production_id)
    folder=args.data/'director-delivery-audit'/identifier
    report_path=args.output or folder/'audit.json'
    report={'started_at':now(),'parent_id':identifier,'instruction':INSTRUCTION,
            'status':'running','external_publication':False,'checks':[]}
    def save():atomic_json(report_path,report)
    def check(name,passed,detail):
        report['checks'].append({'name':name,'passed':bool(passed),'detail':detail});save()
    def update(which):
        def event(item):
            report[which+'_progress']={key:item.get(key) for key in ('id','status','stage','stage_label','attempts','elapsed_seconds','error')};save()
        return event
    save()
    try:
        token=(args.token_file or args.data/'access-token.txt').read_text(encoding='utf-8').strip()
        if not token:raise RuntimeError('Local service token file is empty')
        api=API(args.api,token)
        parent=wait_for_production(api,identifier,args.timeout,update('parent'))
        if parent['status']!='completed':
            report.update(status='blocked',blocker={'stage':'parent','status':parent['status'],'error':parent.get('error')});save();return 2
        snapshot=immutable_snapshot(parent);report['parent_snapshot_sha256']=canonical_hash(snapshot)
        atomic_json(folder/'parent-before.json',snapshot)
        report['parent_downloads']=verify_downloads(api,parent,folder/'parent');save()
        if not report['parent_downloads']['passed']:raise RuntimeError('Parent delivery failed verification; no revision submitted')
        before=production_ids(api)
        revision_id=str(uuid5(NAMESPACE_URL,f'cutflow:{args.run_id}:{identifier}:{INSTRUCTION}'))
        payload={'request_id':revision_id,'instruction':INSTRUCTION}
        report['revision_id']=revision_id;report['before_task_ids']=sorted(before);save()
        first_code,first=api.request(f'/api/productions/{identifier}/revise',payload)
        second_code,second=api.request(f'/api/productions/{identifier}/revise',payload)
        first=first.get('production') or {};second=second.get('production') or {}
        check('duplicate_revision_same_id',first_code==second_code==200 and first.get('id')==second.get('id')==revision_id,
              {'first_http':first_code,'second_http':second_code,'first_id':first.get('id'),'second_id':second.get('id')})
        if first_code!=200 or second_code!=200 or first.get('id')!=revision_id or second.get('id')!=revision_id:
            raise RuntimeError('Revision request was not accepted idempotently')
        after=production_ids(api)
        matching=[item['id'] for item in after.values() if item.get('parent_id')==identifier and item.get('revision')==INSTRUCTION]
        existing_matches=[item['id'] for item in before.values() if item.get('parent_id')==identifier and item.get('revision')==INSTRUCTION]
        check('duplicate_revision_no_additional_tasks',set(matching)==set(existing_matches)|{revision_id},
              {'before_matching':existing_matches,'after_matching':matching,'request_id':revision_id})
        child=wait_for_production(api,revision_id,args.timeout,update('revision'))
        if child['status']=='completed':
            report['revision_downloads']=verify_downloads(api,child,folder/'revision')
            duration=report['revision_downloads']['media']['duration']
            check('revision_applied_duration_and_original_audio',child.get('parent_id')==identifier
                  and child.get('mode')=='vlog' and child.get('options',{}).get('target_duration')==20
                  and duration<=25,{'mode':child.get('mode'),'target_duration':child.get('options',{}).get('target_duration'),
                  'actual_duration':duration,'natural_sentence_tolerance_seconds':5})
            final_code,replayed=api.request(f'/api/productions/{identifier}/revise',payload)
            replayed=replayed.get('production') or {}
            check('completed_revision_replay_no_work',final_code==200 and replayed.get('id')==revision_id
                  and replayed.get('status')=='completed' and replayed.get('attempts')==child.get('attempts')
                  and replayed.get('artifacts')==child.get('artifacts'),{'http_status':final_code,'attempts':replayed.get('attempts')})
            report['editorial_review']={'status':'actual_playback_review_required',
                'parent_opening':first_line(parent),'revision_opening':first_line(child),
                'opening_changed':normalize(first_line(parent))!=normalize(first_line(child)),
                'note':'文件与版本检查不能单独证明开头更直接；需结合这两个真实开头和实际播放核验。'}
        else:
            check('revision_completed',False,{'status':child['status'],'error':child.get('error')})
        final_parent=get_production(api,identifier)
        final_snapshot=immutable_snapshot(final_parent);atomic_json(folder/'parent-after.json',final_snapshot)
        check('parent_version_unchanged',final_snapshot==snapshot,
              {'before_sha256':canonical_hash(snapshot),'after_sha256':canonical_hash(final_snapshot)})
        # Re-download every parent's artifact by streaming hash after the child
        # finishes; comparing database metadata alone is not sufficient.
        parent_bytes=[]
        for item in final_parent.get('artifacts',[]):
            measured=api.download_hash(item['url'],timeout=240)
            parent_bytes.append({'kind':item['kind'],'unchanged':measured['sha256']==item['sha256']
                                 and measured['size']==item['size'],'sha256':measured['sha256']})
        check('parent_artifact_bytes_unchanged',len(parent_bytes)==6 and all(x['unchanged'] for x in parent_bytes),parent_bytes)
        verified=all(item['passed'] for item in report['checks']) and report.get('revision_downloads',{}).get('passed') is True
        report['status']='verified_technical_editorial_review_pending' if verified else 'failed'
        report['completed_at']=now();save()
        print(json.dumps({'status':report['status'],'report':str(report_path),'parent_id':identifier,
                          'revision_id':revision_id},ensure_ascii=False))
        return 0 if verified else 1
    except Exception as exc:
        # Credentials are never copied into the report or any request URL.
        report.update(status='failed',completed_at=now(),failure={'type':type(exc).__name__,'message':str(exc)[:1000]})
        save();print(json.dumps({'status':'failed','report':str(report_path),'error_type':type(exc).__name__},ensure_ascii=False))
        return 1


if __name__=='__main__':raise SystemExit(main())
