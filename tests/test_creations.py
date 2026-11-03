"""Multi-source creation contracts, version safety, and durable explanations."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from studio.server import create_app
from studio.store import DiagnosticSourceConflict, Store


class CreationAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.inbox = self.root / 'inbox'
        self.app = create_app(data_dir=self.root / 'data', inbox=self.inbox, token='creation-test', start_worker=False)
        self.client = TestClient(self.app)
        self.auth = {'Authorization': 'Bearer creation-test'}
        self.store = self.app.state.store
        self.sources = ['开场.mp4', '中段.mov', '结尾.mp4']
        for index, name in enumerate(self.sources):
            (self.inbox / name).write_bytes(('source-version-' + str(index)).encode())
        self.interpret = Mock(side_effect=lambda brief, options: {'resolved': {'opening_title': options.get('opening_title', ''),
            'pace': options.get('pace', 'preserve')}, 'summary': '按想法生成：' + brief,
            'warnings': ['不支持自动理解镜头内容；按选择顺序组织素材。']})
        self.build = Mock(side_effect=lambda paths, brief, options, settings: {'resolved': {'aspect': options.get('aspect', 'original')},
            'summary': '已分析所选素材', 'duration': 6, 'sources': paths, 'warnings': ['实际切点需要审片。']})
        self.render = Mock(side_effect=self.fake_render)
        self.creative_patch = patch.dict(sys.modules, {'studio.creative': types.SimpleNamespace(
            interpret_brief=self.interpret, build_creation_plan=self.build, render_creation_plan=self.render)})
        self.creative_patch.start()

    def tearDown(self):
        self.creative_patch.stop()
        self.app.state.previews.stop()
        self.client.close()
        self.temp.cleanup()

    @staticmethod
    def fake_render(plan, output, settings):
        Path(output).write_bytes(b'generated-test-video')
        return {'output_path': output, 'duration': 6, 'qc': {'passed': True}, 'warnings': ['生成完毕后需核实内容。']}

    def create(self, **changes):
        body = {'sources': self.sources[:2], 'brief': '横版完整保留，片头标题为“门店介绍”',
                'options': {}, 'idempotency_key': 'request-1', **changes}
        return self.client.post('/api/creations', headers=self.auth, json=body)

    def make_diagnostic(self, source):
        record = self.client.post('/api/jobs', headers=self.auth, json={'source': source}).json()['job']
        self.store.transition(record['id'], ['queued'], 'failed', 'fixture')
        self.store.mark_technical_test(record['id'], 'Verified synthetic test input')

    def test_authorization_capability_and_strict_input_schema(self):
        self.assertTrue(self.client.get('/api/config', headers=self.auth).json()['creation_supported'])
        self.assertEqual(self.client.post('/api/creations', json={}).status_code, 401)
        invalid = [{'sources': []}, {'sources': ['x.mp4'] * 13}, {'brief': '   '}, {'brief': 'x' * 3001},
                   {'idempotency_key': ' '}, {'options': {'music': 'light'}}, {'options': {'unknown': True}},
                   {'options': {'duration': 30}}, {'options': {'target_duration': True}},
                   {'options': {'target_duration': '30'}}, {'options': {'target_duration': 3601}},
                   {'options': {'aspect': 'auto'}}, {'options': {'keep_original_audio': 'false'}},
                   {'options': {'opening_title': '字' * 121}}, {'settings': {'creation': {}}}]
        for values in invalid:
            with self.subTest(values=values):
                self.assertEqual(self.create(**values).status_code, 422)
        self.assertEqual(self.store.list(), [])

    def test_all_paths_are_local_supported_nonempty_and_unique(self):
        (self.inbox / 'empty.mp4').touch()
        (self.inbox / 'notes.txt').write_text('not a video')
        for source in ('../outside.mp4', 'C:/outside.mp4', '/outside.mp4', 'missing.mp4', 'empty.mp4', 'notes.txt'):
            with self.subTest(source=source):
                self.assertEqual(self.create(sources=[self.sources[0], source]).status_code, 400)
        self.assertEqual(self.create(sources=[self.sources[0], self.sources[0]]).status_code, 400)
        link = self.inbox / 'alias.mp4'
        try:
            link.symlink_to(self.inbox / self.sources[0])
        except OSError:
            return
        self.assertEqual(self.create(sources=[self.sources[0], link.name]).status_code, 400)

    def test_ordered_versions_brief_options_and_explanation_are_persistent(self):
        options = {'target_duration': 30, 'aspect': '9:16', 'music': True, 'opening_title': '门店介绍'}
        response = self.create(sources=self.sources[::-1], options=options)
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertTrue(body['created'])
        self.assertTrue(body['owned_by_request'])
        job = body['job']
        self.assertEqual(job['sources'], self.sources[::-1])
        self.assertEqual(job['brief'], '横版完整保留，片头标题为“门店介绍”')
        self.assertEqual(job['title'], job['brief'][:30])
        self.assertEqual(job['creation_plan']['stage'], 'interpreted')
        self.assertEqual(job['creation_plan']['resolved']['opening_title'], '门店介绍')
        self.assertIn('不支持', job['creation_warnings'][0])
        persisted = Store(self.root / 'data' / 'studio.sqlite3').get(job['id'])
        creation = persisted['settings']['creation']
        self.assertEqual(creation['options'], options)
        self.assertEqual(creation['version'], 1)
        for entry in creation['sources']:
            stat = (self.inbox / entry['source']).stat()
            self.assertEqual((entry['size'], entry['mtime_ns'], entry['ctime_ns']), (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
        self.assertEqual(self.client.get('/api/jobs', headers=self.auth).json()['jobs'][0]['sources'], self.sources[::-1])

    def test_idempotency_covers_brief_options_order_and_every_source_version(self):
        first = self.create().json()['job']
        replay = self.create()
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json()['job']['id'], first['id'])
        self.assertFalse(replay.json()['created'])
        for change in ({'brief': '另一种要求'}, {'options': {'music': True}}, {'sources': self.sources[:2][::-1]}, {'title': '另一标题'}):
            self.assertEqual(self.create(**change).status_code, 409)
        (self.inbox / self.sources[1]).write_bytes(b'changed-second-source')
        self.assertEqual(self.create().status_code, 409)
        self.assertEqual(len(self.store.list()), 1)
        fresh = self.create(idempotency_key='new-version')
        self.assertEqual(fresh.status_code, 201)
        self.assertNotEqual(fresh.json()['job']['id'], first['id'])

    def test_technical_test_in_nonfirst_source_is_rejected_atomically(self):
        self.make_diagnostic(self.sources[1])
        response = self.create()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'technical_test_source')
        with self.assertRaises(DiagnosticSourceConflict):
            self.store.create(source=self.sources[0], title='bypass attempt',
                              settings={'creation': {'sources': [{'source': self.sources[1]}]}},
                              fingerprint='cannot-bypass-store', source_size=1, source_mtime_ns=1)
        self.assertEqual(self.store.list(), [])

    def test_worker_passes_all_sources_and_brief_and_exposes_actual_plan(self):
        options = {'aspect': '9:16', 'music': True}
        created = self.create(options=options).json()['job']
        self.app.state.worker.process(self.store.claim())
        paths, brief, actual_options, settings = self.build.call_args.args
        self.assertEqual(paths, [str((self.inbox / name).resolve()) for name in self.sources[:2]])
        self.assertEqual(brief, created['brief'])
        self.assertEqual(actual_options, options)
        self.assertNotIn('creation', settings)
        self.render.assert_called_once()
        job = self.client.get('/api/jobs/' + created['id'], headers=self.auth).json()['job']
        self.assertEqual(job['status'], 'review')
        self.assertEqual(job['creation_plan']['stage'], 'planned')
        self.assertEqual(job['creation_plan']['duration'], 6)
        self.assertEqual(job['creation_plan']['resolved']['aspect'], '9:16')
        self.assertEqual(len(job['creation_warnings']), 3)
        self.assertEqual([e['status'] for e in job['events']], ['queued', 'analyzing', 'rendering', 'review'])

    def test_second_source_changed_after_queue_blocks_execution_and_retry(self):
        job = self.create().json()['job']
        (self.inbox / self.sources[1]).write_bytes(b'new-second-version')
        self.app.state.worker.process(self.store.claim())
        self.build.assert_not_called()
        self.render.assert_not_called()
        failed = self.client.get('/api/jobs/' + job['id'], headers=self.auth).json()['job']
        self.assertEqual(failed['status'], 'failed')
        self.assertFalse(failed['can_retry'])
        self.assertEqual(self.client.post('/api/jobs/' + job['id'] + '/retry', headers=self.auth).status_code, 409)

    def test_secondary_source_change_during_analysis_prevents_render(self):
        job = self.create().json()['job']
        def changed(*args):
            (self.inbox / self.sources[1]).write_bytes(b'changed-after-analysis')
            return {'resolved': {}, 'warnings': []}
        self.build.side_effect = changed
        self.app.state.worker.process(self.store.claim())
        self.render.assert_not_called()
        self.assertEqual(self.store.get(job['id'])['status'], 'failed')

    def test_secondary_source_change_during_render_cannot_be_reviewed(self):
        job = self.create().json()['job']
        def changed(plan, output, settings):
            report = self.fake_render(plan, output, settings)
            (self.inbox / self.sources[1]).write_bytes(b'changed-during-render')
            return report
        self.render.side_effect = changed
        self.app.state.worker.process(self.store.claim())
        record = self.store.get(job['id'])
        self.assertEqual(record['status'], 'failed')
        self.assertIsNone(record['output_path'])
        self.assertEqual(self.client.post('/api/jobs/' + job['id'] + '/approve', headers=self.auth).status_code, 409)

    def test_retry_checks_every_version_and_keeps_existing_send_protection(self):
        job = self.create(sources=self.sources).json()['job']
        self.store.transition(job['id'], ['queued'], 'failed', 'fixture')
        response = self.client.post('/api/jobs/' + job['id'] + '/retry', headers=self.auth)
        self.assertEqual(response.status_code, 200)
        self.store.transition(job['id'], ['queued'], 'failed', 'fixture')
        (self.inbox / self.sources[2]).unlink()
        self.assertEqual(self.client.post('/api/jobs/' + job['id'] + '/retry', headers=self.auth).status_code, 409)

    def test_late_secondary_diagnostic_classification_blocks_approval(self):
        job = self.create().json()['job']
        self.app.state.worker.process(self.store.claim())
        self.make_diagnostic(self.sources[1])
        self.assertEqual(self.client.post('/api/jobs/' + job['id'] + '/approve', headers=self.auth).status_code, 409)
        self.assertEqual(self.store.get(job['id'])['status'], 'review')

    def test_creative_rejection_or_missing_explanation_never_queues_task(self):
        self.interpret.side_effect = ValueError('当前无法执行这条想法，请调整')
        response = self.create()
        self.assertEqual(response.status_code, 400)
        self.assertIn('无法执行', response.json()['detail'])
        self.interpret.side_effect = None
        self.interpret.return_value = {'warnings': []}
        self.assertEqual(self.create().status_code, 503)
        self.assertEqual(self.store.list(), [])


class RealCreationAPITests(unittest.TestCase):
    def test_actual_brief_choices_and_unsupported_requests_are_returned_before_render(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            app = create_app(data_dir=root / 'data', inbox=root / 'inbox', token='intent-test', start_worker=False)
            (root / 'inbox' / 'video.mp4').write_bytes(b'api-only-fixture')
            with TestClient(app) as client:
                auth = {'Authorization': 'Bearer intent-test'}
                payload = {'sources': ['video.mp4'], 'brief': '做成20秒竖版，加轻音乐，片头标题为“汽车托管”，自动生成字幕和旁白',
                           'idempotency_key': 'intent-request'}
                response = client.post('/api/creations', headers=auth, json=payload)
                self.assertEqual(response.status_code, 201, response.text)
                plan = response.json()['job']['creation_plan']
                self.assertEqual(plan['resolved']['target_duration'], 20)
                self.assertEqual(plan['resolved']['aspect'], '9:16')
                self.assertEqual(plan['resolved']['opening_title'], '汽车托管')
                self.assertTrue(plan['resolved']['music'])
                self.assertTrue(any('不会自动生成字幕' in warning for warning in plan['warnings']))
                self.assertTrue(any('不会新增或改写配音' in warning for warning in plan['warnings']))
                override = client.post('/api/creations', headers=auth, json={**payload,
                    'idempotency_key': 'explicit-override', 'options': {'aspect': '16:9', 'music': False}})
                resolved = override.json()['job']['creation_plan']['resolved']
                self.assertEqual(resolved['aspect'], '16:9')
                self.assertFalse(resolved['music'])

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
    def test_real_ordered_two_source_creation_reaches_review(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            app = create_app(data_dir=root / 'data', inbox=root / 'inbox', token='real-creation', start_worker=False)
            colors = [('first.mp4', 'red', 440), ('second.mp4', 'blue', 880)]
            for name, color, frequency in colors:
                subprocess.run(['ffmpeg', '-hide_banner', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                    f'color=c={color}:s=320x180:r=30:d=1.5', '-f', 'lavfi', '-i',
                    f'sine=frequency={frequency}:sample_rate=48000:duration=1.5',
                    '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', '-c:a', 'aac',
                    str(root / 'inbox' / name)], capture_output=True, check=True, timeout=30)
            with TestClient(app) as client, patch.dict(os.environ, {'CUTFLOW_SETTINGS': '{"encoder":"cpu"}'}):
                auth = {'Authorization': 'Bearer real-creation'}
                response = client.post('/api/creations', headers=auth, json={'sources': [x[0] for x in colors],
                    'brief': '按照素材顺序完整保留，保留原声，不要额外配乐', 'options': {'pace': 'preserve', 'music': False},
                    'idempotency_key': 'real-ordered-creation'})
                self.assertEqual(response.status_code, 201, response.text)
                job_id = response.json()['job']['id']
                app.state.worker.process(app.state.store.claim())
                job = app.state.store.get(job_id)
                self.assertEqual(job['status'], 'review', job.get('error'))
                self.assertTrue(job['result']['qc']['passed'])
                self.assertAlmostEqual(job['result']['duration'], 3, delta=0.15)
                # Decode representative pixels: selection order is observable,
                # not merely metadata copied from the request.
                pixels = []
                for second in (0.5, 2):
                    pixels.append(subprocess.run(['ffmpeg', '-v', 'error', '-ss', str(second), '-i', job['output_path'],
                        '-frames:v', '1', '-vf', 'scale=1:1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'],
                        capture_output=True, check=True, timeout=30).stdout)
                self.assertGreater(pixels[0][0], pixels[0][2] + 100)
                self.assertGreater(pixels[1][2], pixels[1][0] + 100)
                detail = client.get('/api/jobs/' + job_id, headers=auth).json()['job']
                self.assertEqual(detail['sources'], ['first.mp4', 'second.mp4'])
                self.assertEqual(detail['creation_plan']['stage'], 'planned')


if __name__ == '__main__':
    unittest.main()
