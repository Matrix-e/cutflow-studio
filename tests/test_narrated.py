import array
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

from studio import narration, narrated


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class NarratedIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cutflow-narrated-test-")
        self.folder = Path(self.temp.name)
        self.source = self.folder / "video.mp4"
        result = subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=navy:s=320x180:r=30:d=2",
                                 "-c:v", "libx264", "-pix_fmt", "yuv420p", str(self.source)], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.settings = {"width": 320, "height": 180, "crf": 23, "normalize_audio": False}

    def tearDown(self):
        self.temp.cleanup()

    def voice(self, text, voice_id, output_path, **kwargs):
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        samples = array.array("h", (int(5000 * math.sin(2 * math.pi * 440 * i / 24000)) for i in range(72000)))
        if sys.byteorder != "little":
            samples.byteswap()
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1); handle.setsampwidth(2); handle.setframerate(24000); handle.writeframes(samples.tobytes())
        srt = path.with_suffix(".srt")
        segments = [{"text": "真实问题，值得讨论。", "start": 0, "end": 1.1}, {"text": "记录下一步。", "start": 1.2, "end": 3}]
        srt.write_text(narration.make_srt(segments), encoding="utf-8")
        return {**narration.inspect_audio(path), "output_path": str(path), "srt_path": str(srt), "segments": segments,
                "sha256": narration._sha(path), "srt_sha256": narration._sha(srt), "warnings": []}

    def test_insufficient_visuals_reject_without_cutting_narration(self):
        with patch("studio.narrated.narration.synthesize", side_effect=self.voice), self.assertRaises(narrated.NarratedError) as caught:
            narrated.build_narrated_plan([str(self.source)], "完整讲稿。", "serena", settings=self.settings, work_dir=self.folder / "job")
        self.assertIn("未截断独白", str(caught.exception))

    def test_portrait_chinese_subtitles_fit_inside_frame_without_losing_text(self):
        text = "欢迎了解汽车租赁行业的人工智能沙龙，带着日常业务中的真实问题，一起交流新的思路，探讨适合自己的应用方向。"
        srt, ass = self.folder / "long.srt", self.folder / "long.ass"
        srt.write_text(narration.make_srt([{"text":text,"start":0,"end":10}]),encoding="utf-8")
        vf = narrated._subtitle_filter(srt,360,640,ass)
        rows = [r.split(",",9)[-1] for r in ass.read_text(encoding="utf-8").splitlines() if r.startswith("Dialogue:")]
        self.assertEqual("".join(rows).replace(r"\N",""),text)
        self.assertTrue(all(r.count(r"\N") <= 1 for r in rows))
        short = "欢迎了解汽车租赁行业的人工智能沙龙。"
        srt.write_text(narration.make_srt([{"text":short,"start":0,"end":3}]),encoding="utf-8")
        narrated._subtitle_filter(srt,1080,1920,self.folder / "portrait.ass")
        caption = next(r.split(",",9)[-1] for r in (self.folder / "portrait.ass").read_text(encoding="utf-8").splitlines() if r.startswith("Dialogue:"))
        self.assertTrue(all(len(line)>2 for line in caption.split(r"\N")))
        frame = subprocess.run([shutil.which("ffmpeg"),"-v","error","-f","lavfi","-i","color=black:s=360x640:d=1", "-vf",vf+",format=gray","-frames:v","1","-f","rawvideo","-"],capture_output=True)
        self.assertEqual(frame.returncode,0,frame.stderr)
        pixels=[(i%360,i//360) for i,p in enumerate(frame.stdout) if p>170]
        self.assertGreater(len(pixels),100)
        self.assertGreater(min(x for x,y in pixels),18)
        self.assertLess(max(x for x,y in pixels),342)
        self.assertGreater(min(y for x,y in pixels),450)
        self.assertLess(max(y for x,y in pixels),610)

    def test_explicit_loop_complete_voice_subtitles_music_and_decode(self):
        with patch("studio.narrated.narration.synthesize", side_effect=self.voice):
            plan = narrated.build_narrated_plan([str(self.source)], "完整讲稿。", "serena",
                                               {"visual_fill": "loop", "music": True, "target_duration": 1, "opening_title": "一次真实讨论"}, self.settings,
                                               work_dir=self.folder / "job")
        self.assertEqual(plan["duration"], 3)
        self.assertTrue(plan["loop_visuals"])
        self.assertEqual([c["visual_cycle"] for c in plan["clips"]], [0, 1])
        report = narrated.render_narrated_plan(plan, self.folder / "finished.mp4")
        self.assertTrue(report["qc"]["passed"])
        self.assertTrue(report["qc"]["narration_complete_duration"])
        self.assertEqual(plan["visual_plan"]["resolved"]["opening_title"], "")
        self.assertEqual(report["title_applied"], "一次真实讨论")
        self.assertGreaterEqual(report["duration"], 3)
        self.assertTrue(report["subtitles_burned"])
        self.assertTrue(Path(report["srt_path"]).is_file())
        result = subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-ss", "0.5", "-i", report["output_path"], "-frames:v", "1",
                                 "-vf", "crop=320:75:0:105,format=gray", "-f", "rawvideo", "-"], capture_output=True)
        self.assertEqual(result.returncode, 0)
        self.assertGreater(sum(pixel > 170 for pixel in result.stdout), 50)

    def test_multiple_sources_cover_voice_without_loop_and_preserve_full_audio(self):
        with patch("studio.narrated.narration.synthesize", side_effect=self.voice):
            plan = narrated.build_narrated_plan([str(self.source), str(self.source)], "讲稿。", "serena", {"subtitles": False},
                                               self.settings, work_dir=self.folder / "job")
        self.assertFalse(plan["loop_visuals"])
        self.assertEqual(len(plan["clips"]), 2)
        report = narrated.render_narrated_plan(plan, self.folder / "plain.mp4")
        self.assertTrue(report["qc"]["passed"])
        self.assertFalse(report["subtitles_burned"])
        self.assertGreaterEqual(report["metadata"]["audio_duration"], 3)

    def test_source_changed_while_synthesizing_is_not_silently_replanned(self):
        def changing_voice(*args, **kwargs):
            result = self.voice(*args, **kwargs)
            self.source.write_bytes(self.source.read_bytes() + b"changed")
            return result
        with patch("studio.narrated.narration.synthesize", side_effect=changing_voice), self.assertRaises(narrated.media.MediaError):
            narrated.build_narrated_plan([str(self.source), str(self.source)], "讲稿。", "serena", settings=self.settings,
                                        work_dir=self.folder / "job")

    def test_modified_audio_or_subtitle_rejected_before_render(self):
        with patch("studio.narrated.narration.synthesize", side_effect=self.voice):
            plan = narrated.build_narrated_plan([str(self.source)], "讲稿。", "serena", {"visual_fill": "loop"},
                                               self.settings, work_dir=self.folder / "job")
        Path(plan["narration"]["srt_path"]).write_text("changed", encoding="utf8")
        with self.assertRaises(narrated.NarratedError):
            narrated.render_narrated_plan(plan, self.folder / "tampered.mp4")

    def test_verified_prepared_voice_is_reused_and_mismatched_script_rejected(self):
        text = "讲稿。"
        prepared = {**self.voice(text, "serena", self.folder / "voice.wav"), "voice_id": "serena", "style": "professional",
                    "script_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}
        with patch("studio.narrated.narration.synthesize") as synthesize:
            plan = narrated.build_narrated_plan([str(self.source), str(self.source)], text, "serena", settings=self.settings,
                                               work_dir=self.folder / "prepared", prepared_narration=prepared)
            self.assertEqual(plan["narration"]["sha256"], prepared["sha256"])
            self.assertEqual(plan["narration_duration"], 3)
            synthesize.assert_not_called()
            for changed in ({"script_sha256": "bad"}, {"voice_id": "vivian"}, {"sha256": "bad"}, {"duration": 1}):
                with self.subTest(changed=changed), self.assertRaises(narrated.NarratedError):
                    narrated.build_narrated_plan([str(self.source)], text, "serena", settings=self.settings,
                                                work_dir=self.folder / "invalid", prepared_narration={**prepared, **changed})


if __name__ == "__main__":
    unittest.main()
