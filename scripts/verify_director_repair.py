"""Inject -30 dB into an isolated real film, perform ONE repair, rerun real QC.

Read-only access to production records and all original assets. Never updates a
live task, publishes, or replaces a delivered file. The operator must arrange an
idle GPU; this script deliberately does not acquire or alter the running worker.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
from uuid import UUID

from verify_public_modes import ROOT, atomic_json, fingerprint, load_server_environment, now


class Deadline:
    def __init__(self,seconds):self.deadline=time.monotonic()+seconds
    def is_set(self):return time.monotonic()>=self.deadline
    def wait(self,seconds=None):
        time.sleep(min(seconds or 0,max(0,self.deadline-time.monotonic())))
        return self.is_set()


def snapshot_record(data,identifier):
    database=(data/'productions.sqlite3').resolve()
    with sqlite3.connect(database.as_uri()+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        row=db.execute('SELECT * FROM productions WHERE id=?',(identifier,)).fetchone()
    if row is None or row['status']!='completed':raise RuntimeError('The chosen parent is not an actually completed production')
    raw=dict(row);record=dict(raw)
    for name in ('request','snapshot','checkpoints','plan','quality','artifacts'):
        record[name]=json.loads(record.pop(name+'_json'))
    if record['quality'].get('passed') is not True:raise RuntimeError('Parent actual quality record is not passed')
    return record,hashlib.sha256(json.dumps(raw,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def checkpoint(data,record,name):
    path=data/'productions'/record['id']/(name+'.json')
    expected=(record['checkpoints'].get(name) or {}).get('sha256')
    if not expected or not path.is_file() or fingerprint(path)!=expected:
        raise RuntimeError('Locked '+name+' checkpoint is missing or changed')
    return json.loads(path.read_text(encoding='utf-8'))


def source_files(record,inbox):
    result=[]
    for item in record['snapshot']['sources']:
        path=(inbox/item['source']).resolve()
        if not path.is_relative_to(inbox) or not path.is_file():raise RuntimeError('Source is outside the permitted inbox or unreadable')
        stat=path.stat()
        if any(item[key]!=value for key,value in (('size',stat.st_size),('mtime_ns',stat.st_mtime_ns),('ctime_ns',stat.st_ctime_ns))):
            raise RuntimeError('Original source snapshot has changed')
        result.append(path)
    return result


def artifact_files(data,record):
    root=(data/'productions'/record['id']).resolve();result={}
    for item in record['artifacts']:
        path=Path(item['path']).resolve()
        if not path.is_relative_to(root) or not path.is_file():raise RuntimeError('Artifact is not inside the parent production')
        if path.stat().st_size!=item['size'] or fingerprint(path)!=item['sha256']:
            raise RuntimeError('Original artifact does not match its committed hash')
        result[item['kind']]=path
    if not {'video','subtitles','cover','copy','record','package'}<=set(result):
        raise RuntimeError('Parent delivery is incomplete')
    return result


def video_packet_hash(path):
    from studio import media
    process=media._run([media._tool('ffmpeg'),'-v','error','-nostdin',*media.INPUT_OPTIONS,
                       '-i',str(path),'-map','0:v:0','-an','-c:v','copy','-f','hash','-hash','sha256','-'],180)
    text=process.stdout.strip()
    if not text.startswith('SHA256='):raise RuntimeError('Could not verify unchanged video packets')
    return text.split('=',1)[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--production-id',type=UUID,default=UUID('9f3e5140-8582-51a4-8280-9f0580043706'))
    parser.add_argument('--data',type=Path,default=Path('/root/autodl-tmp/cutflow-studio/data'))
    parser.add_argument('--inbox',type=Path,help='Optional permitted inbox; otherwise use the actual server environment')
    parser.add_argument('--output',type=Path,help='JSON report; default inside the independent audit directory')
    parser.add_argument('--timeout',type=int,default=1200,help='Overall cooperative deadline, 120–1800 seconds')
    args=parser.parse_args()
    if not 120<=args.timeout<=1800:parser.error('--timeout must be 120–1800 seconds')
    identifier=str(args.production_id);data=args.data.resolve()
    audit_root=data/'director-repair-audit';audit_root.mkdir(parents=True,exist_ok=True)
    work=Path(tempfile.mkdtemp(prefix=identifier+'-',dir=audit_root))
    report_path=args.output or work/'report.json'
    report={'started_at':now(),'production_id':identifier,'status':'running','work_dir':str(work),
            'defect':{'kind':'audio_volume','adjustment_db':-30},'repair_attempts':0,'max_repair_attempts':1,
            'live_task_modified':False,'external_publication':False,'checks':[]}
    originals={};record_before=None;deadline=Deadline(args.timeout)
    def save():atomic_json(report_path,report)
    def check(name,passed,detail):report['checks'].append({'name':name,'passed':bool(passed),'detail':detail});save()
    save()
    try:
        report['environment']=load_server_environment(data)
        import os
        from studio import media
        from studio.director_quality import verify_delivery,repair_delivery
        inbox=(args.inbox or Path(os.environ.get('CUTFLOW_INBOX',''))).resolve()
        if args.inbox is None and not os.environ.get('CUTFLOW_INBOX'):
            raise RuntimeError('Actual server environment did not supply the permitted inbox; specify --inbox explicitly')
        record,record_before=snapshot_record(data,identifier)
        plan=checkpoint(data,record,'content-plan')
        if plan!=record['plan']:raise RuntimeError('Persisted plan and locked checkpoint disagree')
        analysis=checkpoint(data,record,'analysis');render=checkpoint(data,record,'render-result')
        paths=source_files(record,inbox);artifacts=artifact_files(data,record)
        for i,path in enumerate(paths):originals['source:'+str(i)]={'path':str(path),'sha256':fingerprint(path)}
        for kind,path in artifacts.items():originals['artifact:'+kind]={'path':str(path),'sha256':fingerprint(path)}
        for name in ('content-plan','analysis','render-result','quality'):
            path=data/'productions'/identifier/(name+'.json')
            originals['checkpoint:'+name]={'path':str(path),'sha256':fingerprint(path)}
        for name in ('story-provider.json','processes.json'):
            path=data/name
            if path.is_file():originals['configuration:'+name]={'path':str(path),'sha256':fingerprint(path)}
        report['originals']=originals;report['parent_record_sha256']=record_before;save()
        if shutil.disk_usage(work).free<artifacts['video'].stat().st_size*4+256*1024*1024:
            raise RuntimeError('Not enough space for isolated original, defective and repaired films')
        original=work/'original-copy.mp4';shutil.copy2(artifacts['video'],original)
        srt=work/'locked.srt';shutil.copy2(artifacts['subtitles'],srt)
        check('independent_copy_matches_delivery',fingerprint(original)==originals['artifact:video']['sha256'],
              {'original_sha256':originals['artifact:video']['sha256'],'copy_sha256':fingerprint(original)})
        quiet=work/'quiet-minus-30db.mp4'
        media._run([media._tool('ffmpeg'),'-v','error','-nostdin','-y',*media.INPUT_OPTIONS,
                    '-i',str(original),'-map','0:v:0','-map','0:a:0','-c:v','copy','-af','volume=-30dB',
                    '-c:a','aac','-ar','48000','-b:a','192k','-movflags','+faststart',str(quiet)],180)
        original_picture=video_packet_hash(original);quiet_picture=video_packet_hash(quiet)
        check('defect_does_not_change_picture',original_picture==quiet_picture,{'video_packet_sha256':quiet_picture})
        local_render=copy.deepcopy(render);local_render.update(output_path=str(quiet),output_sha256=fingerprint(quiet),srt_path=str(srt))
        options=record['request']['options']
        def quality_plan(render_report):
            return {**copy.deepcopy(plan),'render_report':render_report,'srt_path':render_report['srt_path'],
                    'options':options,'source_snapshots':record['snapshot']['sources'],'material_evidence':analysis}
        if deadline.is_set():raise TimeoutError('Deadline exceeded before actual quality inspection')
        report['stage']='checking_injected_defect';save()
        before=verify_delivery(quiet,quality_plan(local_render),work_dir=work/'before',data=data,
                               render_result=local_render,paths=paths,options=options,stop_event=deadline)
        report['before_quality']=before;save()
        volume_issue=next((item for item in before.get('issues',[]) if item.get('code')=='audible_sound'),None)
        check('actual_qc_detects_low_volume',before.get('passed') is False and volume_issue is not None,
              {'issues':before.get('issues',[]),'audio_check':next((c for c in before.get('checks',[]) if c['id']=='audible_sound'),None)})
        if volume_issue is None:raise RuntimeError('Existing actual QC did not detect the intended low-volume defect; no repair was attempted')
        if deadline.is_set():raise TimeoutError('Deadline exceeded before the single allowed repair')
        report['repair_attempts']=1;report['stage']='one_local_repair';save()
        # Pass the complete real failed report. Never filter away ASR, visual or
        # timing errors just to force the repairer to accept this experiment.
        repaired=repair_delivery(quiet,quality_plan(local_render),before,work_dir=work/'repair',data=data,
                                 render_result=local_render,paths=paths,options=options,stop_event=deadline)
        if not repaired:
            report['status']='not_safely_repairable'
            report['reason']='The existing repairer declined the full set of detected defects; no additional attempt was made.'
            save();return 2
        fixed=Path(repaired['output_path']).resolve()
        if not fixed.is_relative_to(work) or not fixed.is_file() or fixed==quiet:
            raise RuntimeError('Repair did not produce an independent output inside the audit directory')
        report['repair_result']=repaired;save()
        if deadline.is_set():raise TimeoutError('Deadline exceeded before actual post-repair quality inspection')
        report['stage']='checking_actual_repaired_film';save()
        after=verify_delivery(fixed,quality_plan(repaired),work_dir=work/'after',data=data,
                              render_result=repaired,paths=paths,options=options,stop_event=deadline)
        report['after_quality']=after;report['repaired_video']={'path':str(fixed),'sha256':fingerprint(fixed)}
        check('repair_preserves_video_packets',video_packet_hash(fixed)==original_picture,
              {'expected_video_packet_sha256':original_picture,'actual_video_packet_sha256':video_packet_hash(fixed)})
        check('actual_repaired_film_passes_full_qc',after.get('passed') is True,
              {'checks':after.get('checks',[]),'issues':after.get('issues',[])})
        check('repair_was_bounded',report['repair_attempts']==1 and (repaired.get('repair') or {}).get('attempt')==1,
              repaired.get('repair'))
        report['status']='verified' if all(item['passed'] for item in report['checks']) else 'failed'
    except Exception as exc:
        report.update(status='failed',failure={'type':type(exc).__name__,'message':str(exc)[:1000]})
    finally:
        # This runs on success, refusal and error. Hashing originals proves that
        # this audit did not "repair" a historical delivery in place.
        try:
            unchanged=[]
            for name,item in originals.items():
                actual=fingerprint(Path(item['path']))
                unchanged.append({'item':name,'unchanged':actual==item['sha256'],'sha256':actual})
            if originals:check('original_sources_deliveries_and_config_unchanged',all(x['unchanged'] for x in unchanged),unchanged)
            if record_before is not None:
                _,record_after=snapshot_record(data,identifier)
                check('parent_durable_record_unchanged',record_before==record_after,
                      {'before_sha256':record_before,'after_sha256':record_after})
            if any(not item['passed'] for item in report['checks']):report['status']='failed'
        except Exception as exc:
            report.update(status='failed',integrity_error={'type':type(exc).__name__,'message':str(exc)[:1000]})
        report['finished_at']=now();save()
        print(json.dumps({'status':report['status'],'production_id':identifier,'repair_attempts':report['repair_attempts'],
                          'report':str(report_path),'work_dir':str(work)},ensure_ascii=False))
    return 0 if report['status']=='verified' else 1


if __name__=='__main__':raise SystemExit(main())
