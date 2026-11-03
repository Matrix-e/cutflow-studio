from contextlib import closing, ExitStack
import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import uuid

import httpx
from fastapi.testclient import TestClient

from studio.local_gateway import create_app
from studio.local_library import ImportProblem, LocalLibrary, probe_video


class VideoStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'video'


class LocalImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.temp.name)
        self.inbox, self.data = self.root / '用户素材', self.root / 'data'
        self.web = self.root / 'web'
        self.web.mkdir()
        (self.web / 'index.html').write_text('local', encoding='utf-8')
        self.target = {'CUTFLOW_SSH_HOST': 'gpu.example', 'CUTFLOW_SSH_PORT': '10893',
                       'CUTFLOW_SSH_USER': 'root', 'CUTFLOW_SSH_REMOTE': '/root/inbox'}
        self.calls = []

        def upstream(request):
            self.calls.append(request)
            if request.url.path.endswith('/video'):
                return httpx.Response(206, stream=VideoStream(), headers={'Content-Range': 'bytes 0-4/10'})
            raise httpx.ConnectError('GPU offline')

        self.app = create_app(gpu_token='private-gpu-token-is-not-a-browser-token', local_token='desktop-test-token',
                              upstream_transport=httpx.MockTransport(upstream), web_root=self.web,
                              inbox=self.inbox, data_dir=self.data, sync_target=self.target)
        self.client = self.stack.enter_context(TestClient(self.app, base_url='http://127.0.0.1:8767'))
        self.headers = {'Authorization': 'Bearer desktop-test-token', 'Content-Type': 'application/octet-stream'}
        self.probe = self.stack.enter_context(patch('studio.local_library.probe_video'))

    def upload(self, identifier=None, name='公司视频.mp4', mode='preserve', content=b'valid-video', **kwargs):
        return self.client.post('/local-api/import', params={'id': identifier or str(uuid.uuid4()), 'filename': name, 'mode': mode},
                                headers=self.headers, content=content, **kwargs)

    def test_offline_import_receipt_and_atomic_mode_directory(self):
        identifier = str(uuid.uuid4())
        response = self.upload(identifier)
        self.assertEqual(response.status_code, 200, response.text)
        receipt = response.json()['import']
        self.assertEqual(receipt['source'], f'工作台导入/preserve/{identifier}/公司视频.mp4')
        self.assertEqual(receipt['status'], 'waiting_sync')
        self.assertEqual(receipt['sha256'], hashlib.sha256(b'valid-video').hexdigest())
        self.assertEqual((self.inbox / receipt['source']).read_bytes(), b'valid-video')
        self.assertFalse(list(self.inbox.rglob('*.part')))
        self.assertEqual(self.calls, [])
        listing = self.client.get('/local-api/library', headers=self.headers).json()
        self.assertEqual(listing['imports'], [receipt])
        self.assertEqual(listing['max_bytes'], 2 * 1024**3)

    def test_same_id_replay_is_duplicate_but_changed_bytes_or_mode_conflict(self):
        identifier = str(uuid.uuid4())
        first = self.upload(identifier).json()['import']
        retry = self.upload(identifier)
        self.assertTrue(retry.json()['duplicate'])
        self.assertEqual(self.upload(identifier, content=b'changed-video').status_code, 409)
        self.assertEqual(self.upload(identifier, mode='gentle').status_code, 409)
        self.assertEqual((self.inbox / first['source']).read_bytes(), b'valid-video')
        self.assertEqual(len(self.client.get('/local-api/library', headers=self.headers).json()['imports']), 1)
        self.assertFalse(list(self.inbox.rglob('*.part')))

    def test_invalid_video_and_incomplete_stream_never_enter_media_queue(self):
        self.probe.side_effect = ImportProblem('视频检查未通过')
        self.assertEqual(self.upload().status_code, 400)
        self.assertFalse(list(self.inbox.rglob('*.mp4')))
        self.assertFalse(list(self.inbox.rglob('*.part')))
        self.assertEqual(self.client.get('/local-api/library', headers=self.headers).json()['imports'], [])
        library = LocalLibrary(self.inbox, self.data, target=self.target)
        stream = library.begin(str(uuid.uuid4()), 'incomplete.mp4', 'gentle', 10)
        try:
            stream.write(b'partial')
            with self.assertRaises(ImportProblem):
                stream.finish()
        finally:
            stream.close()
        self.assertFalse(list(self.inbox.rglob('*.mp4')))

    def test_windows_reserved_and_traversal_names_and_modes_are_rejected(self):
        for name in ('../bad.mp4', 'C:\\bad.mp4', 'CON.mp4', 'COM1.mp4', 'a.mp4:evil', '.hidden.mp4', 'bad.mp4.', 'a.jpg', 'x\x00.mp4'):
            with self.subTest(name=name):
                self.assertEqual(self.upload(name=name).status_code, 400)
        self.assertEqual(self.upload(mode='../preserve').status_code, 400)
        self.assertEqual(self.upload(identifier='not-an-id').status_code, 400)
        self.assertFalse(list(self.inbox.rglob('*.mp4')) if self.inbox.exists() else [])

    def test_auth_and_cross_origin_block_without_writing(self):
        self.assertEqual(self.client.get('/local-api/library').status_code, 401)
        params = {'filename': 'a.mp4', 'mode': 'gentle', 'id': str(uuid.uuid4())}
        self.assertEqual(self.client.post('/local-api/import', params=params, content=b'a', headers={'Content-Type': 'application/octet-stream'}).status_code, 401)
        for headers in ({'Origin': 'https://evil.example'}, {'Sec-Fetch-Site': 'cross-site'}, {'Host': 'evil.example:8767'}):
            response = self.client.post('/local-api/import', params=params, content=b'a', headers={**self.headers, **headers})
            self.assertEqual(response.status_code, 403)
        self.assertFalse(self.inbox.exists())

    def test_chunked_limit_and_disk_shortage_cleanup(self):
        library = LocalLibrary(self.inbox, self.data, target=self.target, max_bytes=10)
        stream = library.begin(str(uuid.uuid4()), 'too-large.mp4', 'gentle')
        try:
            stream.write(b'123456')
            with self.assertRaises(ImportProblem) as raised:
                stream.write(b'78901')
            self.assertEqual(raised.exception.status, 413)
        finally:
            stream.close()
        self.assertFalse(list(self.inbox.rglob('*.part')))
        self.assertFalse(list(self.inbox.rglob('*.mp4')))
        with patch('studio.local_library.shutil.disk_usage') as disk:
            disk.return_value.free = 1
            self.assertEqual(self.upload().status_code, 507)

    def test_same_id_concurrent_writer_is_rejected(self):
        library = LocalLibrary(self.inbox, self.data, target=self.target)
        identifier = str(uuid.uuid4())
        first = library.begin(identifier, 'first.mp4', 'preserve')
        try:
            with self.assertRaises(ImportProblem) as raised:
                library.begin(identifier, 'first.mp4', 'preserve')
            self.assertEqual(raised.exception.status, 409)
        finally:
            first.close()

    def test_existing_media_after_receipt_commit_crash_is_reconciled(self):
        library = LocalLibrary(self.inbox, self.data, target=self.target)
        identifier = str(uuid.uuid4())
        upload = library.begin(identifier, 'a.mp4', 'preserve')
        upload.write(b'video')
        upload.stream.flush()
        os.link(upload.temporary, upload.directory / 'a.mp4')
        upload.close()
        retry = library.begin(identifier, 'a.mp4', 'preserve')
        try:
            retry.write(b'video')
            self.assertTrue(retry.finish()['duplicate'])
        finally:
            retry.close()
        self.assertEqual(len(library.listing()['imports']), 1)

    def test_sync_status_matches_current_server_hash_and_remote_root(self):
        receipt = self.upload().json()['import']
        library = LocalLibrary(self.inbox, self.data, target=self.target)
        with closing(sqlite3.connect(self.data / 'sync.sqlite3')) as con, con:
            con.execute('CREATE TABLE transfers_v2(relative_path,sha256,target_id,destination)')
            con.execute('INSERT INTO transfers_v2 VALUES(?,?,?,?)', (receipt['source'], receipt['sha256'], 'other-server', '/root/inbox/other.mp4'))
        self.assertEqual(library.listing()['imports'][0]['status'], 'waiting_sync')
        with closing(sqlite3.connect(self.data / 'sync.sqlite3')) as con, con:
            con.execute('INSERT INTO transfers_v2 VALUES(?,?,?,?)', (receipt['source'], receipt['sha256'], library.target_id, '/outside/inbox/a.mp4'))
        self.assertEqual(library.listing()['imports'][0]['status'], 'waiting_sync')
        remote = receipt['source'].replace('.mp4', '--' + receipt['sha256'][:16] + '.mp4')
        with closing(sqlite3.connect(self.data / 'sync.sqlite3')) as con, con:
            con.execute('UPDATE transfers_v2 SET destination=? WHERE target_id=?', ('/root/inbox/' + remote, library.target_id))
        result = library.listing()['imports'][0]
        self.assertEqual(result['status'], 'synced')
        self.assertEqual(result['remote_source'], remote)

    def test_directory_link_is_rejected(self):
        self.inbox.mkdir()
        outside = self.root / 'outside'
        outside.mkdir()
        try:
            (self.inbox / '工作台导入').symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('Symlink creation is unavailable on this host')
        self.assertEqual(self.upload().status_code, 409)
        self.assertFalse(list(outside.rglob('*')))

    def test_probe_rejects_disguised_non_video(self):
        with patch('studio.local_library.shutil.which', return_value='ffprobe'), patch('studio.local_library.subprocess.run') as process:
            process.return_value.returncode = 0
            process.return_value.stdout = b'{"streams":[{"codec_type":"audio"}],"format":{"duration":"10"}}'
            with self.assertRaises(ImportProblem):
                probe_video(self.root / 'fake.mp4')

    def test_probe_accepts_format_duration_when_stream_duration_is_unavailable(self):
        with patch('studio.local_library.shutil.which', return_value='ffprobe'), patch('studio.local_library.subprocess.run') as process:
            process.return_value.returncode = 0
            process.return_value.stdout = b'{"streams":[{"codec_type":"video","duration":"N/A","width":1920,"height":1080}],"format":{"duration":"10"}}'
            probe_video(self.root / 'valid.mkv')

    def test_session_cookie_only_authorizes_media_get_and_range(self):
        session = self.client.get('/local-session', headers={'X-CutFlow-Local': '1'})
        self.assertTrue(session.json()['media_streaming'])
        cookie = session.headers['set-cookie']
        for expected in ('HttpOnly', 'SameSite=strict', 'Path=/api'):
            self.assertIn(expected, cookie)
        self.assertNotIn('Max-Age', cookie)
        for path in ('/api/assets/video?source=a.mp4', '/api/jobs/job1/video'):
            response = self.client.get(path, headers={'Range': 'bytes=0-4'})
            self.assertEqual(response.status_code, 206)
            self.assertEqual(response.headers['content-range'], 'bytes 0-4/10')
            self.assertEqual(self.calls[-1].headers['Range'], 'bytes=0-4')
            self.assertNotIn('cookie', self.calls[-1].headers)
        for path in ('/api/status', '/api/jobs', '/api/jobs/job1/video/extra'):
            self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.client.post('/api/jobs/job1/video').status_code, 401)
        self.assertEqual(self.client.get('/local-api/library').status_code, 401)
        self.assertEqual(self.client.get('/api/jobs/job1/video', headers={'Origin': 'https://evil.example'}).status_code, 403)
        self.client.cookies.set('cutflow_media', 'old-gateway-token', domain='127.0.0.1', path='/api')
        self.assertEqual(self.client.get('/api/jobs/job1/video').status_code, 401)


if __name__ == '__main__':
    unittest.main()
