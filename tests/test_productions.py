"""New narrated modes preserve the existing edit workflow and durable guards."""
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from studio.server import create_app
from studio.store import Store


class ProductionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.voice_state = {'configured': True, 'provider': 'local-test', 'model': 'test-voice-v1'}
        self.build = Mock(side_effect=self.plan)
        self.render = Mock(side_effect=self.render_video)
        self.modules = patch.dict(sys.modules, {'studio.narration': types.SimpleNamespace(
            configuration_status=lambda: self.voice_state,
            list_voices=lambda: [{'id': 'zh_female', 'name': '中文女声', 'language': 'zh'},
                                 {'id': 'zh_male', 'name': '中文男声', 'language': 'zh'}]),
            'studio.narrated': types.SimpleNamespace(build_narrated_plan=self.build, render_narrated_plan=self.render)})
        self.modules.start()
        self.app = create_app(data_dir=self.root / 'data', inbox=self.root / 'inbox', token='production-test', start_worker=False)
        self.client = TestClient(self.app)
        self.auth = {'Authorization': 'Bearer production-test'}
        self.store = self.app.state.store
        for name in ('镜头一.mp4', '镜头二.mp4'):
            (self.root / 'inbox' / name).write_bytes(b'video-source-version-one')

    def tearDown(self):
        self.client.close()
        self.modules.stop()
        self.temp.cleanup()

    @staticmethod
    def plan(paths, spoken_script, voice_id, options, settings, *, work_dir, stop_event):
        Path(work_dir, 'narration.wav').write_bytes(b'full-speech')
        return {'kind': 'narrated_composition', 'duration': 18.2, 'narration': {'duration': 18.2},
                'sources': paths, 'resolved': options, 'warnings': ['按完整语音时长制作。']}

    @staticmethod
    def render_video(plan, output, settings):
        Path(output).write_bytes(b'complete-narrated-video')
        return {'output_path': output, 'duration': 18.2, 'qc': {'passed': True}, 'warnings': []}

    def create(self, **changes):
        return self.client.post('/api/creations', headers=self.auth, json={
            'mode': 'narrated_promo', 'sources': ['镜头一.mp4', '镜头二.mp4'],
            'brief': '做成10秒竖版，加轻音乐', 'spoken_script': '这是用户最终确认的完整口语讲稿，不把策划说明读进配音。',
            'voice_id': 'zh_female', 'options': {}, 'idempotency_key': 'production-one', **changes})

    def test_config_voice_validation_and_unavailable_person_never_create_fake_job(self):
        config = self.client.get('/api/config', headers=self.auth).json()['production']
        self.assertTrue(config['modes']['narrated_promo']['ready'])
        self.assertTrue(config['modes']['brand_promo']['ready'])
        self.assertFalse(config['modes']['talking_head']['ready'])
        self.assertEqual(config['voices'][0]['language'], 'zh')
        self.assertEqual(self.create(voice_id='not-a-real-voice').status_code, 400)
        self.assertEqual(self.create(mode='talking_head', sources=[], presenter={'kind': 'personal',
            'portrait_id': 'portrait123', 'consent_confirmed': True}).status_code, 409)
        self.assertEqual(self.create(sources=[]).status_code, 400)
        self.voice_state['configured'] = False
        self.assertEqual(self.create().status_code, 503)
        self.assertEqual(self.store.list(), [])

    def test_schema_requires_real_script_and_mode_specific_options(self):
        for change in ({'spoken_script': ''}, {'spoken_script': '字' * 1601}, {'voice_id': ''},
                       {'visual_fill': 'automatic'}, {'brand': {'website': 'https://invalid'}},
                       {'brand': {'event_place': '位置\n'}}, {'subtype': 'salon'}, {'options': {'subtitles': 'true'}},
                       {'options': {'voice_style': 'cloned'}}, {'options': {'source_audio': 'full'}},
                       {'options': {'pace': 'compact'}}, {'mode': 'edit'}):
            with self.subTest(change=change):
                self.assertEqual(self.create(**change).status_code, 422)
        self.assertEqual(self.store.list(), [])

    def test_v2_snapshots_every_field_and_keeps_full_speech_duration(self):
        response = self.create(mode='brand_promo', subtype='salon_recap', visual_fill='loop',
            brand={'name': '用户品牌', 'cta': '欢迎了解', 'event_date': '已提供日期', 'event_place': '已提供地点'},
            options={'subtitles': True, 'voice_style': 'warm', 'source_audio': 'mute'})
        self.assertEqual(response.status_code, 201, response.text)
        job = response.json()['job']
        self.assertEqual(job['settings']['creation']['version'], 2)
        self.assertEqual(job['mode'], 'brand_promo')
        self.assertEqual(job['production']['stage'], 'queued')
        self.assertEqual(job['brand']['name'], '用户品牌')
        self.app.state.worker.process(self.store.claim())
        record = self.store.get(job['id'])
        self.assertEqual(record['status'], 'review')
        self.assertEqual(record['result']['duration'], 18.2)
        self.assertEqual(record['production']['narration_duration'], 18.2)
        self.assertEqual(record['production']['stage'], 'ready')
        arguments = self.build.call_args
        self.assertEqual(arguments.args[1], job['spoken_script'])
        self.assertEqual(arguments.args[2], 'zh_female')
        self.assertEqual(arguments.args[3]['target_duration'], 10)
        self.assertEqual(arguments.args[3]['visual_fill'], 'loop')
        self.assertEqual(arguments.args[3]['source_audio'], 'mute')
        self.assertEqual(arguments.args[3]['aspect'], '9:16')
        self.assertTrue(arguments.args[3]['music'])
        self.assertTrue(Path(arguments.kwargs['work_dir']).is_relative_to((self.root / 'data' / 'production').resolve()))
        self.assertEqual(Store(self.root / 'data' / 'studio.sqlite3').get(job['id'])['production']['stage'], 'ready')

    def test_idempotency_covers_script_mode_voice_brand_and_fill(self):
        response = self.create()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.create().status_code, 200)
        for change in ({'spoken_script': '这是另外一份明确修改过的讲稿。'}, {'mode': 'brand_promo'},
                       {'voice_id': 'zh_male'}, {'brand': {'name': '另一品牌'}},
                       {'subtype': 'corporate'}, {'visual_fill': 'loop'}):
            with self.subTest(change=change):
                self.assertEqual(self.create(**change).status_code, 409)
        self.assertEqual(len(self.store.list()), 1)

    def test_all_sources_checked_and_insufficient_visuals_is_explicit_failure(self):
        self.create()
        claimed = self.store.claim()
        error = RuntimeError('画面不足以覆盖完整旁白，请补充素材或明确允许循环。')
        error.code = 'insufficient_visuals'
        self.build.side_effect = error
        self.app.state.worker.process(claimed)
        record = self.store.get(claimed['id'])
        self.assertEqual(record['status'], 'failed')
        self.assertEqual(record['production']['error_code'], 'insufficient_visuals')
        self.render.assert_not_called()
        (self.root / 'inbox' / '镜头二.mp4').write_bytes(b'changed-version')
        self.assertEqual(self.client.post('/api/jobs/' + claimed['id'] + '/retry', headers=self.auth).status_code, 409)

    def test_source_changed_while_speech_preparing_blocks_render(self):
        self.create()
        def changing(*args, **kwargs):
            (self.root / 'inbox' / '镜头二.mp4').write_bytes(b'changed-during-voice')
            return self.plan(*args, **kwargs)
        self.build.side_effect = changing
        claimed = self.store.claim()
        self.app.state.worker.process(claimed)
        self.assertEqual(self.store.get(claimed['id'])['status'], 'failed')
        self.render.assert_not_called()

    def test_registered_diagnostic_rejected_and_restart_preserves_v2(self):
        source = self.client.post('/api/jobs', headers=self.auth, json={'source': '镜头二.mp4'}).json()['job']
        self.store.transition(source['id'], ['queued'], 'failed', 'fixture')
        self.store.mark_technical_test(source['id'], 'Known synthetic technical test')
        self.assertEqual(self.create().status_code, 409)
        response = self.create(sources=['镜头一.mp4'])
        job = response.json()['job']
        self.store.claim()
        self.store.transition(job['id'], ['analyzing'], 'analyzing', 'preparing',
                              production={**job['production'], 'stage': 'preparing_voice'})
        self.store.recover()
        record = self.store.get(job['id'])
        self.assertEqual(record['status'], 'queued')
        self.assertEqual(record['production']['stage'], 'queued')
        self.assertEqual(record['settings']['creation']['spoken_script'], job['spoken_script'])


if __name__ == '__main__':
    unittest.main()
