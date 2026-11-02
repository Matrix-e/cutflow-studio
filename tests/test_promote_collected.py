import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from scripts.promote_collected import adopt
from studio.media import MediaError


class PromotionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.collected, self.inbox = self.root / "collected", self.root / "inbox"
        self.collected.mkdir()
        self.source = self.collected / "12345.mp4"
        self.source.write_bytes(b"fixture contents; probe is separately verified")
        self.provenance = {"version": 1, "item_id": "12345", "source_url": "https://www.douyin.com/video/12345",
                           "sha256": hashlib.sha256(self.source.read_bytes()).hexdigest(), "size_bytes": self.source.stat().st_size,
                           "rights_status": "unknown", "watermark_removed": False, "title": "sample"}
        self.sidecar = self.collected / "12345.provenance.json"
        self.sidecar.write_text(json.dumps(self.provenance), encoding="utf-8")
        self.probe = lambda _: {"video_codec": "h264", "duration": 2, "width": 320, "height": 180}

    def tearDown(self):
        self.tmp.cleanup()

    def promote(self, **kwargs):
        return adopt(self.collected, self.inbox, kwargs.pop("item_id", "12345"), kwargs.pop("rights_status", "owned"),
                     kwargs.pop("rights_note", "本人原创拍摄，拥有完整权利"), probe=kwargs.pop("probe", self.probe))

    def test_copy_preserves_source_and_records_explicit_rights(self):
        result = self.promote()
        target = Path(result["path"])
        self.assertEqual(self.source.read_bytes(), target.read_bytes())
        self.assertTrue(self.source.exists())
        preserved = json.loads(Path(result["provenance"]).read_text(encoding="utf-8"))
        self.assertEqual(self.provenance, preserved["source_provenance"])
        self.assertEqual("owned", preserved["adoption"]["rights_status"])
        self.assertEqual("already_adopted", self.promote()["status"])

    def test_requires_ownership_choice_and_note(self):
        for rights in ("unknown", "public", "", "downloaded"):
            with self.assertRaises(ValueError): self.promote(rights_status=rights)
        with self.assertRaises(ValueError): self.promote(rights_note="ok")
        self.assertFalse(self.inbox.exists())

    def test_identifier_cannot_traverse(self):
        for item in ("../secret", "123/45", "C:\\private", "12345.mp4", "１２３"):
            with self.assertRaises(ValueError): self.promote(item_id=item)

    def test_tampered_bytes_are_not_adopted(self):
        self.source.write_bytes(b"modified video")
        with self.assertRaises(ValueError): self.promote()
        self.assertEqual([], list((self.inbox / "抖音已选").iterdir()))

    def test_existing_different_video_is_preserved(self):
        target_dir = self.inbox / "抖音已选"
        target_dir.mkdir(parents=True)
        target = target_dir / "12345.mp4"
        target.write_bytes(b"existing different content")
        with self.assertRaises(ValueError): self.promote()
        self.assertEqual(b"existing different content", target.read_bytes())

    def test_source_mutation_during_probe_aborts_before_commit(self):
        def changing_probe(path):
            self.source.write_bytes(b"changed during probe")
            return self.probe(path)
        with self.assertRaises(ValueError): self.promote(probe=changing_probe)
        self.assertEqual([], list((self.inbox / "抖音已选").iterdir()))

    def test_symlink_source_is_rejected(self):
        other = self.root / "outside.mp4"
        self.source.rename(other)
        try:
            self.source.symlink_to(other)
        except OSError:
            self.skipTest("Host does not allow test symlinks")
        with self.assertRaises(ValueError): self.promote()

    @unittest.skipUnless(shutil.which("ffprobe"), "ffprobe unavailable")
    def test_real_ffprobe_rejects_non_video_fixture(self):
        with self.assertRaises(MediaError): self.promote(probe=None)
        self.assertEqual([], list((self.inbox / "抖音已选").iterdir()))


if __name__ == "__main__":
    unittest.main()
