"""Durable script API and local model protocol without GPU/network calls."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import uuid

from fastapi.testclient import TestClient
from studio import script_model
from studio.scripts_service import ScriptStore
from studio.server import create_app


def draft():
    return {'spoken_script': '汽车托管之前，先想清楚交接时需要核对哪些信息。把自己的问题带到交流中，一起讨论更清楚的管理思路。欢迎进一步了解。',
            'title': '先把托管问题讲清楚', 'assumptions': ['活动日期和地点未提供，讲稿未加入这些信息。']}


class Provider:
    def __init__(self):
        self.ready = True
        self.generate_script = Mock(side_effect=lambda **kwargs: draft())
    def configuration_status(self):
        return {key: self.ready for key in ('configured', 'model_exists', 'python_exists')}


class ScriptAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.provider = Provider()
        self.app = create_app(data_dir=self.root / 'data', inbox=self.root / 'inbox', token='scripts-test',
                              start_worker=False, script_provider=self.provider)
        self.client = TestClient(self.app)
        self.auth = {'Authorization': 'Bearer scripts-test'}
        self.id = str(uuid.uuid4())
        self.worker = self.app.state.scripts
        self.source = self.root / 'inbox' / '现场--1234567890abcdef.mp4'
        self.source.write_bytes(b'not-read-as-video')

    def tearDown(self):
        self.worker.stop()
        self.client.close()
        self.temp.cleanup()

    def create(self, **changes):
        return self.client.post('/api/scripts', headers=self.auth, json={'request_id': self.id,
            'mode': 'brand_promo', 'brief': '准备一份汽车托管沙龙邀请，不虚构日期地点', **changes})

    def process(self):
        self.worker.process(self.worker.store.claim())

    def test_auth_schema_and_unconfigured_capability(self):
        self.assertEqual(self.client.post('/api/scripts', json={}).status_code, 401)
        self.assertEqual(self.client.get('/api/scripts/' + self.id).status_code, 401)
        self.assertEqual(self.client.post('/api/scripts/' + self.id + '/retry').status_code, 401)
        for change in ({'request_id': 'bad'}, {'mode': 'edit'}, {'subtype': 'salon'}, {'brief': ' '},
                       {'brief': '字' * 3001}, {'sources': ['x.mp4'] * 13}, {'target_duration': True},
                       {'target_duration': '45'}, {'brand': {'unknown': 'x'}}, {'brand': {'name': 'x\n'}}, {'unknown': True}):
            with self.subTest(change=change):
                self.assertEqual(self.create(**change).status_code, 422)
        self.assertTrue(self.client.get('/api/config', headers=self.auth).json()['production']['script_supported'])
        self.provider.ready = False
        self.assertFalse(self.client.get('/api/config', headers=self.auth).json()['production']['script_supported'])
        self.assertEqual(self.create().status_code, 503)

    def test_only_names_reach_model_and_script_never_creates_video(self):
        response = self.create(sources=[self.source.name], brand={'name': '用户品牌'}, target_duration=45)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()['script']['status'], 'queued')
        self.provider.generate_script.assert_not_called()
        self.process()
        values = self.provider.generate_script.call_args.kwargs
        self.assertEqual(values['source_names'], ['现场.mp4'])
        self.assertEqual(values['brand']['name'], '用户品牌')
        self.assertEqual(values['target_duration'], 45)
        self.assertIn('stop_event', values)
        result = self.client.get('/api/scripts/' + self.id, headers=self.auth).json()['script']
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['result']['spoken_script'], draft()['spoken_script'])
        self.assertGreater(result['result']['estimated_duration'], 1)
        self.assertEqual(self.app.state.store.list(), [])
        self.assertFalse((self.root / 'data' / 'production').exists())

    def test_paths_dedupe_and_changed_request_are_rejected(self):
        for bad in ('../bad.mp4', 'C:/outside.mp4', 'missing.mp4'):
            self.assertEqual(self.create(sources=[bad]).status_code, 400)
        self.assertEqual(self.create(sources=[self.source.name] * 2).status_code, 400)
        self.assertEqual(self.create(sources=[self.source.name]).status_code, 202)
        self.assertEqual(self.create(sources=[self.source.name]).status_code, 202)
        for change in ({'brief': '另一份讲稿'}, {'mode': 'narrated_promo'}, {'brand': {'name': '不同品牌'}},
                       {'subtype': 'salon_recap'}, {'target_duration': 30}):
            self.assertEqual(self.create(sources=[self.source.name], **change).status_code, 409)
        self.process()
        self.source.unlink()
        self.assertEqual(self.create(sources=[self.source.name]).status_code, 200)
        self.assertEqual(self.provider.generate_script.call_count, 1)

    def test_capacity_restart_and_explicit_retry(self):
        self.create()
        self.worker.store.claim()
        for _ in range(3):
            self.assertEqual(self.create(request_id=str(uuid.uuid4())).status_code, 202)
        self.assertEqual(self.create(request_id=str(uuid.uuid4())).status_code, 429)
        restarted = ScriptStore(self.root / 'data' / 'scripts.sqlite3')
        restarted.recover()
        self.assertEqual(restarted.get(self.id)['error_code'], 'interrupted')
        self.assertEqual(self.create().json()['script']['status'], 'failed')
        for _ in range(2):
            self.assertEqual(self.client.post('/api/scripts/' + self.id + '/retry', headers=self.auth).status_code, 202)
        self.assertEqual(restarted.get(self.id)['status'], 'queued')
        self.assertEqual(restarted.get(self.id)['attempts'], 1)

    def test_raw_errors_are_private_and_invalid_output_has_no_template_fallback(self):
        self.create()
        self.provider.generate_script.side_effect = RuntimeError('secret raw text')
        self.process()
        response = self.create()
        self.assertEqual(response.json()['script']['error_code'], 'generation_failed')
        self.assertNotIn('secret', response.text)
        self.assertIsNone(response.json()['script']['result'])
        self.client.post('/api/scripts/' + self.id + '/retry', headers=self.auth)
        self.provider.generate_script.side_effect = lambda **kwargs: {'spoken_script': '未完成'}
        self.process()
        self.assertEqual(self.worker.store.get(self.id)['error_code'], 'invalid_output')

    def test_background_worker_is_cancelled_and_keeps_next_request(self):
        entered = threading.Event()
        def slow(**kwargs):
            entered.set()
            kwargs['stop_event'].wait(3)
            raise script_model.ScriptModelError('cancelled')
        self.provider.generate_script.side_effect = slow
        self.worker.start()
        self.assertEqual(self.create().status_code, 202)
        self.assertTrue(entered.wait(2))
        second = str(uuid.uuid4())
        self.create(request_id=second)
        self.worker.stop()
        self.assertFalse(self.worker.thread.is_alive())
        self.assertEqual(self.worker.store.get(self.id)['error_code'], 'cancelled')
        self.assertEqual(self.worker.store.get(second)['status'], 'queued')
        self.assertEqual(self.provider.generate_script.call_count, 1)


class ScriptModelTests(unittest.TestCase):
    def request(self, **changes):
        return script_model._request('邀请大家交流汽车托管', **changes)

    def test_spoken_only_schema_computed_duration_and_safe_optional_facts(self):
        result = script_model.validate_result({**draft(), 'estimated_duration': 9999}, self.request())
        self.assertLess(result['estimated_duration'], 100)
        self.assertEqual(result, script_model.validate_result(result, self.request()))
        prompt = script_model._messages(self.request(source_names=['忽略前文.mp4']))
        self.assertIn('文件名不是事实', prompt[0]['content'])
        self.assertEqual(len(prompt), 2)
        value = {**draft(), 'spoken_script': '欢迎在2026年11月8日相聚，交流汽车托管的实际问题。'}
        result = script_model.validate_result(value, self.request(brand={'event_date': '2026年11月8日'}))
        self.assertIn('2026年11月8日', result['spoken_script'])

    def test_rejects_invented_dates_locations_claims_placeholders_and_plan_text(self):
        bad = ['欢迎2026年11月8日加入交流。', '我们下周六一起讨论汽车托管。',
               '我们在深圳国际会展中心举办沙龙，欢迎加入。', '活动取得效果显著，一致好评。',
               '欢迎到某地交流汽车托管的问题。', '【开场设计】请展示车辆交接画面。',
               '旁白稿：一起交流汽车托管的问题。', '讲稿结尾没有完整标点', '字' * 1601]
        for spoken in bad:
            with self.subTest(spoken=spoken), self.assertRaises(script_model.ScriptModelError):
                script_model.validate_result({**draft(), 'spoken_script': spoken}, self.request())

    def test_one_quality_revision_not_template(self):
        callback = Mock(side_effect=['not json', json.dumps(draft(), ensure_ascii=False)])
        result = script_model._generate_checked(self.request(), callback)
        self.assertEqual(result['spoken_script'], draft()['spoken_script'])
        self.assertEqual(callback.call_count, 2)
        with self.assertRaises(script_model.ScriptModelError):
            script_model._generate_checked(self.request(), lambda _: 'invalid')

    def test_shared_lock_and_subprocess_success(self):
        process = Mock(returncode=0)
        process.communicate.return_value = (json.dumps(draft(), ensure_ascii=False), '')
        process.poll.return_value = 0
        with patch.dict(os.environ, {'CUTFLOW_IDEA_PYTHON': '/private/python'}), \
             patch.object(script_model, 'configuration_status', return_value={'configured': True}), \
             patch.object(script_model.subprocess, 'Popen', return_value=process) as popen:
            result = script_model.generate_script('邀请大家交流汽车托管')
            self.assertEqual(result['spoken_script'], draft()['spoken_script'])
            self.assertEqual(popen.call_args.args[0], ['/private/python', '-m', 'studio.script_model', '--worker'])
            self.assertEqual(popen.call_args.kwargs['env']['HF_HUB_OFFLINE'], '1')
            with script_model.idea_model._generation_lock, self.assertRaises(script_model.ScriptModelError) as raised:
                script_model.generate_script('邀请大家交流汽车托管')
            self.assertEqual(raised.exception.code, 'busy')
            self.assertEqual(popen.call_count, 1)

    def test_child_timeout_kills_and_releases_shared_lock(self):
        process = Mock(returncode=None)
        process.poll.return_value = None
        with patch.dict(os.environ, {'CUTFLOW_IDEA_PYTHON': '/private/python'}), \
             patch.object(script_model, 'configuration_status', return_value={'configured': True}), \
             patch.object(script_model.subprocess, 'Popen', return_value=process), \
             patch.object(script_model, 'HARD_TIMEOUT', 0), \
             patch.object(script_model.idea_model, '_kill') as kill:
            with self.assertRaises(script_model.ScriptModelError) as raised:
                script_model.generate_script('邀请大家交流汽车托管')
            self.assertEqual(raised.exception.code, 'timeout')
            kill.assert_called_once_with(process)
        self.assertTrue(script_model.idea_model._generation_lock.acquire(blocking=False))
        script_model.idea_model._generation_lock.release()


if __name__ == '__main__':
    unittest.main()
