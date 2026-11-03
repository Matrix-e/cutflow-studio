import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from studio import presenter


class PresenterTests(unittest.TestCase):
    def test_missing_models_do_not_claim_ready(self):
        with patch.dict(os.environ, {}, clear=True):
            status = presenter.configuration_status()
        self.assertFalse(status["configured"])
        self.assertFalse(status["runtime_verified"])
        self.assertFalse(status["capabilities"]["background_replacement"])
        self.assertFalse(status["capabilities"]["outfit_generation"])

    def test_files_alone_are_not_a_verified_runtime_and_model_change_invalidates_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for path in presenter.model_files(root):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"placeholder-model-for-unit-test" * 2)
            settings = {"CUTFLOW_PRESENTER_MODEL_DIR": str(root), "CUTFLOW_PRESENTER_PYTHON": sys.executable}
            with patch.dict(os.environ, settings, clear=True):
                self.assertTrue(presenter.configuration_status()["model_exists"])
                self.assertFalse(presenter.configuration_status()["configured"])
                marker = {"revision": presenter.REVISION, "model_signature": presenter._signature(root),
                          "python": sys.executable, "qc_passed": True}
                (root / "runtime-validation.json").write_text(json.dumps(marker))
                self.assertTrue(presenter.configuration_status()["configured"])
                (root / "musetalkV15/unet.pth").write_bytes(b"changed-model" * 10)
                self.assertFalse(presenter.configuration_status()["configured"])

    def test_authorization_is_strict_boolean_and_precedes_loading_models(self):
        for consent in (False, "true", 1, None):
            with self.subTest(consent=consent), patch.object(presenter, "_child") as child:
                with self.assertRaises(presenter.PresenterError) as raised:
                    presenter.render_presenter("a.png", "b.wav", "c.mp4", consent_confirmed=consent)
                self.assertEqual(raised.exception.code, "consent_required")
                child.assert_not_called()

    def test_cancelled_request_does_not_start_a_worker(self):
        import threading
        stop = threading.Event()
        stop.set()
        with patch.object(presenter, "_child") as child:
            with self.assertRaises(presenter.PresenterError) as raised:
                presenter.render_presenter("a.png", "b.wav", "c.mp4", consent_confirmed=True, stop_event=stop)
            self.assertEqual(raised.exception.code, "cancelled")
            child.assert_not_called()

    def test_silent_or_oversized_audio_cannot_reach_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            portrait, audio = root / "a.png", root / "a.wav"
            portrait.write_bytes(b"image")
            audio.write_bytes(b"audio")
            for info in ({"qc": {"passed": False}, "duration": 3}, {"qc": {"passed": True}, "duration": 121}):
                with patch("studio.narration.inspect_audio", return_value=info), patch.object(presenter, "_child") as child:
                    with self.assertRaises(presenter.PresenterError) as raised:
                        presenter.render_presenter(portrait, audio, root / "out.mp4", consent_confirmed=True)
                    self.assertEqual(raised.exception.code, "invalid_audio")
                    child.assert_not_called()

    def test_static_or_incomplete_generated_output_is_not_published(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            portrait, audio = root / "a.png", root / "a.wav"
            portrait.write_bytes(b"image")
            audio.write_bytes(b"audio")
            with patch("studio.narration.inspect_audio", return_value={"qc": {"passed": True}, "duration": 3}), \
                    patch.object(presenter, "configuration_status", return_value={"configured": True}), \
                    patch.object(presenter, "_inspect_output", return_value={"duration": 3, "width": 512, "height": 512}), \
                    patch.object(presenter, "_child", return_value={"frames": 75, "mouth_variation": 0}):
                with self.assertRaises(presenter.PresenterError) as raised:
                    presenter.render_presenter(portrait, audio, root / "out.mp4", consent_confirmed=True)
                self.assertEqual(raised.exception.code, "invalid_output")
                self.assertFalse((root / "out.mp4").exists())

    def test_existing_output_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for filename in ("a.png", "a.wav", "out.mp4"):
                (root / filename).write_bytes(b"original")
            with self.assertRaises(presenter.PresenterError):
                presenter.render_presenter(root / "a.png", root / "a.wav", root / "out.mp4", consent_confirmed=True)
            self.assertEqual((root / "out.mp4").read_bytes(), b"original")


if __name__ == "__main__":
    unittest.main()
