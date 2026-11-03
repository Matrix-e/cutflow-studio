import array
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import wave

from studio import narration


def tone(path, duration, rate=24000, amplitude=0.12):
    samples = array.array("h", (int(32767 * amplitude * math.sin(2 * math.pi * 330 * i / rate)) for i in range(round(duration * rate))))
    if sys.byteorder != "little":
        samples.byteswap()
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(samples.tobytes())


class NarrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cutflow-narration-test-")
        self.folder = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_sentence_split_preserves_every_nonwhitespace_character(self):
        text = "沙龙结束以后，真正值得带走的是什么？\n先看真实的问题与讨论。最后再决定下一步！"
        parts = narration.split_sentences(text)
        self.assertEqual(len(parts), 3)
        self.assertEqual("".join(parts), text.replace("\n", ""))
        self.assertEqual(narration.split_sentences('他说：“记录要完整。”之后再核实。'), ['他说：“记录要完整。”', '之后再核实。'])

    def test_long_unpunctuated_script_rejected_without_arbitrary_character_cuts(self):
        with self.assertRaises(narration.NarrationError):
            narration.split_sentences("字" * 161)
        with self.assertRaises(narration.NarrationError):
            narration.split_sentences("a" * 1501)

    def test_true_sentence_audio_timing_not_uniform_character_allocation(self):
        first, second = self.folder / "first.wav", self.folder / "second.wav"
        tone(first, 0.8)
        tone(second, 1.4)
        report = narration.assemble_audio(["短句。", "第二个句子。"], [first, second], self.folder / "joined.wav")
        self.assertAlmostEqual(report["duration"], 2.3, places=5)
        self.assertEqual(report["segments"][0], {"text": "短句。", "start": 0.0, "end": 0.8})
        self.assertAlmostEqual(report["segments"][1]["start"], 0.9)
        self.assertAlmostEqual(report["segments"][1]["end"], 2.3)
        self.assertTrue(report["qc"]["passed"])
        self.assertFalse(report["qc"]["speech_content_verified"])
        self.assertIn("00:00:00,900 --> 00:00:02,300", narration.make_srt(report["segments"]))

    def test_silent_or_truncated_pcm_rejected(self):
        silent = self.folder / "silent.wav"
        tone(silent, 1, amplitude=0)
        self.assertFalse(narration.inspect_audio(silent)["qc"]["passed"])
        with self.assertRaises(narration.NarrationError):
            narration.assemble_audio(["声音。"], [silent], self.folder / "invalid.wav")

    def test_subtitle_markup_cannot_insert_override_commands(self):
        srt = narration.make_srt([{"text": '<font>{\\pos(1,1)}中文</font>', "start": 0, "end": 1}])
        self.assertNotIn("<font>", srt)
        self.assertNotIn("{\\pos", srt)

    def test_voice_style_and_text_bounds(self):
        for text, voice, style in (("", "serena", "professional"), ("文本。", "unknown", "professional"), ("文本。", "serena", "unknown")):
            with self.subTest(voice=voice, style=style), self.assertRaises(narration.NarrationError):
                narration._validate(text, voice, style)
        self.assertEqual(len(narration.list_voices()), 5)

    def _environment(self):
        model = self.folder / "model"
        tokenizer = model / "speech_tokenizer"
        tokenizer.mkdir(parents=True)
        (model / "config.json").write_text(json.dumps({"tts_model_type": "custom_voice", "tts_model_size": "1b7"}))
        (model / "tokenizer_config.json").write_text("{}")
        (model / "model.safetensors").write_bytes(b"fixture")
        (tokenizer / "config.json").write_text("{}")
        (tokenizer / "model.safetensors").write_bytes(b"fixture")
        return patch.dict(os.environ, CUTFLOW_TTS_MODEL_DIR=str(model), CUTFLOW_TTS_PYTHON=sys.executable)

    def test_lazy_configuration_does_not_import_gpu_dependencies(self):
        with self._environment(), patch.dict(sys.modules, {"torch": None, "qwen_tts": None}):
            self.assertTrue(narration.configuration_status()["configured"])

    def test_synthesis_protocol_writes_complete_audio_and_real_srt(self):
        real_popen, captured = subprocess.Popen, {}
        code = """import json,sys,wave,array,math,pathlib
r=json.loads(sys.stdin.read())
for i,s in enumerate(r['sentences']):
 p=pathlib.Path(r['work_dir'])/('sentence-%03d.wav'%i)
 a=array.array('h',(int(4000*math.sin(2*math.pi*330*j/24000)) for j in range(12000+2400*i)))
 with wave.open(str(p),'wb') as w:
  w.setnchannels(1);w.setsampwidth(2);w.setframerate(24000);w.writeframes(a.tobytes())
print('{}')
"""
        def spawn(command, **kwargs):
            captured.update(kwargs)
            return real_popen([sys.executable, "-c", code], **kwargs)
        with self._environment(), patch("studio.narration.subprocess.Popen", side_effect=spawn):
            report = narration.synthesize("第一句。第二句。", "serena", self.folder / "out.wav")
        self.assertAlmostEqual(report["duration"], 1.2)
        self.assertEqual(len(report["segments"]), 2)
        self.assertTrue(Path(report["srt_path"]).is_file())
        self.assertEqual(captured["env"]["HF_HUB_OFFLINE"], "1")
        self.assertTrue(report["qc"]["passed"])

    def test_timeout_terminates_worker_and_does_not_replace_existing_audio(self):
        real_popen, processes = subprocess.Popen, []
        def spawn(command, **kwargs):
            process = real_popen([sys.executable, "-c", "import sys,time;sys.stdin.read();time.sleep(30)"], **kwargs)
            processes.append(process)
            return process
        destination = self.folder / "existing.wav"
        destination.write_bytes(b"original")
        with self._environment(), patch("studio.narration.subprocess.Popen", side_effect=spawn), patch.object(narration, "HARD_TIMEOUT", 0.1):
            with self.assertRaises(narration.NarrationError) as caught:
                narration.synthesize("一句话。", "serena", destination)
        self.assertEqual(caught.exception.code, "timeout")
        self.assertIsNotNone(processes[0].poll())
        self.assertEqual(destination.read_bytes(), b"original")


if __name__ == "__main__":
    unittest.main()
