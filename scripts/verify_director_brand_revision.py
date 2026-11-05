"""Real GPU acceptance: local revision, cited knowledge, true screenshot and revision.

Uses normal API jobs and stable UUIDs, never resets retries or publishes. Every
claimed success is checked against downloaded, fully decoded delivery bytes.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from uuid import NAMESPACE_URL,uuid5

from verify_public_modes import API,atomic_json,load_server_environment,now
from verify_director_delivery import (get_production,wait_for_production,verify_downloads,
                                     immutable_snapshot,canonical_hash,production_ids)


PARENT='d51da663-0e16-5d3e-8496-c64ee667943e'
REWRITE='删掉“先确认目的明确”；把“进楼”改成“进小区”；结尾改成：安排拜访时，可以把路程、沟通和等待缓冲一起考虑。其他内容保持不变。'
BRAND_GOAL='做一条给潜在托管车主看的介绍短片，结合 Super Ecar 小程序，讲清楚办理前怎样了解托管规则，保留真实现场感。'
BRAND_EDIT='开头更直接，先点出托管前需要问清规则，其他段落保持不变。'


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data',type=Path,default=Path('/root/autodl-tmp/cutflow-studio/data'))
    p.add_argument('--run-id',default='v012-acceptance')
    p.add_argument('--timeout',type=int,default=1500)
    args=p.parse_args()
    load_server_environment(args.data)
    api=API('http://127.0.0.1:8765',(args.data/'access-token.txt').read_text().strip())
    out=args.data/'director-brand-revision-audit'/args.run_id;out.mkdir(parents=True,exist_ok=True)
    report={'started_at':now(),'status':'running','cases':{},'external_publication':False,'run_id':args.run_id}
    def save():atomic_json(out/'report.json',report)
    def identifier(case):return str(uuid5(NAMESPACE_URL,'cutflow:'+args.run_id+':'+case))
    def submit(case,path,body):
        body={**body,'request_id':identifier(case)}
        status,response=api.request(path,body)
        if status not in (200,201,202):raise RuntimeError(f'{case} rejected: {status}: {response}')
        item=response['production'];one_id=item['id']
        status,again=api.request(path,body)
        if status not in (200,201,202) or again['production']['id']!=one_id:raise RuntimeError('Repeated request created another job')
        report['cases'][case]={'id':one_id,'duplicate_same_id':True,'status':item['status']};save()
        def event(value):
            report['cases'][case].update({k:value.get(k) for k in ('status','stage','attempts','error')});save()
        return wait_for_production(api,one_id,args.timeout,event)
    def delivery(case,item):
        if item['status']!='completed':
            report['cases'][case]['production']=item;save();return False
        result=verify_downloads(api,item,out/case)
        report['cases'][case].update(production=item,downloads=result,verified=result['passed']);save()
        return result['passed']
    save()
    try:
        parent=get_production(api,PARENT)
        if parent['status']!='completed':raise RuntimeError('Known real source parent is not completed')
        before=immutable_snapshot(parent);atomic_json(out/'parent-before.json',before)
        revised=submit('local_revision',f'/api/productions/{PARENT}/revise',{'instruction':REWRITE})
        ok_revision=delivery('local_revision',revised)
        if ok_revision:
            plan=revised['content_plan'];script=plan['actual_script']
            report['cases']['local_revision']['instruction_applied']=('先确认目的明确' not in script and '进楼' not in script and
                '进小区' in script and '路程、沟通和等待缓冲一起考虑' in script)
            report['cases']['local_revision']['local_patch_record']=plan.get('revision_patch')
            report['cases']['local_revision']['revision_scope']=revised.get('revision_scope')
        branded=submit('brand_story','/api/productions',{'sources':parent['sources'],'mode':'story',
            'goal':BRAND_GOAL,'title':'','options':{'aspect':'9:16','target_duration':45,'subtitles':True,'music':True,'brand_usage':'required'}})
        ok_brand=delivery('brand_story',branded)
        if ok_brand:
            report['cases']['brand_story']['editorial_review']={
                'accepted_for_final_delivery':False,
                'issues':['初稿只泛提小程序，没有讲清具体入口和操作。','初稿“进了小区”应保留原声“接下来进小区”的计划状态。'],
                'action':'保留原版，通过正常修改接口重新策划知识表达，不篡改完成记录。'}
            refined=submit('brand_refined',f'/api/productions/{branded["id"]}/revise',{
                'instruction':'把知识讲具体：小程序该点哪里，咨询和申请怎么区分。保留真实行程，尚未进行的动作不要写成已经完成。精简到三十秒以内。',
                'options':{'target_duration':30}})
            if not delivery('brand_refined',refined):
                report.update(status='review_required',finished_at=now());save();return 0
            branded=refined
            brand_before=immutable_snapshot(branded)
            atomic_json(out/'brand-before.json',brand_before)
            plan=branded['content_plan'];used={ref for s in plan['scenes'] for ref in s.get('evidence_ids',[])}
            cited=[f for f in plan.get('brand_facts',[]) if f['id'] in used]
            screens=[s for s in plan['scenes'] if s.get('kind')=='brand_screenshot']
            report['cases']['brand_refined'].update(cited_facts=cited,screenshot_scenes=screens,
                brand_actually_used=bool(cited and screens))
            edit=submit('brand_revision',f'/api/productions/{branded["id"]}/revise',{'instruction':BRAND_EDIT})
            ok_edit=delivery('brand_revision',edit)
            if ok_edit:
                old=branded['content_plan']['scenes'];new=edit['content_plan']['scenes']
                report['cases']['brand_revision'].update(instruction_applied='托管' in new[0]['spoken_text'] and '规则' in new[0]['spoken_text'],
                    opening_changed=new[0]['spoken_text']!=old[0]['spoken_text'],
                    unaffected_scenes_unchanged=old[1:]==new[1:])
                old_images={s['brand_media_id']:s['brand_asset']['sha256'] for s in old if s.get('kind')=='brand_screenshot'}
                new_images={s['brand_media_id']:s['brand_asset']['sha256'] for s in new if s.get('kind')=='brand_screenshot'}
                report['cases']['brand_revision']['frozen_screenshots_unchanged']=old_images==new_images
            negative=submit('unsupported_revision',f'/api/productions/{branded["id"]}/revise',
                {'instruction':'把结尾改成：本次已经签约成功，保底收益一万元。其他内容保持不变。'})
            report['cases']['unsupported_revision'].update(production=negative,
                rejected_without_delivery=negative['status'] in ('blocked','failed') and not negative.get('artifacts'))
            current_brand=get_production(api,branded['id'])
            report['brand_parent_metadata_unchanged']=canonical_hash(immutable_snapshot(current_brand))==canonical_hash(brand_before)
            report['brand_parent_downloads']=verify_downloads(api,current_brand,out/'brand-parent-after')
            report['brand_parent_bytes_unchanged']=all(report['brand_parent_downloads']['files'][a['kind']]['sha256']==a['sha256'] for a in brand_before['artifacts'])
        current=get_production(api,PARENT)
        report['parent_metadata_unchanged']=canonical_hash(immutable_snapshot(current))==canonical_hash(before)
        report['parent_downloads']=verify_downloads(api,current,out/'parent-after')
        report['parent_bytes_unchanged']=all(report['parent_downloads']['files'][a['kind']]['sha256']==a['sha256'] for a in before['artifacts'])
        report['status']='review_required'
        report['finished_at']=now();save()
        print(json.dumps({'status':report['status'],'cases':{k:v.get('status') for k,v in report['cases'].items()}},ensure_ascii=False))
        return 0
    except Exception as exc:
        report.update(status='error',error=str(exc),finished_at=now());save();print(type(exc).__name__,str(exc));return 2


if __name__=='__main__':raise SystemExit(main())
