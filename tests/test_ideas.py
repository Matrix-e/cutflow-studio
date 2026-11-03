"""Async idea queue tests with an injected provider; no model/network calls."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock
import uuid

from fastapi.testclient import TestClient

from studio.ideas_service import IdeaStore
from studio.server import create_app


def valid_result():
    return {'expanded_brief': '为门店经营者制作清晰可信的介绍，先呈现顾客疑问，再说明服务流程。',
            'production_brief': '保留原声，完整保留素材，不新增配音，不添加背景音乐。',
            'title': '门店服务介绍', 'audience': '了解服务的潜在顾客', 'goal': '解释服务流程',
            'hook': '先回答顾客最关心的问题', 'outline': ['顾客问题', '服务步骤', '行动提示'],
            'visual_style': '清晰克制，使用已有画面', 'narration': '仅保留已有原声，不新增旁白',
            'music': '不额外添加音乐', 'cta': '引导了解服务详情', 'assumptions': ['尚未查看视频内容']}


class FakeProvider:
    def __init__(self):
        self.state = {'configured': True, 'model_exists': True, 'python_exists': True}
        self.generate_idea = Mock(side_effect=lambda *args, **kwargs: valid_result())
    def configuration_status(self):
        return dict(self.state)


class IdeaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.provider = FakeProvider()
        self.app = create_app(data_dir=self.root / 'data', inbox=self.root / 'inbox', token='idea-test',
                              start_worker=False, idea_provider=self.provider)
        self.client = TestClient(self.app)
        self.auth = {'Authorization': 'Bearer idea-test'}
        self.worker = self.app.state.ideas
        self.identifier = str(uuid.uuid4())
        self.source = self.root / 'inbox' / '讲解--0123456789abcdef.mp4'
        self.source.write_bytes(b'metadata-only-not-read-or-probed')

    def tearDown(self):
        self.worker.stop()
        self.client.close()
        self.temp.cleanup()

    def create(self, **changes):
        return self.client.post('/api/ideas', headers=self.auth,
            json={'request_id': self.identifier, 'brief': '帮我构思一条门店介绍，不要新增旁白和音乐', **changes})

    def wait(self, identifier, status):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            record = self.worker.store.get(identifier)
            if record['status'] == status:
                return record
            time.sleep(0.01)
        self.fail(f"Expected {status}, got {record['status']}")

    def process_one(self):
        self.worker.process(self.worker.store.claim())

    def test_auth_and_schema_validation(self):
        self.assertEqual(self.client.post('/api/ideas', json={}).status_code, 401)
        for route in (f'/api/ideas/{self.identifier}', f'/api/ideas/{self.identifier}/retry'):
            method = self.client.post if route.endswith('/retry') else self.client.get
            self.assertEqual(method(route).status_code, 401)
        self.assertEqual(self.client.get(f'/api/ideas/{self.identifier}?token=idea-test').status_code, 401)
        invalid = [{'request_id': 'not-a-uuid'}, {'brief': ' '}, {'brief': 'x' * 3001}, {'brief': 'bad\0value'},
                   {'sources': ['a.mp4'] * 13}, {'style': 'x' * 101}, {'unknown': 'value'}]
        for changes in invalid:
            with self.subTest(changes=changes):
                self.assertEqual(self.create(**changes).status_code, 422)
        self.assertIsNone(self.worker.store.get(self.identifier))

    def test_capability_requires_configuration_model_and_runtime(self):
        self.assertTrue(self.client.get('/api/config', headers=self.auth).json()['idea_refinement_supported'])
        for key in ('configured', 'model_exists', 'python_exists'):
            self.provider.state[key] = False
            self.assertFalse(self.client.get('/api/config', headers=self.auth).json()['idea_refinement_supported'])
            self.assertEqual(self.create().status_code, 503)
            self.provider.state[key] = True
        self.assertIsNone(self.worker.store.get(self.identifier))

    def test_sources_are_validated_and_only_display_names_reach_provider(self):
        for bad in ('../outside.mp4', '/outside.mp4', 'C:/outside.mp4', 'missing.mp4'):
            self.assertEqual(self.create(sources=[bad]).status_code, 400)
        self.assertEqual(self.create(sources=[self.source.name] * 2).status_code, 400)
        response = self.create(sources=[self.source.name], style='简明可信')
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['idea']['status'], 'queued')
        self.provider.generate_idea.assert_not_called()
        self.process_one()
        args, kwargs = self.provider.generate_idea.call_args
        self.assertEqual(args[1], ['讲解.mp4'])
        self.assertEqual(args[2], '简明可信')
        self.assertIs(kwargs['stop_event'], self.worker.stop_event)
        self.assertEqual(self.app.state.store.list(), [])
        self.assertEqual(list((self.root / 'data' / 'renders').iterdir()), [])

    def test_idempotency_and_every_changed_input_conflict(self):
        first = self.create().json()['idea']
        self.assertEqual(self.create().json()['idea'], first)
        for change in ({'brief': '另一条想法'}, {'style': '活泼'}, {'sources': [self.source.name]}, {'sources': ['../bad.mp4']}):
            self.assertEqual(self.create(**change).status_code, 409)
        self.process_one()
        response = self.create()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['idea']['status'], 'succeeded')
        self.assertEqual(response.json()['idea']['attempts'], 1)
        self.assertEqual(self.provider.generate_idea.call_count, 1)

    def test_concurrent_same_id_creates_one_durable_record(self):
        store = self.worker.store
        def create():
            return store.create(self.identifier, brief='一样的想法', sources=[], source_names=[], style=None)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: create(), range(8)))
        self.assertTrue(all(row['id'] == self.identifier for row in results))
        with store.connection() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM ideas').fetchone()[0], 1)

    def test_bounded_queue_counts_running_and_duplicate_does_not_use_slot(self):
        identifiers = [str(uuid.uuid4()) for _ in range(4)]
        for identifier in identifiers:
            self.assertEqual(self.create(request_id=identifier).status_code, 202)
        first = self.worker.store.claim()
        self.assertEqual(first['status'], 'running')
        self.assertEqual(self.create(request_id=identifiers[0]).status_code, 202)
        self.assertEqual(self.create().status_code, 429)
        self.assertIsNone(self.worker.store.get(self.identifier))
        self.worker.store.complete(first['id'], code='generation_failed')
        self.assertEqual(self.create().status_code, 202)

    def test_failure_is_safe_and_only_explicit_retry_requeues(self):
        self.provider.generate_idea.side_effect = RuntimeError('secret=must-never-reach-client')
        self.create()
        self.process_one()
        failure = self.create()
        self.assertEqual(failure.status_code, 200)
        self.assertEqual(failure.json()['idea']['status'], 'failed')
        self.assertNotIn('secret', failure.text)
        self.assertTrue(failure.json()['idea']['can_retry'])
        self.assertEqual(self.provider.generate_idea.call_count, 1)
        self.provider.generate_idea.side_effect = lambda *args, **kwargs: valid_result()
        route = f'/api/ideas/{self.identifier}/retry'
        for _ in range(2):
            self.assertEqual(self.client.post(route, headers=self.auth).status_code, 202)
        self.process_one()
        record = self.worker.store.get(self.identifier)
        self.assertEqual(record['status'], 'succeeded')
        self.assertEqual(record['attempts'], 2)
        self.assertEqual(self.client.post(route, headers=self.auth).status_code, 409)

    def test_invalid_model_output_is_failed_without_template_fallback(self):
        self.provider.generate_idea.side_effect = lambda *args, **kwargs: {**valid_result(), 'expanded_brief': '字' * 2401}
        self.create()
        self.process_one()
        result = self.worker.store.get(self.identifier)
        self.assertEqual(result['error_code'], 'invalid_output')
        self.assertIsNone(result['result'])
        self.assertEqual(self.app.state.store.list(), [])

    def test_success_persists_after_restart_and_source_deletion(self):
        self.create(sources=[self.source.name])
        self.process_one()
        self.source.unlink()
        recovered = IdeaStore(self.root / 'data' / 'ideas.sqlite3')
        recovered.recover()
        self.assertEqual(recovered.get(self.identifier)['status'], 'succeeded')
        self.assertEqual(self.create(sources=[self.source.name]).status_code, 200)
        self.provider.state['model_exists'] = False
        result = self.client.get(f'/api/ideas/{self.identifier}', headers=self.auth)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['idea']['result']['expanded_brief'], valid_result()['expanded_brief'])

    def test_restart_marks_running_failed_but_preserves_waiting_work(self):
        self.create()
        self.worker.store.claim()
        second = str(uuid.uuid4())
        self.create(request_id=second)
        restarted = IdeaStore(self.root / 'data' / 'ideas.sqlite3')
        restarted.recover()
        self.assertEqual(restarted.get(self.identifier)['status'], 'failed')
        self.assertEqual(restarted.get(self.identifier)['error_code'], 'interrupted')
        self.assertEqual(restarted.get(second)['status'], 'queued')
        self.provider.generate_idea.assert_not_called()

    def test_background_serialization_and_cancellation_keep_http_fast(self):
        entered = threading.Event()
        calls = []
        def slow(brief, names, style, *, stop_event):
            calls.append(brief)
            entered.set()
            stop_event.wait(3)
            error = RuntimeError('raw child diagnostics stay private')
            error.code = 'cancelled'
            raise error
        self.provider.generate_idea.side_effect = slow
        self.worker.start()
        before = time.monotonic()
        self.assertEqual(self.create().status_code, 202)
        self.assertLess(time.monotonic() - before, 1)
        self.assertTrue(entered.wait(2))
        second = str(uuid.uuid4())
        self.assertEqual(self.create(request_id=second).status_code, 202)
        self.assertEqual(self.create().json()['idea']['status'], 'running')
        self.assertEqual(len(calls), 1)
        self.worker.stop()
        self.assertFalse(self.worker.thread.is_alive())
        self.assertEqual(self.worker.store.get(self.identifier)['error_code'], 'cancelled')
        self.assertEqual(self.worker.store.get(second)['status'], 'queued')

    def test_provider_extra_metadata_is_not_exposed(self):
        self.provider.generate_idea.side_effect = lambda *args, **kwargs: {**valid_result(), 'raw_debug': 'not-public'}
        self.create(brief='字' * 3000)
        self.process_one()
        response = self.client.get(f'/api/ideas/{self.identifier}', headers=self.auth)
        self.assertEqual(response.json()['idea']['status'], 'succeeded')
        self.assertNotIn('raw_debug', response.json()['idea']['result'])
        self.assertNotIn('fingerprint', response.json()['idea'])

    def test_full_provider_narration_limit_is_accepted(self):
        self.provider.generate_idea.side_effect = lambda *args, **kwargs: {**valid_result(), 'narration': '字' * 800}
        self.create()
        self.process_one()
        response = self.client.get(f'/api/ideas/{self.identifier}', headers=self.auth)
        self.assertEqual(response.json()['idea']['status'], 'succeeded')
        self.assertEqual(len(response.json()['idea']['result']['narration']), 800)

    def test_server_lifespan_starts_and_stops_idea_worker(self):
        app = create_app(data_dir=self.root / 'lifecycle-data', inbox=self.root / 'lifecycle-inbox',
                         token='test-token', idea_provider=self.provider)
        with TestClient(app) as client:
            self.assertTrue(app.state.ideas.thread.is_alive())
            response = client.post('/api/ideas', json={'request_id': self.identifier, 'brief': '介绍汽车托管'},
                                   headers={'Authorization': 'Bearer test-token'})
            self.assertEqual(response.status_code, 202)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and app.state.ideas.store.get(self.identifier)['status'] != 'succeeded':
                time.sleep(0.01)
            self.assertEqual(app.state.ideas.store.get(self.identifier)['status'], 'succeeded')
            self.assertEqual(app.state.store.list(), [])
        self.assertFalse(app.state.ideas.thread.is_alive())


if __name__ == '__main__':
    unittest.main()
