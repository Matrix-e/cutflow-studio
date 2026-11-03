import tempfile
import unittest
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from studio.watch import Watcher, validated_url
from studio.server import create_app


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.inbox = self.root / "inbox"
        self.inbox.mkdir()
        self.source = self.inbox / "speech.mp4"
        self.source.write_bytes(b"video")
        self.calls = []
        self.job_status = "review"
        self.job_kind = "production"
        self.send_timeout = False
        self.submit_timeout = False

    def tearDown(self):
        self.temp.cleanup()

    def handler(self, request):
        self.calls.append((request.method,request.url.path))
        if request.method == "POST" and request.url.path == "/api/jobs":
            if self.submit_timeout:
                raise httpx.ReadTimeout("redacted test")
            return httpx.Response(201,json={"job":{"id":"job1"},"owned_by_request":True})
        if request.url.path.endswith("/approve"):
            self.job_status = "approved"
        elif request.url.path.endswith("/publish"):
            if self.send_timeout:
                raise httpx.ReadTimeout("secret should not be logged")
            self.job_status = "published"
        return httpx.Response(200,json={"job":{"id":"job1","status":self.job_status,"job_kind":self.job_kind,"result":{"qc":{"passed":True}}}})

    def watcher(self, automatic=False):
        client = httpx.Client(transport=httpx.MockTransport(self.handler))
        self.addCleanup(client.close)
        return Watcher(self.inbox,self.root / "data","http://127.0.0.1:8765","test",auto_publish=automatic,client=client)

    def test_waits_for_stability_then_submits_once_across_restart(self):
        watch = self.watcher()
        watch.tick(now=0)
        watch.tick(now=14)
        self.assertEqual(self.calls,[])
        watch.tick(now=15)
        watch.tick(now=30)
        self.assertEqual(self.calls,[("POST","/api/jobs")])
        restarted = self.watcher()
        restarted.tick(now=40)
        restarted.tick(now=55)
        self.assertEqual(self.calls,[("POST","/api/jobs")])

    def test_changed_file_restarts_stability_and_ignores_part_files(self):
        (self.inbox / "upload.mp4.part").write_bytes(b"partial")
        watch = self.watcher()
        watch.tick(now=0)
        self.source.write_bytes(b"changed-video")
        watch.tick(now=15)
        self.assertEqual(self.calls,[])
        watch.tick(now=30)
        self.assertEqual(self.calls,[("POST","/api/jobs")])

    def test_default_never_approves_or_publishes(self):
        watch = self.watcher()
        watch.tick(now=0)
        watch.tick(now=15)
        watch.tick(now=45)
        self.assertFalse(any(path.endswith(("/approve","/publish")) for _,path in self.calls))

    def test_library_uploads_never_enqueue_even_with_auto_publish_enabled(self):
        self.source.unlink()
        library_file = self.inbox / '工作台导入/library/a/素材.mp4'
        library_file.parent.mkdir(parents=True)
        library_file.write_bytes(b'video')
        for start in (0, 40):
            watch = self.watcher(automatic=True)
            watch.tick(now=start)
            watch.tick(now=start + 15)
        self.assertEqual(self.calls, [])
        with watch.connection() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM submissions').fetchone()[0], 0)
        self.assertTrue(library_file.is_file())

    def test_old_library_submission_never_advances_automatic_approval(self):
        self.source.unlink()
        watch = self.watcher(automatic=True)
        with watch.connection() as con:
            con.execute("INSERT INTO submissions(request_key,source,sha256,settings,status,job_id,updated_at,owned_by_request) VALUES(?,?,?,?,?,?,?,?)",
                        ('old-library', '工作台导入/library/id/素材.mp4', 'hash', '{}', 'submitted', 'old-job', 0, 1))
        watch.tick(now=0)
        self.assertEqual(self.calls, [])
        with watch.connection() as con:
            row = con.execute("SELECT publish_state,error FROM submissions WHERE request_key='old-library'").fetchone()
        self.assertEqual(tuple(row), ('blocked', 'library_requires_generation'))

    def test_manual_active_job_is_not_automatically_approved(self):
        app = create_app(data_dir=self.root / "server-data",inbox=self.inbox,token="test",start_worker=False)
        with TestClient(app) as client:
            manual = client.post("/api/jobs",headers={"Authorization":"Bearer test"},json={"source":"speech.mp4"}).json()["job"]
            watch = Watcher(self.inbox,self.root / "watch-data","http://127.0.0.1:8765","test",auto_publish=True,client=client)
            watch.tick(now=0)
            records = watch.tick(now=15)
            self.assertEqual(records[0]["job_id"],manual["id"])
            self.assertFalse(records[0]["owned_by_request"])
            app.state.store.transition(manual["id"],["queued"],"review","fixture",result={"qc":{"passed":True}})
            watch.tick(now=30)
            self.assertEqual(app.state.store.get(manual["id"])["status"],"review")

    def test_missing_ownership_proof_never_autopublishes(self):
        calls = []
        def legacy_handler(request):
            calls.append((request.method,request.url.path))
            return httpx.Response(201,json={"job":{"id":"manual-job"},"created":False})
        with httpx.Client(transport=httpx.MockTransport(legacy_handler)) as client:
            watch = Watcher(self.inbox,self.root / "legacy-data","http://127.0.0.1:8765","test",auto_publish=True,client=client)
            watch.tick(now=0)
            watch.tick(now=15)
            watch.tick(now=30)
        self.assertEqual(calls,[("POST","/api/jobs")])

    def test_explicit_auto_publishes_only_once(self):
        watch = self.watcher(automatic=True)
        watch.tick(now=0)
        watch.tick(now=15)
        watch.tick(now=45)
        self.assertEqual(self.calls.count(("POST","/api/jobs/job1/publish")),1)

    def test_ambiguous_publish_never_retries_after_restart(self):
        self.send_timeout = True
        watch = self.watcher(automatic=True)
        watch.tick(now=0)
        watch.tick(now=15)
        self.send_timeout = False
        restarted = self.watcher(automatic=True)
        restarted.tick(now=30)
        restarted.tick(now=45)
        self.assertEqual(self.calls.count(("POST","/api/jobs/job1/publish")),1)

    def test_submission_timeout_can_retry_idempotently(self):
        self.submit_timeout = True
        watch = self.watcher()
        watch.tick(now=0)
        watch.tick(now=15)
        watch.tick(now=20)
        self.assertEqual(len(self.calls),1)
        self.submit_timeout = False
        watch.tick(now=30)
        watch.tick(now=45)
        self.assertEqual(len(self.calls),2)

    def test_unsafe_api_urls_rejected(self):
        for url in ("http://example.com", "http://localhost.evil", "https://user:secret@example.com", "https://example.com?token=x"):
            with self.assertRaises(ValueError):
                validated_url(url)
        self.assertEqual(validated_url("https://gpu.example.com/"),"https://gpu.example.com")

    def test_registered_test_source_is_not_retried_after_restart_or_settings_change(self):
        calls=[]
        def reject_diagnostic(request):
            calls.append((request.method,request.url.path))
            return httpx.Response(409,json={"detail":"technical evidence","code":"technical_test_source"})
        with httpx.Client(transport=httpx.MockTransport(reject_diagnostic)) as client:
            watch=Watcher(self.inbox,self.root/'diagnostic-data','http://127.0.0.1:8765','test',auto_publish=True,client=client)
            watch.tick(now=0)
            result=watch.tick(now=15)
            self.assertEqual(result[0]['status'],'technical_test')
            watch.tick(now=30)
            restarted=Watcher(self.inbox,self.root/'diagnostic-data','http://127.0.0.1:8765','test',
                              settings={'remove_silence':False},auto_publish=True,client=client)
            restarted.tick(now=45)
            restarted.tick(now=60)
        self.assertEqual(calls,[('POST','/api/jobs')])
        self.assertTrue(self.source.is_file())

    def test_owned_job_later_classified_as_test_stops_automatic_progress(self):
        self.job_status='queued'
        watch=self.watcher(automatic=True)
        watch.tick(now=0)
        watch.tick(now=15)
        self.job_status='review'
        self.job_kind='technical_test'
        result=watch.tick(now=30)
        self.assertTrue(any(row['status']=='technical_test' for row in result))
        calls_after_classification=list(self.calls)
        watch.tick(now=45)
        restarted=self.watcher(automatic=True)
        restarted.tick(now=60)
        restarted.tick(now=75)
        self.assertEqual(self.calls,calls_after_classification)
        self.assertFalse(any(path.endswith(('/approve','/publish')) for _,path in self.calls))

    def test_other_conflicts_do_not_falsely_classify_material_as_test(self):
        calls=[]
        def other_conflict(request):
            calls.append(request.url.path)
            return httpx.Response(409,json={'detail':'different conflict'})
        with httpx.Client(transport=httpx.MockTransport(other_conflict)) as client:
            watch=Watcher(self.inbox,self.root/'other-data','http://127.0.0.1:8765','test',client=client)
            watch.tick(now=0)
            self.assertEqual(watch.tick(now=15)[0]['status'],'waiting')
            watch.tick(now=30)
        self.assertEqual(calls,['/api/jobs','/api/jobs'])


if __name__ == "__main__":
    unittest.main()
