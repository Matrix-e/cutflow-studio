"""Security, recovery and state-machine tests; no external sends."""
import os
import sys
import tempfile
import types
import unittest
import shutil
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from studio.server import create_app, detect_gpu, safe_source
from studio.store import StateConflict, Store


class APITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.inbox = self.root / "inbox"
        self.inbox.mkdir()
        self.source = self.inbox / "访谈.mp4"
        self.source.write_bytes(b"synthetic-file-only-for-api-test")
        self.app = create_app(data_dir=self.root / "data", inbox=self.inbox, token="test-token-not-production", start_worker=False)
        self.client = TestClient(self.app)
        self.auth = {"Authorization": "Bearer test-token-not-production"}

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def create(self, **extra):
        return self.client.post("/api/jobs", headers=self.auth, json={"source": self.source.name, **extra})

    def reviewed_job(self):
        job = self.create().json()["job"]
        output = self.root / "data" / "renders" / (job["id"] + ".mp4")
        output.write_bytes(b"reviewed-render")
        self.app.state.store.transition(job["id"], ["queued"], "review", "fixture",
                                        output_path=str(output), result={"qc": {"passed": True}})
        return job["id"]

    def test_health_is_public_but_api_requires_bearer(self):
        self.assertEqual(self.client.get("/api/health").status_code, 200)
        for url in ("/api/assets", "/api/jobs", "/api/config", "/api/status"):
            self.assertEqual(self.client.get(url).status_code, 401)
        self.assertEqual(self.client.post("/api/jobs", json={"source": self.source.name}).status_code, 401)
        self.assertEqual(self.client.get("/api/assets", params={"token": "test-token-not-production"}).status_code, 401)
        result = self.client.get("/api/assets", headers=self.auth)
        self.assertEqual(result.json()["assets"][0]["source"], self.source.name)

    def test_gpu_inventory_is_cached_and_remote_target_is_explicit(self):
        probe = types.SimpleNamespace(returncode=0,stdout="NVIDIA GeForce RTX 4090 D, 24564\n")
        with patch("studio.server.shutil.which",side_effect=lambda name:name), patch("studio.server.subprocess.run",return_value=probe) as run, patch.dict(os.environ,{"CUTFLOW_EXECUTION_TARGET":"gpu"}):
            app = create_app(data_dir=self.root / "gpu-data",inbox=self.inbox,token="test-token-not-production",start_worker=False)
            with TestClient(app) as client:
                first = client.get("/api/status",headers=self.auth).json()
                second = client.get("/api/status",headers=self.auth).json()
            self.assertEqual(first["gpu"],{"available":True,"name":"NVIDIA GeForce RTX 4090 D","memory_mb":24564})
            self.assertEqual(first["execution_target"],"gpu")
            self.assertEqual(second["gpu"],first["gpu"])
            self.assertEqual(run.call_count,1)
            self.assertEqual(run.call_args.kwargs["timeout"],3)

    def test_gpu_inventory_failure_does_not_break_status(self):
        with patch("studio.server.shutil.which",return_value="nvidia-smi"), patch("studio.server.subprocess.run",side_effect=subprocess.TimeoutExpired("nvidia-smi",3)):
            self.assertEqual(detect_gpu(),{"available":False,"name":None,"memory_mb":None})

    def test_token_missing_fails_closed(self):
        app = create_app(data_dir=self.root / "other-data", inbox=self.inbox, token="", start_worker=False)
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/jobs", headers=self.auth).status_code, 503)

    def test_traversal_and_absolute_paths_are_rejected(self):
        for source in ("../escape.mp4", "C:/escape.mp4", "/tmp/escape.mp4", "\\server\\escape.mp4"):
            response = self.client.post("/api/jobs", headers=self.auth, json={"source": source})
            self.assertEqual(response.status_code, 400, source)

    def test_subtitle_paths_cannot_read_outside_inbox(self):
        subtitle = self.root / "private.srt"
        subtitle.write_text("private text", encoding="utf-8")
        for path in (str(subtitle), "../private.srt"):
            response = self.create(settings={"srt_path": path})
            self.assertEqual(response.status_code, 400)

    def test_second_worker_cannot_recover_first_workers_jobs(self):
        self.app.state.worker.start()
        other = create_app(data_dir=self.root / "data", inbox=self.inbox, token="different", start_worker=False)
        try:
            with self.assertRaises(RuntimeError):
                other.state.worker.start()
        finally:
            self.app.state.worker.stop()

    def test_symlink_outside_inbox_is_rejected_when_supported(self):
        outside = self.root / "outside.mp4"
        outside.write_bytes(b"secret")
        link = self.inbox / "link.mp4"
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("This host cannot create symlinks")
        with self.assertRaises(ValueError):
            safe_source(self.inbox, link.name)

    def test_idempotency_and_active_job_coalescing(self):
        first = self.create(idempotency_key="asset-sha-and-settings").json()
        second = self.create(idempotency_key="asset-sha-and-settings").json()
        third = self.create().json()
        self.assertEqual(first["job"]["id"], second["job"]["id"])
        self.assertEqual(first["job"]["id"], third["job"]["id"])
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertTrue(first["owned_by_request"])
        self.assertTrue(second["owned_by_request"])
        self.assertFalse(third["owned_by_request"])
        self.assertNotIn("idempotency_key",first["job"])
        response = self.create(idempotency_key="asset-sha-and-settings", settings={"target_aspect": "9:16"})
        self.assertEqual(response.status_code, 409)

    def test_manual_active_job_is_not_owned_by_another_request(self):
        manual = self.create().json()
        reused = self.create(idempotency_key="watcher-request").json()
        self.assertEqual(manual["job"]["id"],reused["job"]["id"])
        self.assertFalse(reused["created"])
        self.assertFalse(reused["owned_by_request"])

    def test_unsupported_settings_fail_clearly(self):
        self.assertEqual(self.create(settings={"semantic_rewrite": True}).status_code, 400)
        self.assertEqual(self.create(settings={"remove_silence": "false"}).status_code, 400)

    def test_retry_only_requeues_failed_edits_not_failed_delivery(self):
        job_id = self.create().json()["job"]["id"]
        store = self.app.state.store
        store.transition(job_id, ["queued"], "failed", "fixture", error="render error")
        response = self.client.post(f"/api/jobs/{job_id}/retry", headers=self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["job"]["status"], "queued")
        store.transition(job_id, ["queued"], "publishing", "fixture")
        store.transition(job_id, ["publishing"], "failed", "fixture")
        self.assertEqual(self.client.post(f"/api/jobs/{job_id}/retry", headers=self.auth).status_code, 409)

    def test_approval_requires_review_and_passed_qc(self):
        job_id = self.create().json()["job"]["id"]
        self.assertEqual(self.client.post(f"/api/jobs/{job_id}/approve", headers=self.auth).status_code, 409)
        self.assertEqual(self.client.post(f"/api/jobs/{job_id}/publish", headers=self.auth).status_code, 409)
        self.app.state.store.transition(job_id, ["queued"], "review", "fixture", result={"qc": {"passed": False}})
        self.assertEqual(self.client.post(f"/api/jobs/{job_id}/approve", headers=self.auth).status_code, 409)

    def test_disabled_publication_does_not_change_approved_state(self):
        job_id = self.reviewed_job()
        self.client.post(f"/api/jobs/{job_id}/approve", headers=self.auth)
        with patch.dict(os.environ, {"CUTFLOW_PUBLISH_ENABLED": "0"}):
            response = self.client.post(f"/api/jobs/{job_id}/publish", headers=self.auth)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.app.state.store.get(job_id)["status"], "approved")

    def test_publish_is_exactly_once_after_confirmed_success(self):
        job_id = self.reviewed_job()
        self.client.post(f"/api/jobs/{job_id}/approve", headers=self.auth)
        calls = []
        def fake_publish(path, title, key, approved=False):
            calls.append((path, title, key, approved))
            return {"status": "published", "remote_id": "fake"}
        module = types.SimpleNamespace(publish_video=fake_publish)
        with patch.dict(sys.modules, {"studio.delivery": module}), patch.dict(os.environ, {
            "CUTFLOW_PUBLISH_ENABLED": "1", "CUTFLOW_WECOM_MODE": "webhook", "CUTFLOW_WECOM_WEBHOOK": "test-only"}):
            first = self.client.post(f"/api/jobs/{job_id}/publish", headers=self.auth)
            second = self.client.post(f"/api/jobs/{job_id}/publish", headers=self.auth)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json()["already_published"])
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0][-1])

    def test_uncertain_publication_does_not_allow_automatic_retry(self):
        job_id = self.reviewed_job()
        self.client.post(f"/api/jobs/{job_id}/approve", headers=self.auth)
        def uncertain(*args, **kwargs):
            raise TimeoutError("should never leak webhook secret")
        module = types.SimpleNamespace(publish_video=uncertain)
        with patch.dict(sys.modules, {"studio.delivery": module}), patch.dict(os.environ, {
            "CUTFLOW_PUBLISH_ENABLED": "1", "CUTFLOW_WECOM_MODE": "webhook", "CUTFLOW_WECOM_WEBHOOK": "test-only"}):
            response = self.client.post(f"/api/jobs/{job_id}/publish", headers=self.auth)
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("webhook secret", response.text)
        self.assertEqual(self.app.state.store.get(job_id)["status"], "failed")
        self.assertEqual(self.client.post(f"/api/jobs/{job_id}/publish", headers=self.auth).status_code, 409)

    def test_worker_records_qc_failure_without_approving(self):
        job_id = self.create().json()["job"]["id"]
        job = self.app.state.store.claim()
        fake = types.SimpleNamespace(build_plan=lambda *args: {"duration": 1},
                                     render_plan=lambda *args: {"qc": {"passed": False}})
        with patch.dict(sys.modules, {"studio.media": fake}):
            self.app.state.worker.process(job)
        self.assertEqual(self.app.state.store.get(job_id)["status"], "failed")

    def test_modified_source_is_not_rendered_as_original_job(self):
        job_id = self.create().json()["job"]["id"]
        job = self.app.state.store.claim()
        self.source.write_bytes(b"changed")
        self.app.state.worker.process(job)
        self.assertEqual(self.app.state.store.get(job_id)["status"], "failed")

    def test_restart_recovers_encodes_but_not_uncertain_sends(self):
        job_id = self.create().json()["job"]["id"]
        store = self.app.state.store
        store.claim()
        store.recover()
        self.assertEqual(store.get(job_id)["status"], "queued")
        store.transition(job_id, ["queued"], "publishing", "fixture")
        store.recover()
        self.assertEqual(store.get(job_id)["status"], "failed")
        self.assertIn("未知", store.get(job_id)["error"])

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
    def test_real_api_render_reaches_review_and_streams_output(self):
        subprocess.run([shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", "testsrc2=s=160x90:r=15:d=1.6",
                        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1.6",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(self.source)],
                       check=True, capture_output=True, timeout=30)
        self.app.state.worker.start()
        try:
            job_id = self.create(settings={"width": 160, "height": 90, "fps": 15, "remove_silence": False}).json()["job"]["id"]
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                job = self.app.state.store.get(job_id)
                if job["status"] in {"review", "failed"}:
                    break
                time.sleep(0.1)
            self.assertEqual(job["status"], "review", job.get("error"))
            self.assertTrue(job["result"]["qc"]["passed"])
            response = self.client.get(f"/api/jobs/{job_id}/video", headers={**self.auth, "Range": "bytes=0-31"})
            self.assertEqual(response.status_code, 206)
            self.assertEqual(len(response.content), 32)
            self.assertEqual(self.client.post(f"/api/jobs/{job_id}/approve", headers=self.auth).status_code, 200)
        finally:
            self.app.state.worker.stop()


if __name__ == "__main__":
    unittest.main()
