"""Work-library API integration; all rendering/model operations are controlled."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from studio.server import create_app


class ScriptProvider:
    def __init__(self):
        self.generate_script = Mock(return_value={'spoken_script': '交流汽车托管时，先把自己的问题说清楚。把需要核对的信息逐项记下来，欢迎进一步了解。',
            'title': '把托管问题说清楚', 'assumptions': ['活动具体资料仍需核对。']})
    @staticmethod
    def configuration_status():
        return {'configured': True, 'model_exists': True, 'python_exists': True}


class WorkLibraryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.provider = ScriptProvider()
        self.capabilities = {'modes': {mode: {'ready': True, 'reason': None} for mode in
            ('edit', 'narrated_promo', 'brand_promo', 'talking_head')},
            'voices': [{'id': 'serena', 'name': '中文女声', 'language': 'zh'}],
            'script_supported': True, 'narration_provider': 'test-local', 'narration_model': 'test-model'}
        self.cap_patch = patch('studio.server.production_capabilities', side_effect=lambda *args: copy.deepcopy(self.capabilities))
        self.cap_patch.start()
        self.app = create_app(data_dir=self.root / 'data', inbox=self.root / 'inbox', token='work-test',
                              start_worker=False, script_provider=self.provider)
        self.client = TestClient(self.app)
        self.auth = {'Authorization': 'Bearer work-test'}
        self.store = self.app.state.store
        self.source = self.root / 'inbox' / '公司画面.mp4'
        self.source.write_bytes(b'immutable original source fixture')
        self.parent = self.completed()

    def tearDown(self):
        self.app.state.scripts.stop()
        self.client.close()
        self.cap_patch.stop()
        self.temp.cleanup()

    def create(self, **changes):
        return self.client.post('/api/creations', headers=self.auth, json={
            'mode': 'narrated_promo', 'sources': [self.source.name], 'brief': '竖版介绍汽车托管',
            'spoken_script': '汽车托管之前，先把自己关心的问题列出来。欢迎进一步了解。',
            'voice_id': 'serena', 'options': {'subtitles': True}, 'idempotency_key': str(uuid4()), **changes})

    def completed(self, **changes):
        response = self.create(**changes)
        self.assertEqual(response.status_code, 201, response.text)
        job = response.json()['job']
        output = self.root / 'data' / 'renders' / (job['id'] + '.mp4')
        output.write_bytes(b'completed original render fixture ' + job['id'].encode())
        self.store.transition(job['id'], ['queued'], 'analyzing', '正在分析原始素材')
        self.store.transition(job['id'], ['analyzing'], 'rendering', '正在合成画面、字幕和声音')
        return self.store.transition(job['id'], ['rendering'], 'review', '成片检查通过，等待人工试看确认',
            result={'duration': 18.2, 'qc': {'passed': True}}, output_path=str(output),
            production={**(job['production'] or {}), 'stage': 'ready'})

    def draft(self, identifier=None, **kwargs):
        return self.client.get('/api/jobs/' + (identifier or self.parent['id']) + '/draft', headers=self.auth, **kwargs)

    def script(self, **changes):
        return self.client.post('/api/scripts', headers=self.auth, json={'request_id': str(uuid4()),
            'mode': 'narrated_promo', 'brief': '改得简洁些，保持事实边界', **changes})

    def favorite(self, identifier=None):
        identifier = identifier or self.parent['id']
        response = self.client.put('/api/knowledge/' + identifier, headers=self.auth,
                                   json={'favorite': True, 'notes': '参考开场结构，不复用业务事实。'})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['item']

    def test_draft_requires_authenticated_completed_production_qc_and_existing_video(self):
        self.assertEqual(self.client.get('/api/jobs/' + self.parent['id'] + '/draft').status_code, 401)
        self.assertEqual(self.draft(str(uuid4())).status_code, 404)
        self.assertEqual(self.draft('invalid-id').status_code, 422)
        for status in ('queued', 'analyzing', 'rendering', 'failed', 'publishing'):
            with self.subTest(status=status), patch.object(self.store, 'get', return_value={**self.parent, 'status': status}):
                self.assertEqual(self.draft().status_code, 409)
        with patch.object(self.store, 'get', return_value={**self.parent, 'result': {'qc': {'passed': False}}}):
            self.assertEqual(self.draft().status_code, 409)
        with patch.object(self.store, 'get', return_value={**self.parent, 'job_kind': 'technical_test'}):
            self.assertEqual(self.draft().status_code, 404)
        Path(self.parent['output_path']).unlink()
        self.assertEqual(self.draft().status_code, 404)

    def test_returned_narrated_draft_can_create_a_child_without_modifying_parent(self):
        parent = copy.deepcopy(self.store.get(self.parent['id']))
        parent_events = self.store.events(parent['id'])
        media = Path(parent['output_path']).read_bytes()
        source = self.source.read_bytes()
        response = self.draft(str(UUID(parent['id'])))
        self.assertEqual(response.status_code, 200, response.text)
        copied = response.json()
        body = {**copied['draft'], 'sources': copied['sources'], 'title': copied['title'],
                'parent_job_id': copied['parent_job_id'], 'revision_kind': 'rewrite',
                'spoken_script': '这是修改后的完整口语讲稿，先把用户的问题说清楚。欢迎了解。',
                'idempotency_key': str(uuid4())}
        child = self.client.post('/api/creations', headers=self.auth, json=body)
        self.assertEqual(child.status_code, 201, child.text)
        record = child.json()['job']
        self.assertNotEqual(record['id'], parent['id'])
        self.assertEqual(record['parent_job_id'], parent['id'])
        self.assertEqual(record['revision_kind'], 'rewrite')
        self.assertEqual(record['status'], 'queued')
        self.assertEqual(self.store.get(parent['id']), parent)
        self.assertEqual(self.store.events(parent['id']), parent_events)
        self.assertEqual(Path(parent['output_path']).read_bytes(), media)
        self.assertEqual(self.source.read_bytes(), source)
        replay = self.client.post('/api/creations', headers=self.auth, json=body)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json()['job']['id'], record['id'])

    def test_parent_link_requires_matching_operation_and_different_request_keys_keep_versions(self):
        self.assertEqual(self.create(parent_job_id=self.parent['id']).status_code, 422)
        self.assertEqual(self.create(revision_kind='rewrite').status_code, 422)
        self.assertEqual(self.create(parent_job_id=uuid4().hex, revision_kind='rewrite').status_code, 404)
        first = self.create(parent_job_id=self.parent['id'], revision_kind='rewrite', idempotency_key='child-1')
        second = self.create(parent_job_id=self.parent['id'], revision_kind='rewrite', idempotency_key='child-2')
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(second.status_code, 201, second.text)
        self.assertNotEqual(first.json()['job']['id'], second.json()['job']['id'])
        repeat = self.create(parent_job_id=self.parent['id'], revision_kind='rewrite', idempotency_key='child-2')
        self.assertEqual(repeat.json()['job']['id'], second.json()['job']['id'])

    def test_legacy_edit_draft_defaults_roundtrip_without_null_production_fields(self):
        parent = self.completed(mode='edit', spoken_script='', voice_id='', options={})
        response = self.draft(parent['id'])
        self.assertEqual(response.status_code, 200, response.text)
        copied = response.json()
        body = {**copied['draft'], 'sources': copied['sources'], 'title': copied['title'],
                'parent_job_id': parent['id'], 'revision_kind': 'remix', 'idempotency_key': str(uuid4())}
        created = self.client.post('/api/creations', headers=self.auth, json=body)
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()['job']['mode'], 'edit')
        self.assertEqual(created.json()['job']['parent_job_id'], parent['id'])

    def test_presenter_variant_only_derives_from_presenter_work_and_checks_new_identity(self):
        portrait_id = str(uuid4())
        portrait = {'kind': 'personal', 'portrait_id': portrait_id, 'consent_confirmed': True}
        snapshot = {'id': portrait_id, 'kind': 'personal', 'sha256': 'd' * 64,
                    'consent_confirmed': True, 'face_validated': True, 'name': '新形象'}
        with patch.object(self.app.state.portraits, 'resolve', return_value=self.root / 'portrait.png'), \
             patch.object(self.app.state.portraits, 'snapshot', return_value=snapshot):
            self.assertEqual(self.create(parent_job_id=self.parent['id'], revision_kind='presenter').status_code, 409)
            rejected = self.create(mode='talking_head', sources=[], presenter=portrait,
                parent_job_id=self.parent['id'], revision_kind='presenter')
            self.assertEqual(rejected.status_code, 409)
            person = self.completed(mode='talking_head', sources=[], presenter=portrait)
            changed = self.create(mode='talking_head', sources=[], presenter=portrait,
                parent_job_id=person['id'], revision_kind='presenter')
            self.assertEqual(changed.status_code, 201, changed.text)
            self.assertEqual(changed.json()['job']['parent_job_id'], person['id'])
            self.assertEqual(self.store.get(person['id'])['status'], 'review')
            bad = self.create(mode='talking_head', sources=[], presenter={**portrait, 'consent_confirmed': False},
                parent_job_id=person['id'], revision_kind='presenter')
            self.assertEqual(bad.status_code, 422)
            with patch.object(self.app.state.portraits, 'snapshot', return_value={**snapshot, 'kind': 'public'}):
                self.assertEqual(self.create(mode='talking_head', sources=[], presenter=portrait,
                    parent_job_id=person['id'], revision_kind='presenter').status_code, 409)

    def test_reference_ids_resolve_both_uuid_formats_and_reject_nonfavorite_or_technical(self):
        saved = self.favorite()
        result = self.create(reference_ids=[str(UUID(self.parent['id']))], idempotency_key='with-reference')
        self.assertEqual(result.status_code, 201, result.text)
        reference = result.json()['job']['reference_works'][0]
        self.assertEqual(reference['source_job_id'], self.parent['id'])
        self.assertEqual(reference['revision'], saved['revision'])
        self.assertTrue(reference['untrusted'])
        self.assertEqual(self.create(reference_ids=[self.parent['id'], str(UUID(self.parent['id']))]).status_code, 400)
        self.assertEqual(self.create(reference_ids=[uuid4().hex]).status_code, 404)
        self.client.put('/api/knowledge/' + self.parent['id'], headers=self.auth,
                        json={'favorite': False, 'expected_revision': 1})
        self.assertEqual(self.create(reference_ids=[self.parent['id']]).status_code, 409)
        self.store.mark_technical_test(self.parent['id'], 'Verified test fixture')
        self.assertEqual(self.script(reference_ids=[self.parent['id']]).status_code, 409)

    def test_script_id_requires_success_and_preserves_user_edits(self):
        self.assertEqual(self.create(script_id=str(uuid4())).status_code, 409)
        queued = self.script()
        self.assertEqual(queued.status_code, 202, queued.text)
        identifier = queued.json()['script']['id']
        self.assertEqual(self.create(script_id=identifier).status_code, 409)
        worker = self.app.state.scripts
        claimed = worker.store.claim()
        self.assertEqual(self.create(script_id=identifier).status_code, 409)
        worker.store.complete(identifier, code='generation_failed')
        self.assertEqual(self.create(script_id=identifier).status_code, 409)
        worker.store.retry(identifier)
        worker.process(worker.store.claim())
        ready = worker.store.get(identifier)
        self.assertEqual(ready['status'], 'succeeded')
        exact = self.create(script_id=identifier, spoken_script=ready['result']['spoken_script'])
        self.assertEqual(exact.status_code, 201, exact.text)
        self.assertFalse(exact.json()['job']['settings']['creation']['script_modified'])
        edited = self.create(script_id=identifier, spoken_script='用户自己修改过的最终讲稿，保留新的说法。')
        self.assertEqual(edited.status_code, 201)
        self.assertTrue(edited.json()['job']['settings']['creation']['script_modified'])
        self.assertEqual(worker.store.get(identifier)['result'], ready['result'])

    def test_script_rewrite_and_reference_context_are_server_resolved(self):
        self.favorite()
        response = self.script(reference_ids=[str(UUID(self.parent['id']))], rewrite_source_id=self.parent['id'])
        self.assertEqual(response.status_code, 202, response.text)
        worker = self.app.state.scripts
        record = worker.store.get(response.json()['script']['id'])
        context = record['resolved_context']
        self.assertEqual(context['original_script'], self.parent['settings']['creation']['spoken_script'])
        self.assertEqual(context['reference_works'][0]['source_job_id'], self.parent['id'])
        worker.process(worker.store.claim())
        kwargs = self.provider.generate_script.call_args.kwargs
        self.assertEqual(kwargs['original_script'], context['original_script'])
        self.assertTrue(kwargs['reference_works'][0]['untrusted'])
        self.assertNotIn('resolved_context', response.json()['script'])

    def test_reference_reclassified_after_queue_is_blocked_before_model_call(self):
        self.favorite()
        response = self.script(reference_ids=[self.parent['id']])
        self.assertEqual(response.status_code, 202, response.text)
        self.store.mark_technical_test(self.parent['id'], 'Explicitly identified QA after submission')
        worker = self.app.state.scripts
        worker.process(worker.store.claim())
        record = worker.store.get(response.json()['script']['id'])
        self.assertEqual(record['status'], 'failed')
        self.provider.generate_script.assert_not_called()

    def test_progress_tracks_database_queue_and_phase_not_a_timer_percentage(self):
        first = self.create(brief='第一条排队', idempotency_key='queue-first').json()['job']
        second = self.create(brief='第二条排队', idempotency_key='queue-second').json()['job']
        detail = self.client.get('/api/jobs/' + second['id'], headers=self.auth).json()['job']['progress_detail']
        self.assertEqual(detail['stage'], 'queued')
        self.assertEqual(detail['queue_position'], 2)
        self.assertEqual(detail['completed_count'], 0)
        self.assertIsNone(detail['eta_seconds'])
        claimed = self.store.claim()
        self.assertEqual(claimed['id'], first['id'])
        next_detail = self.client.get('/api/jobs/' + second['id'], headers=self.auth).json()['job']['progress_detail']
        self.assertEqual(next_detail['queue_position'], 1)
        active = self.client.get('/api/jobs/' + first['id'], headers=self.auth).json()['job']['progress_detail']
        self.assertEqual(active['stage'], 'validating')
        self.assertIsNone(active['queue_position'])
        self.assertNotIn('percent', active)
        self.store.transition(first['id'], ['analyzing'], 'analyzing', '正在按已确认讲稿生成完整旁白',
            production={'stage': 'preparing_voice', 'mode': 'narrated_promo'})
        actual = self.client.get('/api/jobs/' + first['id'], headers=self.auth).json()['job']['progress_detail']
        self.assertEqual(actual['stage'], 'preparing_voice')
        self.assertIsNone(actual['eta_seconds'])


if __name__ == '__main__':
    unittest.main()
