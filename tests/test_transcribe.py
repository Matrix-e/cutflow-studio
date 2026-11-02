"""Offline ASR contract tests use fake models, never install/download anything."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from studio.transcribe import TranscriptionError, segments_to_srt, transcribe_source


class SubtitleFormattingTests(unittest.TestCase):
    def test_preserves_source_clock(self):
        result = segments_to_srt([{"start": 65.125, "end": 67.9, "text": "这是源片字幕。"}])
        self.assertIn("00:01:05,125 --> 00:01:07,900", result)
        self.assertIn("这是源片字幕。", result)

    def test_rejects_invalid_and_overlapping_timing(self):
        for segments in ([{"start": 1, "end": float("nan"), "text": "a"}],
                         [{"start": 0, "end": 2, "text": "a"}, {"start": 1, "end": 3, "text": "b"}]):
            with self.assertRaises(TranscriptionError):
                segments_to_srt(segments)


class OfflineAsrTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cutflow-asr-tests-")
        self.directory = Path(self.temp.name)
        self.model = self.directory / "local-model"
        self.model.mkdir()
        for name in ("model.bin", "config.json", "tokenizer.json"):
            (self.model / name).write_text("test fixture")
        self.source = self.directory / "source.mp4"
        self.source.write_bytes(b"mock source")
        self.info = {"duration": 3.0, "has_audio": True, "audio_start": 0, "video_start": 0}

    def tearDown(self):
        self.temp.cleanup()

    def test_requires_local_tokenizer_before_import(self):
        (self.model / "tokenizer.json").unlink()
        with patch("studio.transcribe.importlib.import_module") as importer:
            with self.assertRaisesRegex(TranscriptionError, "tokenizer.json"):
                transcribe_source(self.source, self.model, self.directory / "output")
            importer.assert_not_called()

    def test_model_name_cannot_trigger_download(self):
        with patch("studio.transcribe.importlib.import_module") as importer:
            with self.assertRaisesRegex(TranscriptionError, "local model directory"):
                transcribe_source(self.source, self.directory / "large-v3", self.directory / "output")
            importer.assert_not_called()

    def test_mock_transcription_preserves_words_and_uses_offline_cuda(self):
        word = SimpleNamespace(start=0.5, end=1.4, word=" 测试", probability=0.91)
        segment = SimpleNamespace(start=0.5, end=1.5, text="测试", words=[word])
        model_class = MagicMock()
        model_class.return_value.transcribe.return_value = (iter([segment]), SimpleNamespace(language="zh"))
        with patch("studio.transcribe.inspect_media", return_value=self.info), \
             patch("studio.transcribe._decode_aligned_audio"), \
             patch("studio.transcribe.importlib.import_module", return_value=SimpleNamespace(WhisperModel=model_class)):
            result = transcribe_source(self.source, self.model, self.directory / "output", device="cuda")
        kwargs = model_class.call_args.kwargs
        self.assertTrue(kwargs["local_files_only"])
        self.assertEqual(kwargs["device"], "cuda")
        self.assertEqual(kwargs["compute_type"], "float16")
        self.assertFalse(result["validation"]["burned_subtitles"])
        self.assertFalse(result["validation"]["human_verified"])
        self.assertEqual(result["clock"], "source_video_seconds")
        self.assertIn("00:00:00,500 --> 00:00:01,500", Path(result["srt_path"]).read_text(encoding="utf-8"))
        saved = json.loads(Path(result["json_path"]).read_text(encoding="utf-8"))
        self.assertEqual(saved["segments"][0]["words"][0]["probability"], 0.91)

    def test_no_audio_does_not_import_model(self):
        with patch("studio.transcribe.inspect_media", return_value={**self.info, "has_audio": False}), \
             patch("studio.transcribe.importlib.import_module") as importer:
            with self.assertRaisesRegex(TranscriptionError, "no audio"):
                transcribe_source(self.source, self.model, self.directory / "output")
            importer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
