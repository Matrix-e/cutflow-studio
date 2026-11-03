"""Imported modes, authenticated source playback, and safe retry affordances."""
import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from studio.profiles import display_name, effective_settings, import_mode
from studio.server import create_app
from studio.watch import Watcher


class ProfileTests(unittest.TestCase):
    def test_only_exact_root_profile_components_select_a_mode(self):
        self.assertEqual(import_mode("工作台导入/preserve/id/source.mp4"), "preserve")
        self.assertEqual(import_mode("工作台导入/gentle/id/source.mp4"), "gentle")
        for source in ("backup/工作台导入/preserve/id/source.mp4", "工作台导入/preserve-old/id/source.mp4",
                       "工作台导入/Preserve/id/source.mp4", "工作台导入/preserve/source.mp4", "preserve/source.mp4"):
            self.assertIsNone(import_mode(source), source)

    def test_preserve_keeps_encoder_but_cannot_inherit_cut_ranges_or_resize(self):
        original = {"encoder": "nvenc", "remove_silence": True, "normalize_audio": False,
                    "target_aspect": "9:16", "source_ranges": [[1, 2]], "width": 320, "height": 568, "crf": 19}
        result = effective_settings("工作台导入/preserve/id/source.mp4", original)
        self.assertEqual(result, {"encoder": "nvenc", "remove_silence": False, "normalize_audio": True,
                                  "target_aspect": "original", "crf": 19})
        self.assertIn("source_ranges", original)
        for source in ("ordinary.mp4", "工作台导入/gentle/id/source.mp4"):
            self.assertEqual(effective_settings(source, original), original)

    def test_sync_suffix_is_removed_only_from_display_name(self):
        source = "工作台导入/preserve/id/采访第一集--0123456789abcdef.MOV"
        self.assertEqual(display_name(source), "采访第一集.MOV")
        self.assertTrue(source.endswith("--0123456789abcdef.MOV"))
        for name in ("speech--not-a-hash.mp4", "speech--0123456789abcde.mp4", "speech--0123456789abcdef0.mp4"):
            self.assertEqual(display_name(name), name)


class AssetProfileAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.inbox = self.root / "inbox"
        self.app = create_app(data_dir=self.root / "data", inbox=self.inbox, token="profile-test", start_worker=False)
        self.client = TestClient(self.app)
        self.auth = {"Authorization": "Bearer profile-test"}
        self.source = self.inbox / "工作台导入/preserve/123/采访--0123456789abcdef.mov"
        self.source.parent.mkdir(parents=True)
        self.source.write_bytes(b"test-container-bytes-for-http-only")
        self.relative = self.source.relative_to(self.inbox).as_posix()

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def create_job(self):
        response = self.client.post("/api/jobs", headers=self.auth, json={"source": self.relative})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["job"]

    def get_job(self, job_id):
        return self.client.get("/api/jobs/" + job_id, headers=self.auth).json()["job"]

    def test_assets_offer_display_and_profile_without_changing_source(self):
        asset = self.client.get("/api/assets", headers=self.auth).json()["assets"][0]
        self.assertEqual(asset["source"], self.relative)
        self.assertEqual(asset["name"], self.source.name)
        self.assertEqual(asset["display_name"], "采访.mov")
        self.assertEqual(asset["suggested_preset"], "preserve")

    def test_source_preview_requires_auth_and_supports_ranges_with_real_mime(self):
        route = "/api/assets/video"
        params = {"source": self.relative}
        self.assertEqual(self.client.get(route, params=params).status_code, 401)
        self.assertEqual(self.client.get(route, params=params, headers={"Authorization": "Bearer wrong"}).status_code, 401)
        response = self.client.get(route, params=params, headers={**self.auth, "Range": "bytes=2-9"})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, self.source.read_bytes()[2:10])
        self.assertEqual(response.headers["content-type"], "video/quicktime")
        self.assertEqual(response.headers["content-range"], f"bytes 2-9/{self.source.stat().st_size}")
        self.assertTrue(response.headers["content-disposition"].startswith("inline;"))
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_preview_rejects_escapes_missing_and_nonvideo_files(self):
        secret = self.inbox / "notes.txt"
        secret.write_text("not a video")
        for source in ("../outside.mp4", "/outside.mp4", "C:/outside.mp4", "missing.mp4", "notes.txt", "bad\0.mp4"):
            with self.subTest(source=source):
                response = self.client.get("/api/assets/video", params={"source": source}, headers=self.auth)
                self.assertEqual(response.status_code, 400)

    def test_preview_rejects_symlink_escape_when_supported(self):
        outside = self.root / "outside.mp4"
        outside.write_bytes(b"private")
        link = self.inbox / "linked.mp4"
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("symlink permission unavailable")
        self.assertEqual(self.client.get("/api/assets/video", params={"source": "linked.mp4"}, headers=self.auth).status_code, 400)

    def test_diagnostic_original_cannot_be_previewed_as_production(self):
        job = self.create_job()
        self.app.state.store.transition(job["id"], ["queued"], "failed", "test fixture")
        self.app.state.store.mark_technical_test(job["id"], "Confirmed synthetic test fixture")
        response = self.client.get("/api/assets/video", params={"source": self.relative}, headers=self.auth)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.client.get("/api/assets", headers=self.auth).json()["assets"], [])
        self.assertFalse(self.get_job(job["id"])["can_retry"])

    def test_retry_hint_and_action_use_same_rules_including_publish_history(self):
        job = self.create_job()
        self.assertFalse(job["can_retry"])
        store = self.app.state.store
        store.transition(job["id"], ["queued"], "failed", "render fixture")
        failed = self.get_job(job["id"])
        self.assertTrue(failed["can_retry"])
        self.assertIsNone(failed["retry_block_reason"])
        self.assertTrue(self.client.get("/api/jobs", headers=self.auth).json()["jobs"][0]["can_retry"])
        retried = self.client.post(f"/api/jobs/{job['id']}/retry", headers=self.auth)
        self.assertEqual(retried.status_code, 200)
        self.assertFalse(retried.json()["job"]["can_retry"])
        store.transition(job["id"], ["queued"], "publishing", "external send fixture")
        store.transition(job["id"], ["publishing"], "failed", "interrupted fixture")
        failed = self.get_job(job["id"])
        self.assertFalse(failed["can_retry"])
        blocked = self.client.post(f"/api/jobs/{job['id']}/retry", headers=self.auth)
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.json()["detail"], failed["retry_block_reason"])

    def test_modified_or_missing_source_blocks_retry_hint_and_action(self):
        job = self.create_job()
        self.app.state.store.transition(job["id"], ["queued"], "failed", "fixture")
        self.source.write_bytes(b"different-file")
        for missing in (False, True):
            if missing:
                self.source.unlink()
            failed = self.get_job(job["id"])
            self.assertFalse(failed["can_retry"])
            response = self.client.post(f"/api/jobs/{job['id']}/retry", headers=self.auth)
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["detail"], failed["retry_block_reason"])

    def test_preserve_watch_settings_and_idempotency_survive_restart(self):
        settings = {"encoder": "nvenc", "remove_silence": True, "target_aspect": "9:16", "silence_min": 1.5}
        for start in (0, 30):
            watch = Watcher(self.inbox, self.root / "watch", "http://127.0.0.1:8765", "profile-test", settings=settings,
                            client=self.client)
            watch.tick(now=start)
            watch.tick(now=start + 15)
        jobs = self.app.state.store.list()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["title"], "采访")
        self.assertEqual(jobs[0]["settings"], {**settings, "remove_silence": False,
                                               "normalize_audio": True, "target_aspect": "original"})
        with watch.connection() as con:
            rows = con.execute("SELECT * FROM submissions").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0]["settings"]), jobs[0]["settings"])

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
    def test_preserve_import_renders_full_duration_with_silence_and_unchanged_source(self):
        # This lives only in a temporary directory, never in a user media library.
        subprocess.run([shutil.which("ffmpeg"), "-hide_banner", "-v", "error", "-y",
                        "-f", "lavfi", "-i", "testsrc2=s=160x90:r=15:d=4",
                        "-f", "lavfi", "-i", "aevalsrc=if(between(t\\,0.5\\,1.5)+between(t\\,3\\,3.5)\\,0.15*sin(2*PI*440*t)\\,0):s=48000:d=4",
                        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(self.source)],
                       check=True, capture_output=True, timeout=30)
        before = hashlib.sha256(self.source.read_bytes()).hexdigest()
        watch = Watcher(self.inbox, self.root / "watch", "http://127.0.0.1:8765", "profile-test",
                        settings={"fps": 15, "encoder": "cpu", "remove_silence": True}, client=self.client)
        watch.tick(now=0)
        watch.tick(now=15)
        job = self.app.state.store.claim()
        self.assertIsNotNone(job)
        self.app.state.worker.process(job)
        result = self.get_job(job["id"])
        self.assertEqual(result["status"], "review", result.get("error"))
        self.assertEqual(len(result["plan"]["clips"]), 1)
        self.assertEqual(result["plan"]["clips"][0]["source_in"], 0)
        self.assertAlmostEqual(result["plan"]["clips"][0]["source_out"], 4, delta=1/15)
        self.assertAlmostEqual(result["result"]["duration"], 4, delta=0.08)
        self.assertEqual((result["result"]["metadata"]["display_width"], result["result"]["metadata"]["display_height"]), (160, 90))
        self.assertTrue(result["result"]["qc"]["passed"])
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), before)
        watch.tick(now=45)
        self.assertEqual(len(self.app.state.store.list()), 1)


if __name__ == "__main__":
    unittest.main()
