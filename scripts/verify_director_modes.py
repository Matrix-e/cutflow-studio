"""Real material audits for the director's narrated public modes; never publish."""
import argparse,json,os,time
from pathlib import Path
from uuid import uuid5,NAMESPACE_URL
from verify_public_modes import API,atomic_json,now
from verify_director_delivery import wait_for_production,verify_downloads

def main():
    p=argparse.ArgumentParser();p.add_argument('--data',type=Path,default=Path(os.environ.get('CUTFLOW_DATA','data')))
    p.add_argument('--inbox',type=Path,default=Path(os.environ.get('CUTFLOW_INBOX','inbox')))
    p.add_argument('--modes',nargs='+',choices=['story','narrated','vlog'],default=['story','narrated','vlog'])
    p.add_argument('--timeout',type=int,default=900);a=p.parse_args()
    api=API('http://127.0.0.1:8765',(a.data/'access-token.txt').read_text().strip())
    report={'started_at':now(),'external_publication':False,'modes':{}};out=a.data/'director-narrated-audit.json'
    goals={'story':'以这些真实外出片段为例，讲清独自去见客户时行程安排与现场等待的关系，做成有独白的工作记录。',
           'vlog':'保留现场原声，把这些打乱的片段整理成一次独自出发去见客户的 Vlog，让观众看懂出发、路途和现场进展。',
           'narrated':'把这些素材做成带独白的工作日短片，讲清这次独自外出的经历。'}
    for mode in a.modes:
        sources=[next(a.inbox.rglob('video('+str(i)+')--*.mp4')).relative_to(a.inbox).as_posix()
                 for i in ([6,3,2,8,5,7] if mode=='vlog' else [6,2,5])]
        ident=str(uuid5(NAMESPACE_URL,'cutflow-director-v011-real-'+mode))
        row={'id':ident,'status':'submitting'};report['modes'][mode]=row;atomic_json(out,report)
        try:
            body={'request_id':ident,'sources':sources,'mode':mode,'goal':goals[mode],
                  'options':{'target_duration':45 if mode=='vlog' else 30,'aspect':'9:16','music':True,'subtitles':True}}
            code,r=api.request('/api/productions',body);code2,r2=api.request('/api/productions',body)
            assert code==code2==200 and r['production']['id']==r2['production']['id']==ident
            row['duplicate_submission_same_task']=True
            def update(item):
                row.update(status=item['status'],stage=item['stage'],error=item.get('error'));atomic_json(out,report)
            item=wait_for_production(api,ident,a.timeout,update)
            if item['status']=='completed':
                row['delivery']=verify_downloads(api,item,a.data/'director-mode-downloads'/mode)
                row['theme']=item['content_plan']['theme'];row['quality']=item['quality']
                row['status']='verified' if row['delivery']['passed'] else 'failed'
        except Exception as exc:row.update(status='failed',error_type=type(exc).__name__,error=str(exc)[:400])
        atomic_json(out,report)
    report['finished_at']=now();report['all_verified']=all(x['status']=='verified' for x in report['modes'].values());atomic_json(out,report)
    print(json.dumps({'all_verified':report['all_verified'],'report':str(out)},ensure_ascii=False))

if __name__=='__main__':main()
