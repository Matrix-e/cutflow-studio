import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from studio import brand_context as brand


class BrandContextTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name).resolve()
    def tearDown(self):self.temp.cleanup()

    def test_default_rules_persist_intent_but_source_code_is_not_business_fact(self):
        data=brand.load_context(self.root)
        context=brand.model_context(self.root)
        self.assertEqual(data['revision'],0)
        self.assertIn('故事',''.join(context['creative_preferences']))
        self.assertIn('小程序',''.join(context['creative_preferences']))
        self.assertIn('Super Ecar',brand.confirmed_text(context))
        self.assertNotIn('续租',brand.confirmed_text(context))
        self.assertTrue(context['review_questions'])
        self.assertFalse((self.root/'brand-context.json').exists())

    def test_form_save_restarts_and_conflict_does_not_overwrite(self):
        payload={'facts':'托管合同逐项确认车辆交接责任。','advantages':'车辆交接记录可核对。',
                 'miniapp':'小程序名称为 Super Ecar。','culture':'','creative_rules':'先用真实经历开场。\n每条只解释一个问题。'}
        result=brand.update_from_form(self.root,payload,0)
        self.assertEqual(result['revision'],1)
        self.assertEqual(brand.load_context(self.root)['form'],payload)
        with self.assertRaises(brand.BrandContextConflict):brand.update_from_form(self.root,{},0)
        self.assertEqual(brand.load_context(self.root)['form'],payload)
        brand.update_from_form(self.root,{**payload,'advantages':'新的已确认优势。'},1)
        self.assertTrue((self.root/'brand-context-history/revision-1.json').is_file())
        self.assertEqual(brand.load_context(self.root)['revision'],2)

    def test_two_concurrent_writes_cannot_both_claim_same_revision(self):
        barrier=threading.Barrier(2);outcomes=[]
        def write(text):
            barrier.wait()
            try:brand.update_from_form(self.root,{'facts':text},0);outcomes.append('saved')
            except brand.BrandContextConflict:outcomes.append('conflict')
        threads=[threading.Thread(target=write,args=(text,)) for text in ['甲','乙']]
        for thread in threads:thread.start()
        for thread in threads:thread.join()
        self.assertCountEqual(outcomes,['saved','conflict'])
        self.assertEqual(brand.load_context(self.root)['revision'],1)

    def test_invalid_and_corrupt_context_are_explicit_errors(self):
        for payload in ({'unknown':'value'},{'facts':'x'*5001},{'facts':'x\0y'}):
            with self.assertRaises(brand.BrandContextError):brand.update_from_form(self.root,payload,0)
        (self.root/'brand-context.json').write_text('{bad',encoding='utf-8')
        with self.assertRaises(brand.BrandContextError):brand.model_context(self.root)

    def test_culture_is_persisted_and_old_clients_cannot_erase_it(self):
        value=brand.update_from_form(self.root,{'culture':'服务理念：把流程讲清楚，把记录留下来。','facts':'交接需核对车况。'},0)
        context=brand.model_context(self.root,query='介绍车辆交接')
        self.assertTrue(any(x['category']=='culture' for x in context['confirmed_facts']))
        brand.update_from_form(self.root,{'facts':'交接需核对车况。'},value['revision'])
        self.assertEqual(brand.load_context(self.root)['form']['culture'],'服务理念：把流程讲清楚，把记录留下来。')
        brand.update_from_form(self.root,{'culture':''},2)
        self.assertEqual(brand.load_context(self.root)['form']['culture'],'')

    def large_context(self):
        value=brand.default_context()
        value['facts']=[{'id':f'ordinary-{i}','category':'facts','text':(f'常规资料{i}：'+
            '页面采用清晰排版与文字说明。'*14).ljust(300,'。'),'source':'公开产品资料','status':'confirmed'} for i in range(12)]
        value['facts'] += [
            {'id':'identity','category':'miniapp','text':'小程序名称为 Super Ecar。','source':'品牌说明','status':'confirmed'},
            {'id':'rent-power','category':'facts','text':'续租电费为每月200元，订单明细中核对电费项目。','source':'已核验资料','status':'confirmed'},
            {'id':'unverified','category':'facts','text':'续租电费为每月999元，订单续租电费可退款。','source':'未核验备注','status':'needs_review'}]
        return value

    def test_query_recalls_tail_facts_under_budget_without_cutting_entries(self):
        value=self.large_context()
        baseline=brand.model_context(self.root,context=value)
        self.assertNotIn('每月200元',brand.confirmed_text(baseline))
        selected=brand.model_context(self.root,context=value,query='讲订单续租电费如何核对')
        text=brand.confirmed_text(selected)
        self.assertIn('每月200元',text)
        self.assertIn('小程序名称为 Super Ecar。',text)
        self.assertEqual(selected['brand_name'],'Super Ecar')
        self.assertNotIn('999元',text)
        self.assertTrue(any('999元' in item for item in selected['review_questions']))
        self.assertLessEqual(sum(len(item['text']) for item in selected['confirmed_facts']),1800)
        originals={(item['category'],item['text']) for item in value['facts'] if item['status']=='confirmed'}
        self.assertTrue(all((item['category'],item['text']) in originals for item in selected['confirmed_facts']))
        self.assertEqual(baseline,brand.model_context(self.root,context=value,query=''))
        self.assertEqual(baseline,brand.model_context(self.root,context=value,query=' \n '))
        self.assertEqual(selected,brand.model_context(self.root,context=value,query='讲订单续租电费如何核对'))

    def test_retrieved_facts_keep_stable_identity_and_source_scope(self):
        value=self.large_context()
        selected=brand.model_context(self.root,context=value,query='讲订单续租电费如何核对')
        originals={item['id']:item for item in value['facts']}
        for item in selected['confirmed_facts']:
            self.assertEqual(item,originals[item['id']])
            self.assertEqual(selected['fact_sources'][item['id']],item['source'])
        self.assertNotIn('unverified',selected['fact_sources'])
        repeated=brand.model_context(self.root,context=value,query='解释电费项目')
        self.assertEqual(next(item for item in repeated['confirmed_facts'] if item['id']=='rent-power')['source'],'已核验资料')

    def test_retrieval_ties_keep_storage_order_and_large_fact_is_not_truncated(self):
        value=brand.default_context()
        value['facts']=[{'id':str(i),'category':'facts','text':text,'source':'核验资料','status':'confirmed'}
            for i,text in enumerate(['续租电费'+('说明'*900),'续租电费记录甲。','续租电费记录乙。','其他资料。'])]
        selected=brand.model_context(self.root,context=value,query='续租电费')
        self.assertEqual([item['text'] for item in selected['confirmed_facts']],
                         ['续租电费记录甲。','续租电费记录乙。','其他资料。'])

    def test_nonempty_query_limits_facts_to_eight_but_empty_query_keeps_all_that_fit(self):
        value=brand.default_context()
        value['facts']=[{'id':str(i),'category':'facts','text':f'交接资料项目{i}。',
                        'source':'核验资料','status':'confirmed'} for i in range(15)]
        value['facts'].append({'id':'identity','category':'miniapp','text':'小程序名称为 Super Ecar。',
                               'source':'品牌说明','status':'confirmed'})
        selected=brand.model_context(self.root,context=value,query='交接资料')
        self.assertEqual(len(selected['confirmed_facts']),8)
        self.assertEqual(selected['confirmed_facts'][0]['text'],'小程序名称为 Super Ecar。')
        for query in (None,'',' \n '):
            unfiltered=brand.model_context(self.root,context=value,query=query)
            self.assertEqual([x['text'] for x in unfiltered['confirmed_facts']],
                             [x['text'] for x in value['facts']])

    def test_form_roundtrip_keeps_verified_ids_sources_and_review_isolation(self):
        value=brand.default_context()
        value['facts'] += [
            {'id':'code-order-01','category':'miniapp','text':'本地实现有订单续租入口。',
             'source':'frontend/order.vue:54 + backend/order.py:81','status':'confirmed'},
            {'id':'public-record-02','category':'facts','text':'交接时核对车辆资料。',
             'source':'产品知识库/交接说明:12','status':'confirmed'}]
        saved=brand.save_context(self.root,value,0)
        fields=brand.form_fields(saved)
        fields['facts']='新增用户资料。\n'+fields['facts']
        result=brand.update_from_form(self.root,fields,1)
        prior={(item['category'],item['text']):item for item in saved['facts']}
        for item in result['facts']:
            key=(item['category'],item['text'])
            if key in prior:self.assertEqual(item,prior[key])
        added=next(item for item in result['facts'] if item['text']=='新增用户资料。')
        self.assertEqual(added['source'],'用户保存的品牌资料')
        self.assertEqual(brand.load_context(self.root)['facts'],result['facts'])
        self.assertEqual([x for x in result['facts'] if x['status']=='needs_review'],
                         [x for x in saved['facts'] if x['status']=='needs_review'])

    def test_generation_and_validation_retrieve_the_same_brief_facts(self):
        from studio import idea_model,script_model
        from tests.test_idea_model import result_fixture
        brand.save_context(self.root,self.large_context(),0)
        brief='讲订单续租电费如何核对'
        with patch.dict(os.environ,{'CUTFLOW_DATA':str(self.root)}):
            for module in (idea_model,script_model):
                with patch.object(brand,'model_context',wraps=brand.model_context) as lookup:
                    request=idea_model._request(brief,[],'') if module is idea_model else script_model._request(brief)
                    module._messages(request)
                    self.assertEqual(lookup.call_args.kwargs['query'],brief)
                    module._messages(request)
                    self.assertEqual(lookup.call_count,1)
                self.assertIn('每月200元',brand.confirmed_text(request['brand_context']))
            idea=result_fixture();idea['cta']='了解每月200元的续租电费项目。'
            idea_model._validated_result(idea,idea_model._request(brief,[],''))
            script={'title':'核对续租电费','spoken_script':'办理续租时，先核对订单记录。续租电费为每月200元。具体项目请查看明细。','assumptions':[]}
            script_model.validate_result(script,script_model._request(brief))
            script['spoken_script']=script['spoken_script'].replace('200元','999元')
            with self.assertRaises(script_model.ScriptModelError):
                script_model.validate_result(script,script_model._request(brief))

    def test_story_retrieval_uses_brief_and_actual_speech_summaries(self):
        from studio import story
        class Planned(Exception):pass
        evidence={'speech_summary':'原声讨论续租电费核对。'}
        with patch.object(story,'_source_evidence',return_value=evidence),\
             patch('studio.brand_media.list_screenshots',return_value=[]),\
             patch.object(brand,'model_context',wraps=brand.model_context) as lookup,\
             patch.object(story,'run_process',side_effect=Planned),\
             patch.dict(os.environ,{'CUTFLOW_IDEA_PYTHON':'python'}),\
             self.assertRaises(Planned):
            story.build_story_plan(['input.mp4'],'解释一个有用的问题',{}, {},
                                   work_dir=self.root/'work',data=self.root)
        self.assertEqual(lookup.call_args.kwargs['query'],'解释一个有用的问题\n原声讨论续租电费核对。')

    def test_model_prompts_receive_saved_context_without_user_repeating_it(self):
        from studio import idea_model,script_model
        brand.update_from_form(self.root,{'facts':'交车前记录车辆外观。','creative_rules':'围绕一个具体问题编排故事。'},0)
        with patch.dict(os.environ,{'CUTFLOW_DATA':str(self.root)}):
            for messages in (idea_model._messages(idea_model._request('讲车辆托管',[],'')),
                             script_model._messages(script_model._request('讲车辆托管'))):
                payload=json.loads(messages[1]['content'])
                self.assertEqual(payload['brand_context']['revision'],1)
                self.assertIn('交车前',brand.confirmed_text(payload['brand_context']))
                self.assertIn('confirmed_facts',messages[0]['content'])

    def test_favorite_references_are_read_only_bounded_and_never_confirmed_facts(self):
        (self.root/'renders').mkdir();video=self.root/'renders/a.mp4';video.write_bytes(b'fake render for metadata fixture')
        stat=video.stat();snapshot={'spoken_script':'先提一个真实问题。再解释流程。欢迎了解。',
            'artifact_version':{'size_bytes':stat.st_size,'mtime_ns':stat.st_mtime_ns,'ctime_ns':stat.st_ctime_ns},'options':{'voice_style':'warm'}}
        digest=hashlib.sha256(json.dumps({'snapshot':snapshot,'job_fingerprint':'fp'},ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
        with sqlite3.connect(self.root/'studio.sqlite3') as db:
            db.execute('CREATE TABLE jobs(id TEXT,job_kind TEXT,status TEXT,result TEXT,output_path TEXT,fingerprint TEXT)')
            db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)',('a','production','review',json.dumps({'qc':{'passed':True}}),str(video),'fp'))
        db.close()
        with sqlite3.connect(self.root/'knowledge.sqlite3') as db:
            db.execute('CREATE TABLE knowledge(id TEXT,title TEXT,notes TEXT,learn_fields_json TEXT,snapshot_json TEXT,source_digest TEXT,revision INTEGER,favorite INTEGER,updated_at TEXT)')
            db.execute('INSERT INTO knowledge VALUES(?,?,?,?,?,?,?,?,?)',('a','优秀开场','短句，问题引入',json.dumps(['opening','notes']),json.dumps(snapshot),digest,1,1,'2026-11-04'))
        db.close()
        before={p:p.read_bytes() for p in self.root.glob('*.sqlite3')}
        context=brand.model_context(self.root)
        ref=context['favorite_references'][0]
        self.assertEqual(ref['opening'],'先提一个真实问题。')
        self.assertNotIn('structure',ref)
        self.assertFalse(ref['facts_verified'])
        self.assertNotIn('短句',brand.confirmed_text(context))
        self.assertTrue(all(p.read_bytes()==raw for p,raw in before.items()))
        video.write_bytes(b'changed')
        self.assertEqual(brand.model_context(self.root)['favorite_references'],[])

    def test_idea_claims_can_use_confirmed_brand_facts_but_not_review_notes(self):
        from studio import idea_model
        from tests.test_idea_model import result_fixture
        brand.update_from_form(self.root,{'facts':'车辆管理服务费为每月200元。'},0)
        with patch.dict(os.environ,{'CUTFLOW_DATA':str(self.root)}):
            value=result_fixture();value['cta']='了解每月200元的车辆管理服务费。'
            request=idea_model._request('介绍车辆管理的费用记录',[],'')
            idea_model._validated_result(value,request)
            value['cta']='了解每月999元的车辆管理服务费。'
            with self.assertRaises(idea_model.IdeaModelError):idea_model._validated_result(value,request)


if __name__=='__main__':unittest.main()
