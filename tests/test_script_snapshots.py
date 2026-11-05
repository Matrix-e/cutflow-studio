"""Frozen script evidence and the resident local-model generation branch."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from uuid import uuid4

from fastapi.testclient import TestClient
from studio import brand_context,script_model
from studio.local_story_provider import LocalStoryProviderError
from studio.scripts_service import ScriptStore
from studio.server import create_app


def snapshot():
    return {'revision':7,'brand_name':'Super Ecar','confirmed_facts':[
        {'category':'facts','text':'平台每月收取200元服务费。'},
        {'category':'culture','text':'我们的价值观是透明沟通。'}],
        'creative_preferences':[],'review_questions':[],'favorite_references':[]}


def draft():
    return {'title':'先把交接资料核对清楚','spoken_script':'交车前，资料都核对清楚了吗？平台每月收取200元服务费。具体项目请逐项核对。',
            'assumptions':[]}


class ScriptSnapshotAPITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.data=self.root/'data'
        self.provider=SimpleNamespace(configuration_status=lambda:{'configured':True,'model_exists':True,'python_exists':True},
                                      generate_script=Mock(return_value=draft()))
        self.app=create_app(data_dir=self.data,inbox=self.root/'inbox',token='script-freeze-test',
                            start_worker=False,script_provider=self.provider)
        self.client=TestClient(self.app);self.auth={'Authorization':'Bearer script-freeze-test'}
        self.worker=self.app.state.scripts
        brand_context.update_from_form(self.data,{'facts':'平台每月收取200元服务费。'},0)

    def tearDown(self):
        self.worker.stop();self.client.close();self.temp.cleanup()

    def create(self,identifier=None,**extra):
        return self.client.post('/api/scripts',headers=self.auth,json={
            'request_id':identifier or str(uuid4()),'mode':'story','brief':'解释服务费与资料核对，不杜撰企业文化。',**extra})

    def test_queue_freezes_generation_validation_and_public_digest_across_brand_changes(self):
        response=self.create();self.assertEqual(response.status_code,202,response.text)
        record=response.json()['script'];identifier=record['id']
        self.assertEqual(record['brand_context_revision'],1);self.assertIsNone(record['script_sha256'])
        frozen=self.worker.store.get(identifier)['resolved_context']
        brand_context.update_from_form(self.data,{'facts':'平台每月收取999元服务费。'},1)
        self.worker.process(self.worker.store.claim())
        generated=self.provider.generate_script.call_args.kwargs['brand_context']
        self.assertEqual(generated,frozen['brand_context'])
        self.assertIn('200元',brand_context.confirmed_text(generated))
        result=self.client.get('/api/scripts/'+identifier,headers=self.auth).json()['script']
        self.assertEqual(result['status'],'succeeded')
        self.assertEqual(result['script_sha256'],hashlib.sha256(draft()['spoken_script'].encode()).hexdigest())
        self.assertEqual(result['result']['script_sha256'],result['script_sha256'])
        self.assertEqual(result['brand_context_revision'],1)
        self.assertEqual(result['result']['brand_context_revision'],1)
        self.assertNotIn('resolved_context',result)
        restarted=ScriptStore(self.data/'scripts.sqlite3')
        self.assertEqual(restarted.get(identifier)['resolved_context'],frozen)
        self.assertEqual(self.create(identifier).json()['script']['script_sha256'],result['script_sha256'])

    def test_provider_cannot_mutate_snapshot_to_authorize_new_facts(self):
        response=self.create();identifier=response.json()['script']['id']
        def mutate(**values):
            values['brand_context']['confirmed_facts']=[{'text':'平台每月收取999元服务费。'}]
            return {**draft(),'spoken_script':draft()['spoken_script'].replace('200元','999元')}
        self.provider.generate_script.side_effect=mutate
        self.worker.process(self.worker.store.claim())
        result=self.worker.store.get(identifier)
        self.assertEqual(result['status'],'failed');self.assertEqual(result['error_code'],'invalid_output')
        self.assertIn('200元',brand_context.confirmed_text(result['resolved_context']['brand_context']))

    def test_retry_keeps_original_snapshot_and_client_cannot_inject_one(self):
        response=self.create();identifier=response.json()['script']['id']
        self.provider.generate_script.side_effect=RuntimeError('unavailable')
        self.worker.process(self.worker.store.claim())
        brand_context.update_from_form(self.data,{'facts':'平台每月收取999元服务费。'},1)
        self.provider.generate_script.side_effect=None
        self.client.post('/api/scripts/'+identifier+'/retry',headers=self.auth)
        self.worker.process(self.worker.store.claim())
        self.assertEqual(self.worker.store.get(identifier)['status'],'succeeded')
        self.assertEqual(self.worker.store.get(identifier)['brand_context_revision'],1)
        self.assertEqual(self.create(brand_context=snapshot()).status_code,422)


class ScriptResidentProviderTests(unittest.TestCase):
    def request(self):
        return script_model._request('解释企业文化和服务费，缺失内容不杜撰。',mode='story',brand_context=snapshot())

    def tokenizer(self,size=500):
        return SimpleNamespace(apply_chat_template=lambda messages,**kwargs:json.dumps(messages,ensure_ascii=False),
                               encode=lambda prompt,**kwargs:[0]*size)

    def test_request_and_validation_keep_snapshot_and_compute_verified_metadata(self):
        original=snapshot();request=script_model._request('解释服务费',mode='story',brand_context=original)
        original['revision']=99;original['confirmed_facts'].clear()
        with patch.object(brand_context,'model_context',side_effect=AssertionError('must not reload')):
            script_model._messages(request)
            result=script_model.validate_result(draft(),request)
            self.assertEqual(script_model.validate_result(result,request),result)
        self.assertEqual(result['brand_context_revision'],7)
        for extra in ({'script_sha256':'0'*64},{'brand_context_revision':8}):
            with self.assertRaises(script_model.ScriptModelError):
                script_model.validate_result({**result,**extra},request)

    def test_story_limits_length_and_rejects_invented_culture_or_advantages(self):
        request=self.request()
        for spoken in ('我们的使命是让天下没有难做的汽车生意。',
                       '我们的核心价值观是诚信专业、客户第一。',
                       '平台支持随时查询车辆状态，让每次交接都更放心。','核对资料。'*37):
            with self.subTest(spoken=spoken),self.assertRaises(script_model.ScriptModelError):
                script_model.validate_result({**draft(),'spoken_script':spoken},request)
        accepted=script_model.validate_result({**draft(),'spoken_script':'交接之前，问题都问清楚了吗？我们的价值观是透明沟通。'},request)
        self.assertIn('透明沟通',accepted['spoken_script'])

    def test_http_provider_all_modes_skip_local_weights_and_gpu_memory_check(self):
        for mode in script_model.MODES:
            request=script_model._request('说明服务费',mode=mode,brand_context=snapshot())
            provider=Mock(return_value=json.dumps(draft(),ensure_ascii=False));weights=Mock();torch=Mock()
            module=SimpleNamespace(AutoTokenizer=SimpleNamespace(from_pretrained=Mock(return_value=self.tokenizer())),
                                   AutoModelForCausalLM=weights)
            with patch('studio.local_story_provider.get_provider',return_value=provider),\
                 patch.dict(sys.modules,{'transformers':module,'torch':torch}):
                result=script_model._worker_generate(request)
            self.assertEqual(result['spoken_script'],draft()['spoken_script'])
            provider.assert_called_once();weights.from_pretrained.assert_not_called()
            torch.cuda.mem_get_info.assert_not_called();torch.cuda.is_available.assert_not_called()
            payload=json.loads(provider.call_args.args[0][1]['content'])
            if mode=='story':
                self.assertEqual(payload['brand_context']['revision'],7)
                self.assertNotIn('confirmed_facts',payload['brand_context'])
                self.assertEqual(payload['brand_context']['confirmed_fact_sentences'],
                                 {'F0':'平台每月收取200元服务费。','F1':'我们的价值观是透明沟通。'})
            else:self.assertEqual(payload['brand_context'],snapshot())

    def test_http_size_and_provider_errors_fail_without_fallback_or_raw_error(self):
        for oversized in (False,True):
            provider=Mock(side_effect=LocalStoryProviderError('private token must not escape'))
            weights=Mock();module=SimpleNamespace(AutoTokenizer=SimpleNamespace(from_pretrained=lambda *a,**k:self.tokenizer(6145 if oversized else 500)),
                                                  AutoModelForCausalLM=weights)
            with patch('studio.local_story_provider.get_provider',return_value=provider),\
                 patch.dict(sys.modules,{'transformers':module}),self.assertRaises(script_model.ScriptModelError) as caught:
                script_model._worker_generate(self.request())
            self.assertEqual(caught.exception.code,'resource_limit' if oversized else 'generation_failed')
            self.assertNotIn('private',str(caught.exception));weights.from_pretrained.assert_not_called()
            self.assertEqual(provider.call_count,0 if oversized else 1)

    def test_http_quality_rewrite_still_has_only_two_attempts(self):
        provider=Mock(return_value='not json')
        module=SimpleNamespace(AutoTokenizer=SimpleNamespace(from_pretrained=lambda *a,**k:self.tokenizer()))
        with patch('studio.local_story_provider.get_provider',return_value=provider),\
             patch.dict(sys.modules,{'transformers':module}),self.assertRaises(script_model.ScriptModelError):
            script_model._worker_generate(self.request())
        self.assertEqual(provider.call_count,2)

    def test_typed_script_expands_only_frozen_facts_and_keeps_api_contract(self):
        request=self.request();original=copy.deepcopy(request)
        value={'title':'把交接费用问清楚','narration':[{'text':'交接之前，费用都问清楚了吗？'},
               {'fact_ref':'F0'},{'text':'把具体项目逐项核对清楚。'}],'assumptions':[]}
        result=script_model._parse(json.dumps(value,ensure_ascii=False),request)
        self.assertEqual(result['spoken_script'],'交接之前，费用都问清楚了吗？平台每月收取200元服务费。把具体项目逐项核对清楚。')
        self.assertEqual(result['script_sha256'],hashlib.sha256(result['spoken_script'].encode()).hexdigest())
        self.assertNotIn('narration',result)
        self.assertEqual(request,original)

    def test_typed_script_rejects_bad_fragments_and_never_bypasses_grounding(self):
        request=self.request()
        base={'title':'把交接费用问清楚','narration':[{'fact_ref':'F0'}],'assumptions':[]}
        for fragments in ([{'fact_ref':'F99'}],[{'fact_ref':0}],[{'fact_ref':'F00'}],
                          [{'fact_ref':'F0','text':'多加事实。'}],[{'text':'没有句末标点'}],
                          [{'text':'[[FACT:0]]。'}],[],{'fact_ref':'F0'}):
            with self.subTest(fragments=fragments),self.assertRaises(script_model.ScriptModelError):
                script_model._parse(json.dumps({**base,'narration':fragments},ensure_ascii=False),request)
        for extra in ({'spoken_script':'另一份稿。'},{'unknown':True}):
            with self.assertRaises(script_model.ScriptModelError):
                script_model._parse(json.dumps({**base,**extra},ensure_ascii=False),request)
        for text in ('平台可以随时查询车辆位置。','我们的愿景是改变全球汽车出行。'):
            value={**base,'narration':[{'fact_ref':'F0'},{'text':text}]}
            with self.subTest(text=text),self.assertRaises(script_model.ScriptModelError):
                script_model._parse(json.dumps(value,ensure_ascii=False),request)
        value={**base,'narration':[{'fact_ref':'F0'}]*11+[{'text':'核对资料。'*8}]}
        with self.assertRaises(script_model.ScriptModelError):
            script_model._parse(json.dumps(value,ensure_ascii=False),request)

    def test_story_rewrite_returns_typed_facts_without_reintroducing_failed_draft(self):
        request=self.request();calls=[]
        bad={**draft(),'spoken_script':'平台收取每月200元管理费，让整个托管过程都更安心。'}
        valid={'title':'核对交接费用','narration':[{'text':'交车前，费用都问清楚了吗？'},
               {'fact_ref':'F0'},{'text':'再把具体项目逐项核对。'}],'assumptions':[]}
        def generate(messages):
            calls.append(copy.deepcopy(messages))
            return json.dumps(bad if len(calls)==1 else valid,ensure_ascii=False)
        result=script_model._generate_checked(request,generate)
        self.assertEqual(len(calls),2)
        self.assertTrue(all(message['role']!='assistant' for message in calls[1]))
        feedback=json.loads(calls[1][-1]['content'])
        self.assertIn('更安心',feedback['failed_sentence'])
        self.assertEqual(feedback['valid_fact_refs'],['F0','F1'])
        self.assertNotIn('更安心',result['spoken_script'])
        self.assertIn('平台每月收取200元服务费。',result['spoken_script'])

    def test_duplicate_live_fact_text_reports_expanded_length_and_repairs_structure(self):
        facts=['当前申请页面要求车辆照片、行驶证、年检标志、交强险保单和商业险保单，并需完善表单要求的基本信息。',
               '请通过小程序的正式托管申请页面提交材料，不要在通用问答中发送证件、保单或个人资料。']
        context={**snapshot(),'confirmed_facts':[{'category':'facts','text':text} for text in facts]}
        request=script_model._request('解释托管申请材料与提交入口',mode='story',brand_context=context)
        bad={'title':'托管前把资料备齐','narration':[{'text':'准备托管车辆，资料都备齐了吗？'},
            {'fact_ref':'F0'},{'text':facts[0]},{'fact_ref':'F1'},{'text':facts[1]},
            {'text':'核对清单无误，再提交审核。'}],'assumptions':[]}
        good={**bad,'narration':[bad['narration'][0],{'fact_ref':'F0'},{'fact_ref':'F1'},bad['narration'][-1]]}
        calls=[]
        def generate(messages):
            calls.append(copy.deepcopy(messages))
            return json.dumps(bad if len(calls)==1 else good,ensure_ascii=False)
        result=script_model._generate_checked(request,generate)
        self.assertLessEqual(len(result['spoken_script']),180)
        self.assertTrue(all(result['spoken_script'].count(fact)==1 for fact in facts))
        feedback=json.loads(calls[1][-1]['content'])
        self.assertGreater(feedback['expanded_chars'],180)
        self.assertEqual(feedback['fragment_count'],6)
        self.assertEqual(feedback['duplicate_text_fragments'],[{'position':2,'fact_ref':'F0'},{'position':4,'fact_ref':'F1'}])
        payload=json.loads(calls[0][1]['content'])
        self.assertEqual(payload['brand_context']['confirmed_fact_characters'],{'F0':len(facts[0]),'F1':len(facts[1])})
        self.assertEqual(payload['script_budget']['max_chars'],180)

    def test_story_wire_rejects_middle_bridges_and_repeated_refs_even_under_budget(self):
        request=self.request()
        for fragments in ([{'text':'资料核对清楚了吗？'},{'fact_ref':'F0'},{'text':'再核对细节。'},
                           {'fact_ref':'F1'},{'text':'逐项确认就好。'}],
                          [{'text':'资料核对清楚了吗？'},{'fact_ref':'F0'},{'fact_ref':'F0'},{'text':'逐项确认就好。'}]):
            value={'title':'把资料核对清楚','narration':fragments,'assumptions':[]}
            with self.assertRaises(script_model.ScriptModelError) as caught:
                script_model._parse(json.dumps(value,ensure_ascii=False),request)
            self.assertIn(caught.exception.diagnostic['rule'],('narration_layout','repeated_fact'))

    def test_fixed_story_wire_expands_only_selected_facts_and_prompts_one_schema(self):
        request=self.request()
        wire={'title':'把交接费用问清楚','opening':'交接之前，费用都问清楚了吗？',
              'fact_refs':['F0'],'ending':'把具体项目逐项核对清楚。','assumptions':[]}
        result=script_model._parse(json.dumps(wire,ensure_ascii=False),request)
        self.assertEqual(result['spoken_script'],'交接之前，费用都问清楚了吗？平台每月收取200元服务费。把具体项目逐项核对清楚。')
        messages=script_model._messages(request)
        example=json.loads(messages[0]['content'].split('结构示例：')[-1])
        self.assertEqual(set(example),{'title','opening','fact_refs','ending','assumptions'})
        self.assertEqual(json.loads(messages[1]['content'])['script_budget']['max_expanded_chars'],180)
        no_facts=script_model._request('提醒观众逐项核对资料',mode='story',brand_context={**snapshot(),'confirmed_facts':[]})
        self.assertIn('逐项核对',script_model._parse(json.dumps({**wire,'fact_refs':[]},ensure_ascii=False),no_facts)['spoken_script'])

    def test_fixed_story_wire_rejects_mixed_fields_bad_refs_and_unsupported_closing(self):
        request=self.request()
        wire={'title':'把交接费用问清楚','opening':'交车前，费用问清楚了吗？',
              'fact_refs':['F0'],'ending':'把具体项目逐项核对清楚。','assumptions':[]}
        for extra in ({'narration':[]},{'spoken_script':'另写一份稿。'},{'fact_refs':['F0','F0']},
                      {'fact_refs':['F99']},{'fact_refs':'F0'},{'fact_refs':[]},
                      {'opening':'没有问题句。'},{'ending':'平台可以随时查询车辆位置。'},
                      {'opening':'资料'*20+'？'}):
            with self.subTest(extra=extra),self.assertRaises(script_model.ScriptModelError):
                script_model._parse(json.dumps({**wire,**extra},ensure_ascii=False),request)

    def test_story_candidates_exclude_implementation_evidence_without_renumbering(self):
        facts=[{'text':'本地版本已核验。平台支持查看订单。'},
               {'text':'平台每月收取200元服务费。'},
               {'text':'本机源码显示前后端检查必填项。'},
               {'text':'我们的价值观是透明沟通。'},
               {'text':'源代码存在问答入口，但尚未验收。'}]
        request=script_model._request('解释托管流程',mode='story',brand_context={**snapshot(),'confirmed_facts':facts})
        original=copy.deepcopy(request)
        payload=json.loads(script_model._messages(request)[1]['content'])
        self.assertEqual(payload['brand_context']['confirmed_fact_sentences'],
                         {'F2':'平台每月收取200元服务费。','F4':'我们的价值观是透明沟通。'})
        self.assertEqual(payload['brand_context']['confirmed_fact_characters'],{'F2':14,'F4':12})
        self.assertNotIn('本地版本',json.dumps(payload,ensure_ascii=False))
        self.assertNotIn('查看订单',json.dumps(payload,ensure_ascii=False))
        self.assertEqual(request,original)
        wire={'title':'核对服务费用','opening':'交车前，费用问清楚了吗？','fact_refs':['F2'],
              'ending':'把具体项目逐项核对清楚。','assumptions':[]}
        self.assertIn('200元',script_model._parse(json.dumps(wire,ensure_ascii=False),request)['spoken_script'])
        for ref in ('F0','F1','F3','F5'):
            with self.subTest(ref=ref),self.assertRaises(script_model.ScriptModelError):
                script_model._parse(json.dumps({**wire,'fact_refs':[ref]},ensure_ascii=False),request)
        calls=[]
        def generate(messages):
            calls.append(copy.deepcopy(messages))
            return json.dumps({**wire,'fact_refs':['F0']} if len(calls)==1 else wire,ensure_ascii=False)
        script_model._generate_checked(request,generate)
        self.assertEqual(json.loads(calls[1][-1]['content'])['valid_fact_refs'],['F2','F4'])
        # Excluding a candidate does not silently edit a user-authored script.
        manual={**draft(),'spoken_script':'核对本地版本时，要检查源代码。平台每月收取200元服务费。'}
        self.assertEqual(script_model.validate_result(manual,request)['spoken_script'],manual['spoken_script'])


if __name__=='__main__':unittest.main()
