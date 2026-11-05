"""Real, isolated preflight fault injection; no changes to running configuration.

Default: read one real source, run its actual decode/font/encode checks, and
demonstrate that absent planning/vision/ASR/TTS dependencies are reported together.
An optional --material-gap task is submitted only when explicitly requested on
the command line. Neither path publishes or approves a work.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from uuid import NAMESPACE_URL, uuid5

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))

from verify_public_modes import API, atomic_json, find_source, load_server_environment


def now():return datetime.now(timezone.utc).isoformat()


def configuration_fingerprints(data):
    # Model-service configuration is inspected for non-mutation only. No key
    # file, token or credential content is included in the report.
    result={}
    for name in ('story-provider.json','processes.json'):
        path=data/name
        result[name]=hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    return result


def real_source(api,number):
    code,library=api.request('/api/assets')
    if code!=200:raise RuntimeError('无法读取当前真实素材库。')
    source=find_source(library.get('assets') or [],number)
    if source is None:raise RuntimeError(f'素材库中未找到 video({number})；未用测试图案替代。')
    code,config=api.request('/api/config')
    if code!=200:raise RuntimeError('无法读取许可素材目录。')
    inbox=Path(config['inbox']).resolve();path=(inbox/source).resolve()
    if not path.is_relative_to(inbox) or not path.is_file():raise RuntimeError('原素材无法安全读取。')
    return source,path


def verify_absent_dependencies(api,live_data,work_root):
    from studio.director_quality import preflight
    source,path=real_source(api,2)
    temporary=Path(tempfile.mkdtemp(prefix='isolated-',dir=work_root))
    isolated_data=temporary/'data';isolated_data.mkdir()
    before=configuration_fingerprints(live_data)
    prior_environment=dict(os.environ)
    try:
        missing=temporary/'intentionally-absent-models'
        # These are process-local environment variables. The service process and
        # its saved configuration continue to use their original settings.
        os.environ.update({
            'CUTFLOW_DATA':str(isolated_data),
            'CUTFLOW_CAPTION_PYTHON':str(missing/'asr-python'),
            'CUTFLOW_CAPTION_MODEL_DIR':str(missing/'asr-model'),
            'CUTFLOW_ASSET_WHISPER_MODEL':str(missing/'asr-model'),
            'CUTFLOW_IDEA_PYTHON':str(missing/'planner-python'),
            'CUTFLOW_IDEA_MODEL_DIR':str(missing/'planner-model'),
            'CUTFLOW_TTS_PYTHON':str(missing/'tts-python'),
            'CUTFLOW_TTS_MODEL_DIR':str(missing/'tts-model'),
        })
        report=preflight([path],'auto',{'aspect':'9:16','subtitles':True,'music':True,'target_duration':30},
                         data=isolated_data,work_dir=temporary,active=True)
    finally:
        os.environ.clear();os.environ.update(prior_environment)
    after=configuration_fingerprints(live_data)
    checks={item['id']:item for item in report['checks']}
    expected_blockers={'planning','vision','asr','voice'}
    actual_blockers={item['id'] for item in report.get('blockers',[])}
    real_media_checks={'sources','font','encoder','storage'}
    all_media_passed=all(checks.get(name,{}).get('status')=='passed' for name in real_media_checks)
    passed=(report.get('ready') is False and expected_blockers<=actual_blockers and all_media_passed and before==after)
    result={'status':'verified' if passed else 'failed_validation','source':source,
            'isolated_data':str(isolated_data),'actual_media_checks_passed':all_media_passed,
            'required_blockers':sorted(expected_blockers),'observed_blockers':sorted(actual_blockers),
            'live_configuration_unchanged':before==after,
            'provider_config_absent_in_isolation':not (isolated_data/'story-provider.json').exists(),
            'report':report,'finished_at':now(),
            'scope':'使用真实素材进行解码及字体编码检查；仅当前验收进程缺少模型，运行服务未被中断。'}
    atomic_json(temporary/'verification.json',result)
    return result


def verify_material_gap(api,run_id,timeout,report_path,report):
    source,_=real_source(api,6)
    identifier=str(uuid5(NAMESPACE_URL,'cutflow-director-material-gap:'+run_id+':'+source))
    payload={'request_id':identifier,'sources':[source],'mode':'auto',
             'goal':'仅使用这段素材，制作一个明确证明本次成交金额和已完成交付的客户成功案例。'
                    '金额与交付结果必须有真实画面、原声或已确认资料依据。不能把出发、等待或到达说成成交和交付。'
                    '若素材不能证明，就准确报告缺少什么，不得编造，也不得输出与此目标无关的成功案例。',
             'title':'素材不足验收 · 成交与交付证据',
             'options':{'aspect':'9:16','subtitles':True,'music':True,'target_duration':30}}
    result={'status':'submitting','request_id':identifier,'source':source,'started_at':now(),
            'expected':'对缺少成交金额与交付证据给出明确阻断，保留素材分析，不生成虚假成功案例。'}
    report['material_gap']=result;atomic_json(report_path,report)
    code,response=api.request('/api/productions',payload)
    if code not in (200,201) or not response.get('production'):
        result.update(status='failed_validation',http_status=code,detail=response.get('detail','任务未被接收'))
        return result
    production=response['production'];result['production_id']=production['id']
    replay_code,replay=api.request('/api/productions',payload)
    result['duplicate_submission_same_task']=replay_code in (200,201) and replay.get('production',{}).get('id')==production['id']
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        code,response=api.request('/api/productions/'+production['id'])
        if code!=200:raise RuntimeError('无法恢复素材不足验收的任务状态。')
        production=response['production'];result.update(production_status=production['status'],stage=production['stage'])
        atomic_json(report_path,report)
        if production['status'] in {'blocked','failed','completed'}:break
        time.sleep(3)
    error=production.get('error') or {}
    gap_codes={'material_shortfall','insufficient_evidence','evidence_shortfall','no_usable_material'}
    correct_gap=production['status']=='blocked' and error.get('code') in gap_codes
    result.update(status='verified' if correct_gap and result['duplicate_submission_same_task'] else 'failed_validation',
                  error=error,content_plan=production.get('content_plan'),artifacts=production.get('artifacts'),
                  finished_at=now(),production_status=production['status'])
    if not correct_gap:
        result['validation_note']='尚不能证明素材不足被准确处理；服务依赖阻断、一般错误或成片完成都不能当作本用例通过。'
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data',type=Path,default=Path('/root/autodl-tmp/cutflow-studio/data'))
    parser.add_argument('--api',default='http://127.0.0.1:8765')
    parser.add_argument('--output',type=Path)
    parser.add_argument('--run-id',default='v011')
    parser.add_argument('--material-gap',action='store_true',help='显式提交真实 video(6) 素材不足用例；GPU 空闲时使用')
    parser.add_argument('--only-material-gap',action='store_true',help='只运行显式素材不足用例，不重复依赖故障检查')
    parser.add_argument('--timeout',type=int,default=600)
    args=parser.parse_args()
    if args.only_material_gap and not args.material_gap:parser.error('--only-material-gap requires --material-gap')
    if not 30<=args.timeout<=1800:parser.error('--timeout must be 30–1800 seconds')
    data=args.data.resolve();output=args.output or data/'director-dependency-audit.json'
    context=load_server_environment(data)
    token=(data/'access-token.txt').read_text(encoding='utf-8').strip();api=API(args.api,token);del token
    work_root=data/'dependency-audit';work_root.mkdir(exist_ok=True)
    report={'started_at':now(),'environment':context,'external_publication_performed':False,
            'live_configuration_changed':False,'run_id':args.run_id}
    if args.only_material_gap and output.is_file():
        previous=json.loads(output.read_text(encoding='utf-8'))
        if previous.get('run_id')==args.run_id:report=previous
    if not args.only_material_gap:
        report['dependencies']=verify_absent_dependencies(api,data,work_root)
        report['live_configuration_changed']=not report['dependencies']['live_configuration_unchanged']
        atomic_json(output,report)
        print(json.dumps({'dependencies':report['dependencies']['status'],
                          'blockers':report['dependencies']['observed_blockers']},ensure_ascii=False),flush=True)
    if args.material_gap:
        report['material_gap']=verify_material_gap(api,args.run_id,args.timeout,output,report)
        atomic_json(output,report)
        print(json.dumps({'material_gap':report['material_gap']['status'],
                          'production_id':report['material_gap'].get('production_id')},ensure_ascii=False),flush=True)
    report['finished_at']=now();atomic_json(output,report)
    success=all(value.get('status')=='verified' for key,value in report.items() if key in {'dependencies','material_gap'})
    print(json.dumps({'report':str(output),'all_requested_checks_verified':success},ensure_ascii=False),flush=True)
    return 0 if success else 2


if __name__=='__main__':raise SystemExit(main())
