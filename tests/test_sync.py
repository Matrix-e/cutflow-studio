import tempfile
import unittest
import zipfile
from pathlib import Path

from studio.sync import SyncAgent, candidate_files
from scripts.deploy_gpu import build_bundle


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.fail = False

    def upload(self, source, target, digest, size):
        if self.fail:
            raise OSError("connection closed")
        self.calls.append((source.name, target, digest, size))


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "inbox"
        self.root.mkdir()
        self.state = Path(self.tmp.name) / "state.sqlite3"
        self.now = 0
        self.agent = SyncAgent(self.root, "/root/autodl-tmp/cutflow/inbox", self.state, settle_seconds=15, clock=lambda: self.now)
        self.transport = FakeTransport()

    def tearDown(self):
        self.agent.close()
        self.tmp.cleanup()

    def test_stability_deduplication_and_no_delete(self):
        source = self.root / "中文素材.mp4"
        source.write_bytes(b"video data")
        self.assertEqual([], self.agent.scan(self.transport))
        self.now = 14
        self.assertEqual([], self.agent.scan(self.transport))
        self.now = 15
        result = self.agent.scan(self.transport)
        self.assertEqual("synced", result[0]["status"])
        self.assertTrue(source.exists())
        self.assertEqual([], self.agent.scan(self.transport))
        self.assertEqual(1, len(self.transport.calls))
        self.agent.close()
        self.agent = SyncAgent(self.root, "/root/autodl-tmp/cutflow/inbox", self.state, settle_seconds=15, clock=lambda: self.now)
        self.agent.scan(self.transport)
        self.now = 31
        self.assertEqual([], self.agent.scan(self.transport))

    def test_changed_content_waits_and_uses_new_remote_name(self):
        source = self.root / "same.mp4"
        source.write_bytes(b"version1")
        self.agent.scan(self.transport)
        self.now = 16
        first = self.agent.scan(self.transport)[0]
        source.write_bytes(b"version-two")
        self.assertEqual([], self.agent.scan(self.transport))
        self.now = 32
        second = self.agent.scan(self.transport)[0]
        self.assertNotEqual(first["destination"], second["destination"])

    def test_library_mode_directory_survives_sync_unchanged(self):
        source = self.root / '工作台导入/library/abc/待选择.mp4'
        source.parent.mkdir(parents=True)
        source.write_bytes(b'video-library')
        self.agent.scan(self.transport)
        self.now = 15
        result = self.agent.scan(self.transport)[0]
        self.assertEqual(result['path'], '工作台导入/library/abc/待选择.mp4')
        self.assertTrue(result['destination'].startswith('/root/autodl-tmp/cutflow/inbox/工作台导入/library/abc/待选择--'))
        self.assertEqual(self.agent.scan(self.transport), [])
        self.assertTrue(source.is_file())

    def test_dry_run_and_failed_send_do_not_mark_synced(self):
        (self.root / "a.mp4").write_bytes(b"video")
        self.agent.scan(self.transport)
        self.now = 20
        self.assertEqual("dry_run", self.agent.scan(dry_run=True)[0]["status"])
        self.transport.fail = True
        self.assertEqual("failed", self.agent.scan(self.transport)[0]["status"])
        self.transport.fail = False
        self.assertEqual("synced", self.agent.scan(self.transport)[0]["status"])

    def test_filters_and_remote_traversal(self):
        for directory in ["outputs", ".hidden", "renders"]:
            (self.root / directory).mkdir()
            (self.root / directory / "bad.mp4").write_bytes(b"v")
        (self.root / "a.mp4.part").write_bytes(b"v")
        (self.root / "ok.mp4").write_bytes(b"v")
        (self.root / "raw.MTS").write_bytes(b"v")
        self.assertEqual(["ok.mp4", "raw.MTS"], [p.name for p in candidate_files(self.root)])
        with self.assertRaises(ValueError):
            SyncAgent(self.root, "/root/../etc", self.state)

    def test_source_bundle_excludes_runtime_credentials_and_media(self):
        (self.root / "studio").mkdir()
        (self.root / "studio" / "server.py").write_text("app = None\n", encoding="utf-8")
        (self.root / ".env").write_text("SECRET=do-not-package", encoding="utf-8")
        (self.root / "video.mp4").write_bytes(b"user media")
        (self.root / "data").mkdir()
        (self.root / "data" / "token.txt").write_text("do-not-package", encoding="utf-8")
        (self.root / "config").mkdir()
        (self.root / "config" / "preset.json").write_text('{"silence_min":0.9}', encoding="utf-8")
        (self.root / "config" / "local.json").write_text('{"CUTFLOW_TOKEN":"do-not-package"}', encoding="utf-8")
        (self.root / "config" / "private.json").write_text('{"SECRET":"do-not-package"}', encoding="utf-8")
        bundle, manifest = build_bundle(self.root, Path(self.tmp.name) / "bundles")
        with zipfile.ZipFile(bundle) as archive:
            self.assertEqual({"studio/server.py", "config/preset.json", "source-manifest.json"}, set(archive.namelist()))
        self.assertEqual(["config/preset.json", "studio/server.py"], [row["path"] for row in manifest])
        original = bundle.read_bytes()
        same, _ = build_bundle(self.root, Path(self.tmp.name) / "bundles")
        self.assertEqual(original, same.read_bytes())


if __name__ == "__main__":
    unittest.main()
