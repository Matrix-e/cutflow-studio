"""Compatibility previews never replace or alter official deliverables."""
import hashlib
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from studio.preview import PreviewError, PreviewManager, _probe, render_preview
from studio.server import create_app


def wait_status(manager, job_id, source, expected, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = manager.status(job_id, source)
        if result['status'] == expected:
            return result
        time.sleep(0.01)
    raise AssertionError(f'Expected {expected}, received {result}')


class PreviewAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = create_app(data_dir=self.root / 'data', inbox=self.root / 'inbox', token='preview-test', start_worker=False)
        self.client = TestClient(self.app)
        self.auth = {'Authorization': 'Bearer preview-test'}
        (self.root / 'inbox' / 'source.mp4').write_bytes(b'original-source')
        job = self.client.post('/api/jobs', headers=self.auth, json={'source': 'source.mp4'}).json()['job']
        self.job_id = job['id']
        self.source = self.root / 'data' / 'renders' / (self.job_id + '.mp4')
        self.source.write_bytes(b'official-render-never-modify')
        self.store = self.app.state.store
        self.store.transition(self.job_id, ['queued'], 'review', 'fixture', output_path=str(self.source),
                              result={'qc': {'passed': True}})
        self.manager = self.app.state.previews
        self.route = f'/api/jobs/{self.job_id}/preview'

    def tearDown(self):
        self.manager.stop()
        self.client.close()
        self.temp.cleanup()

    def test_preview_routes_require_auth_and_never_accept_arbitrary_file_selector(self):
        self.assertTrue(self.client.get('/api/config', headers=self.auth).json()['preview_supported'])
        for method in ('get', 'post'):
            self.assertEqual(getattr(self.client, method)(self.route).status_code, 401)
        self.assertEqual(self.client.get(self.route, params={'token': 'preview-test'}).status_code, 401)
        self.assertEqual(self.client.get(self.route, headers=self.auth).json(), {'status': 'absent'})
        self.assertEqual(self.client.get(f'/api/jobs/{self.job_id}/video?preview=../../private', headers=self.auth).status_code, 422)
        self.assertEqual(self.client.get(f'/api/jobs/{self.job_id}/video?preview=webm', headers=self.auth).status_code, 404)
        self.assertEqual(list(self.manager.cache.iterdir()), [])

    def test_duplicate_post_is_one_background_render_with_range_and_original_unchanged(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def renderer(source, temporary, stop):
            calls.append(source)
            entered.set()
            if not release.wait(3):
                raise PreviewError('test timeout')
            temporary.write_bytes(b'compatible-webm-payload')
        self.manager.renderer = renderer
        original = self.source.read_bytes()
        history = self.store.events(self.job_id)
        try:
            started = self.client.post(self.route, headers=self.auth)
            self.assertEqual(started.status_code, 200)
            self.assertEqual(started.json()['status'], 'queued')
            self.assertTrue(entered.wait(2))
            self.assertEqual(self.client.post(self.route, headers=self.auth).json()['status'], 'rendering')
            self.assertEqual(self.client.get(self.route, headers=self.auth).json()['status'], 'rendering')
            self.assertEqual(len(calls), 1)
            self.assertIsNone(self.manager.video_path(self.job_id, self.source))
        finally:
            release.set()
        ready = wait_status(self.manager, self.job_id, self.source, 'ready')
        self.assertEqual(ready['video_url'], f'/api/jobs/{self.job_id}/video?preview=webm')
        response = self.client.get(ready['video_url'], headers={**self.auth, 'Range': 'bytes=0-9'})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, b'compatible')
        self.assertEqual(response.headers['content-type'], 'video/webm')
        self.assertEqual(self.client.post(self.route, headers=self.auth).json(), ready)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual(self.store.get(self.job_id)['status'], 'review')
        self.assertEqual(self.store.events(self.job_id), history)
        self.assertEqual(self.client.get(f'/api/jobs/{self.job_id}/video', headers=self.auth).content, original)

    def test_failure_is_not_retried_or_written_to_job_and_restart_is_absent(self):
        self.manager.renderer = Mock(side_effect=PreviewError('测试编码失败'))
        self.client.post(self.route, headers=self.auth)
        failed = wait_status(self.manager, self.job_id, self.source, 'failed')
        self.assertEqual(failed['error'], '测试编码失败')
        for _ in range(3):
            self.assertEqual(self.client.post(self.route, headers=self.auth).json(), failed)
        self.assertEqual(self.manager.renderer.call_count, 1)
        self.assertEqual(self.store.get(self.job_id)['status'], 'review')
        self.assertIsNone(self.store.get(self.job_id)['error'])
        self.assertEqual(list(self.manager.cache.iterdir()), [])
        restarted = PreviewManager(self.root / 'data')
        self.assertEqual(restarted.status(self.job_id, self.source), {'status': 'absent'})

    def test_new_render_version_cannot_return_old_preview_and_ready_cache_survives_restart(self):
        self.manager.renderer = Mock(side_effect=lambda source, temporary, stop: temporary.write_bytes(source.read_bytes()))
        self.client.post(self.route, headers=self.auth)
        wait_status(self.manager, self.job_id, self.source, 'ready')
        first = self.manager.video_path(self.job_id, self.source)
        restarted = PreviewManager(self.root / 'data')
        self.assertEqual(restarted.status(self.job_id, self.source)['status'], 'ready')
        self.source.write_bytes(b'a-new-render-version')
        self.assertEqual(self.client.get(self.route, headers=self.auth).json(), {'status': 'absent'})
        self.assertEqual(self.client.get(f'/api/jobs/{self.job_id}/video?preview=webm', headers=self.auth).status_code, 404)
        self.client.post(self.route, headers=self.auth)
        wait_status(self.manager, self.job_id, self.source, 'ready')
        second = self.manager.video_path(self.job_id, self.source)
        self.assertNotEqual(first, second)
        self.assertEqual(second.read_bytes(), b'a-new-render-version')
        self.assertEqual(self.manager.renderer.call_count, 2)

    def test_source_changed_during_render_is_not_committed(self):
        entered, release = threading.Event(), threading.Event()
        def renderer(source, temporary, stop):
            temporary.write_bytes(b'old-version-preview')
            entered.set()
            release.wait(3)
        self.manager.renderer = renderer
        try:
            self.client.post(self.route, headers=self.auth)
            self.assertTrue(entered.wait(2))
            self.source.write_bytes(b'new-official-render')
            self.assertEqual(self.manager.status(self.job_id, self.source)['status'], 'absent')
        finally:
            release.set()
        self.manager.pending.join()
        self.assertIsNone(self.manager.video_path(self.job_id, self.source))
        self.assertEqual(list(self.manager.cache.iterdir()), [])

    def test_technical_tests_can_preview_but_remain_unpublishable(self):
        self.store.mark_technical_test(self.job_id, 'Synthetic test evidence')
        self.manager.renderer = lambda source, temporary, stop: temporary.write_bytes(b'webm')
        self.assertEqual(self.client.post(self.route, headers=self.auth).status_code, 200)
        wait_status(self.manager, self.job_id, self.source, 'ready')
        for action in ('approve', 'publish', 'retry'):
            self.assertEqual(self.client.post(f'/api/jobs/{self.job_id}/{action}', headers=self.auth).status_code, 409)
        self.assertEqual(self.store.get(self.job_id)['job_kind'], 'technical_test')

    def test_cache_space_limit_fails_preview_without_invoking_encoder(self):
        self.manager.renderer = Mock()
        (self.manager.cache / 'existing.webm').write_bytes(b'x' * 600)
        with patch('studio.preview.MAX_CACHE_BYTES', 1000), patch('studio.preview.MAX_PREVIEW_BYTES', 512):
            self.client.post(self.route, headers=self.auth)
            result = wait_status(self.manager, self.job_id, self.source, 'failed')
        self.assertIn('空间限制', result['error'])
        self.manager.renderer.assert_not_called()
        self.assertEqual(self.store.get(self.job_id)['status'], 'review')

    def test_one_worker_serializes_different_previews(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        other = self.source.with_name('other.mp4')
        other.write_bytes(b'another-render')
        def renderer(source, temporary, stop):
            calls.append(source.name)
            if len(calls) == 1:
                entered.set()
                release.wait(3)
            temporary.write_bytes(b'webm')
        self.manager.renderer = renderer
        try:
            self.manager.request(self.job_id, self.source)
            self.assertTrue(entered.wait(2))
            self.assertEqual(self.manager.request('other', other)['status'], 'queued')
            self.assertEqual(calls, [self.source.name])
        finally:
            release.set()
        wait_status(self.manager, 'other', other, 'ready')
        self.assertEqual(calls, [self.source.name, 'other.mp4'])


class PreviewEncodingTests(unittest.TestCase):
    def test_encoding_deadline_terminates_process_without_modifying_input(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'original.mp4'
            source.write_bytes(b'render')
            process = Mock()
            process.poll.return_value = None
            process.terminate.side_effect = lambda: setattr(process.poll, 'return_value', -15)
            with patch('studio.preview.shutil.which', return_value='ffmpeg'), \
                 patch('studio.preview._probe', return_value={'duration': 1, 'audio': None}), \
                 patch('studio.preview.subprocess.Popen', return_value=process), \
                 patch('studio.preview.time.monotonic', side_effect=[0, 301]):
                with self.assertRaisesRegex(PreviewError, '5 分钟'):
                    render_preview(source, Path(folder) / 'temporary.webm', threading.Event())
            process.terminate.assert_called_once()
            self.assertEqual(source.read_bytes(), b'render')

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
    def test_real_preview_is_complete_vp9_opus_and_source_is_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder) / 'original.mp4', Path(folder) / 'preview.webm'
            subprocess.run(['ffmpeg', '-hide_banner', '-v', 'error', '-y',
                            '-f', 'lavfi', '-i', 'testsrc2=s=640x360:r=15:d=1.2',
                            '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=1.2',
                            '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(source)],
                           capture_output=True, check=True, timeout=30)
            before = hashlib.sha256(source.read_bytes()).hexdigest()
            render_preview(source, target, threading.Event())
            info = _probe(target)
            self.assertEqual(info['video']['codec_name'], 'vp9')
            self.assertEqual(info['audio']['codec_name'], 'opus')
            self.assertEqual((info['video']['width'], info['video']['height']), (640, 360))
            self.assertAlmostEqual(info['duration'], 1.2, delta=0.15)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)
            subprocess.run(['ffmpeg', '-v', 'error', '-i', str(target), '-f', 'null', '-'],
                           capture_output=True, check=True, timeout=30)


if __name__ == '__main__':
    unittest.main()
