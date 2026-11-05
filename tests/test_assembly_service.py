import json
from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
from uuid import uuid4

from fastapi.testclient import TestClient
from fastapi import HTTPException
from studio.assembly_service import AssemblyService,script_hash
from studio.server import create_app


class AssemblyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        with patch('studio.server.detect_gpu',return_value={'available':False}):
            self.app=create_app(data_dir=self.root/'data',inbox=self.root/'inbox',token='test',start_worker=False)
        self.client=TestClient(self.app);self.auth={'Authorization':'Bearer test'}
        self.source=self.root/'inbox'/'交接.mp4';self.source.write_bytes(b'fixture source')
        self.service=self.app.state.assemblies
        self.caps=patch('studio.server.production_capabilities',return_value={'modes':{'story':{'ready':True}}})
        self.caps.start()
        self.body={'request_id':str(uuid4()),'spoken_script':'交车前，先核对车况。记录清楚，再开始旅程。',
            'brief':'介绍交接记录','sources':[],'options':{'subtitles':False},'auto_render':False}

    def tearDown(self):
        self.caps.stop();self.client.close();self.app.state.previews.stop();self.temp.cleanup()

    def post(self,**changes):
        return self.client.post('/api/assembly-plans',headers=self.auth,json={**self.body,**changes})

    def finish(self,record):
        def build(paths,script,options,settings,**kw):
            self.assertEqual(options['subtitles'],False)
            kw['on_stage']('planning_visuals')
            return {'kind':'narrated_composition','spoken_script':script,'assembly':{'version':1,'script':script,
                'script_sha256':script_hash(script),'summary':'按原声与画面质量编排','stats':{'selected_shots':1},
                'shots':[{'source_index':0,'source_in':2,'source_out':7,'timeline_start':0,'timeline_end':5,'frames':150,
                          'narration':script,'selection_basis':'原声匹配','score':85}]}}
        self.service.builder=build;self.service.process(record)
        return self.service.get(record['id'])

    def test_auto_library_snapshot_idempotent_and_durable(self):
        response=self.post();self.assertEqual(response.status_code,202,response.text)
        item=response.json()['assembly'];self.assertEqual(item['status'],'queued')
        self.assertNotIn('snapshot',item)
        self.source.write_bytes(b'changed after request')
        retry=self.post();self.assertEqual(retry.json()['assembly']['id'],item['id'])
        self.assertEqual(self.post(spoken_script='另一篇文字。').status_code,409)
        self.service.process(self.service.get(item['id']))
        self.assertEqual(self.service.get(item['id'])['status'],'failed')
        self.assertIn('素材',self.service.get(item['id'])['error'])

    def test_old_browser_request_survives_optional_title_field_upgrade(self):
        record=self.service.get(self.post().json()['assembly']['id'])
        old=dict(record['request']);old.pop('title',None)
        with closing(self.service.connect()) as db:
            from studio.assembly_service import _json
            db.execute('UPDATE assemblies SET request_json=?,fingerprint=? WHERE id=?',
                (_json(old),script_hash(_json(old)),record['id']));db.commit()
        self.assertEqual(self.post().json()['assembly']['id'],record['id'])
        self.assertEqual(self.post(title='不同标题').status_code,409)

    def test_plan_has_readable_shots_and_final_render_keeps_script(self):
        record=self.service.get(self.post().json()['assembly']['id'])
        result=self.finish(record);self.assertEqual(result['status'],'ready',result.get('error'))
        public=result['result'];self.assertEqual(public['sources'],[self.source.name])
        self.assertEqual(public['shots'][0]['source_in'],2)
        self.assertTrue(public['stats']['automatic_library'])
        req={'mode':'story','sources':public['sources'],'brief':self.body['brief'],
             'spoken_script':self.body['spoken_script'],'expected_script_sha256':public['script_sha256'],
             'assembly_plan_id':record['id'],'options':self.body['options'],'idempotency_key':'final-test'}
        response=self.client.post('/api/creations',headers=self.auth,json=req)
        self.assertEqual(response.status_code,201,response.text)
        job=self.app.state.store.get(response.json()['job']['id'])
        self.assertEqual(job['settings']['creation']['spoken_script'],self.body['spoken_script'])
        self.assertEqual(job['settings']['creation']['assembly_plan_id'],record['id'])
        self.assertFalse(job['settings']['creation']['options']['subtitles'])
        self.assertEqual(self.client.post('/api/creations',headers=self.auth,json=req).json()['job']['id'],job['id'])
        for changes in ({'spoken_script':'偷偷改写。'},{'options':{'subtitles':True}},{'brief':'新角度'}):
            self.assertEqual(self.client.post('/api/creations',headers=self.auth,json={**req,**changes}).status_code,409)

    def test_auto_render_is_server_side_and_crash_handoff_is_idempotent(self):
        record=self.service.get(self.post(auto_render=True,title='品牌精剪样片').json()['assembly']['id'])
        done=self.finish(record)
        self.assertIsNotNone(done['job_id'],done.get('error'))
        job_id=done['job_id']
        self.assertEqual(self.app.state.store.get(job_id)['title'],'品牌精剪样片')
        # Simulate a crash after enqueueing and before recording the job ID.
        self.service.update(record['id'],job_id=None)
        reopened=AssemblyService(self.root/'data',self.root/'inbox',validate_source=self.service.validate_source,
            source_allowed=self.service.source_allowed,enqueue_render=self.service.enqueue_render)
        reopened.handoff(reopened.get(record['id']))
        self.assertEqual(reopened.get(record['id'])['job_id'],job_id)
        self.assertEqual(len(self.app.state.store.list()),1)

    def test_tamper_and_changed_sources_do_not_render(self):
        record=self.finish(self.service.get(self.post().json()['assembly']['id']))
        plan_path=self.service.root/record['id']/'plan.json'
        original=plan_path.read_bytes();plan_path.write_text('{}')
        with self.assertRaises(Exception) as error:self.service.load_plan(record['id'])
        self.assertEqual(error.exception.status_code,409)
        plan_path.write_bytes(original);self.source.write_bytes(b'new media')
        with self.assertRaises(Exception):self.service.load_plan(record['id'])

    def test_manual_render_then_crash_handoff_returns_same_completed_job(self):
        record=self.service.get(self.post(auto_render=True).json()['assembly']['id'])
        enqueue=self.service.enqueue_render
        self.service.enqueue_render=lambda item:(_ for _ in ()).throw(RuntimeError('temporary'))
        done=self.finish(record);public=done['result']
        self.service.enqueue_render=enqueue
        response=self.client.post('/api/creations',headers=self.auth,json={
            'mode':'story','sources':public['sources'],'brief':self.body['brief'],'title':'自定义作品名',
            'spoken_script':self.body['spoken_script'],'expected_script_sha256':public['script_sha256'],
            'assembly_plan_id':record['id'],'options':self.body['options'],'idempotency_key':'manual-click'})
        self.assertEqual(response.status_code,201,response.text)
        job_id=response.json()['job']['id']
        self.assertTrue(self.app.state.store.owned_by_request(job_id,'assembly-render-'+record['id']))
        with self.app.state.store.connection() as db:db.execute("UPDATE jobs SET status='review' WHERE id=?",(job_id,))
        self.service.update(record['id'],job_id=None,error=None)
        self.service.handoff(self.service.retry_handoff(record['id']))
        self.assertEqual(self.service.get(record['id'])['job_id'],job_id)
        self.assertEqual(len(self.app.state.store.list()),1)

    def test_auth_invalid_sources_queue_bound_and_empty_library(self):
        self.assertEqual(self.client.post('/api/assembly-plans',json=self.body).status_code,401)
        self.assertEqual(self.post(sources=['../secret.mp4']).status_code,400)
        self.assertEqual(self.post(sources=[self.source.name,self.source.name]).status_code,400)
        for _ in range(3):self.assertEqual(self.post(request_id=str(uuid4())).status_code,202)
        self.assertEqual(self.post(request_id=str(uuid4())).status_code,429)
        self.source.unlink()
        self.assertEqual(self.post(request_id=str(uuid4())).status_code,400)

    def test_builder_cannot_change_script(self):
        record=self.service.get(self.post().json()['assembly']['id'])
        self.service.builder=lambda *a,**kw:{'assembly':{'script':'改稿','script_sha256':script_hash('改稿')}}
        self.service.process(record)
        self.assertEqual(self.service.get(record['id'])['status'],'failed')
        self.assertFalse((self.service.root/record['id']/'plan.json').exists())

    def test_matching_metadata_cannot_hide_different_actual_narration(self):
        record=self.service.get(self.post().json()['assembly']['id'])
        self.service.builder=lambda paths,script,*a,**kw:{'spoken_script':'偷偷改写的配音稿。',
            'assembly':{'script':script,'script_sha256':script_hash(script)}}
        self.service.process(record)
        failed=self.service.get(record['id'])
        self.assertEqual(failed['error_code'],'script_mismatch')
        self.assertFalse((self.service.root/record['id']/'plan.json').exists())

    def test_failed_handoff_resumes_after_restart_without_exposing_exception(self):
        self.service.enqueue_render=Mock(side_effect=HTTPException(503,'private-api-key must stay private'))
        record=self.service.get(self.post(auto_render=True).json()['assembly']['id'])
        with patch('studio.assembly_service.time.time',return_value=100):
            failed=self.finish(record)
            self.service.retry_ready_handoffs()
        self.assertEqual(self.service.enqueue_render.call_count,1)
        self.assertEqual(failed['handoff_attempts'],1)
        self.assertNotIn('private',failed['error'])
        resumed=Mock(return_value='a'*32)
        reopened=AssemblyService(self.root/'data',self.root/'inbox',validate_source=self.service.validate_source,
            source_allowed=self.service.source_allowed,enqueue_render=resumed)
        with patch('studio.assembly_service.time.time',return_value=106):reopened.retry_ready_handoffs()
        resumed.assert_called_once()
        self.assertEqual(reopened.get(record['id'])['job_id'],'a'*32)
        self.assertIsNone(reopened.get(record['id'])['error'])

    def test_handoff_has_three_persistent_attempts_then_manual_intent_can_retry(self):
        enqueue=Mock(side_effect=[RuntimeError('private detail')]*3+['b'*32])
        self.service.enqueue_render=enqueue
        record=self.service.get(self.post(auto_render=True).json()['assembly']['id'])
        with patch('studio.assembly_service.time.time',return_value=100):self.finish(record)
        with patch('studio.assembly_service.time.time',return_value=105):self.service.retry_ready_handoffs()
        with patch('studio.assembly_service.time.time',return_value=125):self.service.retry_ready_handoffs()
        self.assertEqual(enqueue.call_count,3)
        reopened=AssemblyService(self.root/'data',self.root/'inbox',validate_source=self.service.validate_source,
            source_allowed=self.service.source_allowed,enqueue_render=enqueue)
        with patch('studio.assembly_service.time.time',return_value=1000):
            reopened.retry_ready_handoffs();self.assertEqual(enqueue.call_count,3)
            queued=reopened.retry_handoff(record['id'])
            self.assertEqual(queued['handoff_attempts'],0)
            self.assertEqual(enqueue.call_count,3)  # API only persists intent.
            reopened.retry_ready_handoffs()
        self.assertEqual(enqueue.call_count,4)
        self.assertEqual(reopened.get(record['id'])['job_id'],'b'*32)

    def test_public_errors_are_whitelisted_even_without_path_characters(self):
        class MediaError(RuntimeError):
            code='material_shortfall'
        for error,code in [(RuntimeError('SECRET_TOKEN internal gpu details'),'assembly_failed'),
                           (ValueError('SECRET_TOKEN invalid raw output'),'assembly_failed'),
                           (MediaError('SECRET_TOKEN internal media details'),'material_shortfall')]:
            record=self.service.get(self.post(request_id=str(uuid4())).json()['assembly']['id'])
            self.service.builder=Mock(side_effect=error)
            self.service.process(record)
            failed=self.service.get(record['id'])
            self.assertEqual(failed['status'],'failed')
            self.assertEqual(failed['error_code'],code)
            self.assertNotIn('SECRET_TOKEN',json.dumps(self.service.public(failed)))
            self.assertIn('SECRET_TOKEN',(self.service.root/record['id']/'error.log').read_text(encoding='utf-8'))

    def test_retry_handoff_never_turns_planning_only_into_automatic_render(self):
        record=self.finish(self.service.get(self.post(auto_render=False).json()['assembly']['id']))
        self.service.enqueue_render=Mock()
        same=self.service.retry_handoff(record['id']);self.service.retry_ready_handoffs()
        self.assertEqual(same['status'],'ready')
        self.service.enqueue_render.assert_not_called()


if __name__=='__main__':unittest.main()
