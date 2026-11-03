"""Brief semantics and real multi-source FFmpeg output checks."""
import array
import copy
import hashlib
import math
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from studio.creative import CreativeError, _allocation, build_creation_plan, interpret_brief, render_creation_plan


class BriefTests(unittest.TestCase):
    def test_main_ui_example(self):
        result = interpret_brief('做一条30秒竖屏视频，按素材顺序拼接，保留原声，开头标题用“汽车托管，先看这三张清单”。')
        self.assertEqual(result["resolved"], dict(target_duration=30, aspect="9:16", keep_original_audio=True,
                                               pace="preserve", opening_title="汽车托管，先看这三张清单", music=False))

    def test_full_length_music_example(self):
        result = interpret_brief("保留完整，横屏，加一点轻音乐")
        self.assertEqual(result["resolved"]["aspect"], "16:9")
        self.assertEqual(result["resolved"]["pace"], "preserve")
        self.assertIsNone(result["resolved"]["target_duration"])
        self.assertTrue(result["resolved"]["music"])

    def test_explicit_options_override_even_false_null_or_empty(self):
        result = interpret_brief('三十秒竖屏，不要原声，加轻音乐，标题“标题”',
                                 {"target_duration": None, "aspect": "original", "music": False,
                                  "keep_original_audio": True, "opening_title": ""})
        self.assertEqual(result["resolved"], dict(target_duration=None, aspect="original", keep_original_audio=True,
                                                pace="preserve", opening_title="", music=False))

    def test_title_content_is_not_executable_intent(self):
        result = interpret_brief('标题“30秒竖屏，不要原声，加音乐”，横屏')
        self.assertEqual(result["resolved"]["aspect"], "16:9")
        self.assertIsNone(result["resolved"]["target_duration"])
        self.assertTrue(result["resolved"]["keep_original_audio"])
        self.assertFalse(result["resolved"]["music"])

    def test_negative_music_and_audio(self):
        result = interpret_brief("不要原声，不要轻音乐")
        self.assertFalse(result["resolved"]["music"])
        self.assertFalse(result["resolved"]["keep_original_audio"])
        for brief in ("不要加音乐", "无需背景音乐", "无音乐", "不添加配乐", "不需要添加轻音乐", "不要额外配乐", "不要添加背景音乐"):
            with self.subTest(brief=brief):
                self.assertFalse(interpret_brief(brief)["resolved"]["music"])

    def test_compound_chinese_duration(self):
        self.assertEqual(interpret_brief("一分三十秒横屏")["resolved"]["target_duration"], 90)

    def test_whole_duration_beats_local_title_duration(self):
        self.assertEqual(interpret_brief("前3秒显示标题，整片30秒")["resolved"]["target_duration"], 30)
        self.assertEqual(interpret_brief("整片30秒，开头3秒显示标题")["resolved"]["target_duration"], 30)
        self.assertIsNone(interpret_brief("前3秒显示标题")["resolved"]["target_duration"])
        self.assertIsNone(interpret_brief("每段10秒")["resolved"]["target_duration"])
        result = interpret_brief("30秒或者45秒")
        self.assertTrue(any("多个整体时长" in w for w in result["warnings"]))

    def test_negative_aspect_and_conflicts(self):
        self.assertEqual(interpret_brief("不要竖屏，用横屏")["resolved"]["aspect"], "16:9")
        self.assertEqual(interpret_brief("不要横屏，竖屏")["resolved"]["aspect"], "9:16")
        self.assertEqual(interpret_brief("不要竖屏")["resolved"]["aspect"], "original")
        self.assertTrue(any("多个画幅" in w for w in interpret_brief("横屏和竖屏")["warnings"]))

    def test_silence_cleanup_keeps_original_sound(self):
        for text in ("去掉静音段", "保留原声，清理静音"):
            result = interpret_brief(text)["resolved"]
            self.assertTrue(result["keep_original_audio"])
            self.assertEqual(result["pace"], "gentle")

    def test_adopted_idea_only_compiles_explicit_execution_block(self):
        brief = ('创意正文：开头3秒提出问题，计划60秒横屏，建议音乐和新旁白。'
                 '\n\n【剪辑执行要求】\n整片30秒，9:16，保留原声，不要音乐')
        result = interpret_brief(brief)
        self.assertEqual(result["resolved"]["target_duration"], 30)
        self.assertEqual(result["resolved"]["aspect"], "9:16")
        self.assertTrue(result["resolved"]["keep_original_audio"])
        self.assertFalse(result["resolved"]["music"])
        self.assertTrue(any("作为策划保存" in w for w in result["warnings"]))
        self.assertFalse(any("多个整体时长" in w or "多个画幅" in w for w in result["warnings"]))

    def test_execution_block_empty_duplicate_or_oversized_rejected(self):
        for brief in ('创意【剪辑执行要求】', '【剪辑执行要求】30秒【剪辑执行要求】40秒',
                      '【剪辑执行要求】' + 'a' * 1001):
            with self.subTest(brief=brief[:30]), self.assertRaises(CreativeError):
                interpret_brief(brief)

    def test_unimplemented_requests_are_visible(self):
        result = interpret_brief("提取精彩片段，改写旁白，加字幕，卡点转场")
        joined = " ".join(result["warnings"])
        for required in ("语义", "不会自动生成字幕", "不会新增或改写配音", "不执行转场"):
            self.assertIn(required, joined)

    def test_invalid_options_rejected(self):
        for options in ({"music": "false"}, {"target_duration": float("nan")}, {"target_duration": True},
                        {"target_duration": 0}, {"target_duration": 3601}, {"pace": "semantic"},
                        {"opening_title": "x" * 121}, {"opening_title": "title\nnew"}, {"exec": "command"}):
            with self.subTest(options=options), self.assertRaises(CreativeError):
                interpret_brief("", options)

    def test_allocation_preserves_all_source_contributions(self):
        self.assertEqual(sum(_allocation([3000, 1, 50], 30)), 30)
        self.assertTrue(all(x > 0 for x in _allocation([3000, 1, 50], 30)))
        self.assertEqual(_allocation([4, 6], 20), [4, 6])


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class CreativeIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="cutflow-creative-test-")
        cls.folder = Path(cls.temp.name)
        cls.red = cls.folder / "原片 红色.mp4"
        cls.blue = cls.folder / "原片 蓝色.mp4"
        cls.run_ffmpeg("-f", "lavfi", "-i", "color=c=red:s=160x90:r=30:d=2",
                       "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
                       "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                       "-c:a", "aac", "-b:a", "192k", str(cls.red))
        cls.run_ffmpeg("-f", "lavfi", "-i", "color=c=blue:s=90x160:r=30:d=3",
                       "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(cls.blue))
        cls.settings = {"width": 160, "height": 90, "crf": 22, "normalize_audio": False}

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    @staticmethod
    def run_ffmpeg(*args, binary=False):
        result = subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-y", *args], capture_output=True)
        if result.returncode:
            raise AssertionError(result.stderr.decode("utf8", "replace"))
        return result.stdout if binary else result.stdout.decode("utf8", "replace")

    def audio_rms(self, path, start, duration):
        raw = self.run_ffmpeg("-ss", str(start), "-i", str(path), "-t", str(duration), "-vn",
                              "-f", "s16le", "-ac", "1", "-ar", "48000", "-", binary=True)
        samples = array.array("h", raw)
        return math.sqrt(sum(x*x for x in samples) / max(1, len(samples))) / 32768

    def pixel(self, path, position):
        raw = self.run_ffmpeg("-ss", str(position), "-i", str(path), "-frames:v", "1",
                              "-vf", "crop=2:2:iw/2:ih/2,format=rgb24", "-f", "rawvideo", "-", binary=True)
        return list(raw[:3])

    def test_target_duration_order_mixed_audio_and_canvas(self):
        original_hashes = [hashlib.sha256(p.read_bytes()).hexdigest() for p in (self.red, self.blue)]
        plan = build_creation_plan([str(self.red), str(self.blue)], "3秒，保留原声", settings=self.settings)
        self.assertAlmostEqual(plan["duration"], 3, places=4)
        self.assertEqual([c["source_index"] for c in plan["clips"]], [0, 1])
        self.assertTrue(any("不是语义亮点" in w for w in plan["warnings"]))
        output = self.folder / "mixed.mp4"
        report = render_creation_plan(plan, output)
        self.assertTrue(report["qc"]["passed"])
        self.assertAlmostEqual(report["duration"], 3, delta=0.08)
        red, blue = self.pixel(output, 0.5), self.pixel(output, 2.5)
        self.assertGreater(red[0], red[2] + 100)
        self.assertGreater(blue[2], blue[0] + 100)
        self.assertGreater(self.audio_rms(output, 0.3, 0.4), 0.03)
        self.assertLess(self.audio_rms(output, 2.2, 0.4), 0.001)
        self.assertEqual(original_hashes, [hashlib.sha256(p.read_bytes()).hexdigest() for p in (self.red, self.blue)])

    def test_preserve_complete_default_and_larger_target_no_loop(self):
        plan = build_creation_plan([str(self.red), str(self.blue)], settings=self.settings)
        self.assertAlmostEqual(plan["duration"], 5, delta=0.04)
        larger = build_creation_plan([str(self.red)], "10秒", settings=self.settings)
        self.assertAlmostEqual(larger["duration"], 2, delta=0.04)
        self.assertTrue(any("未循环" in w for w in larger["warnings"]))

    def test_plan_retains_full_adopted_idea_while_using_execution_settings(self):
        brief = '完整策划：建议60秒横屏音乐旁白。\n【剪辑执行要求】\n1秒，保留原比例，不要音乐'
        plan = build_creation_plan([str(self.red)], brief, settings=self.settings)
        self.assertEqual(plan["brief"], brief)
        self.assertEqual(plan["resolved"]["target_duration"], 1)
        self.assertFalse(plan["resolved"]["music"])

    def test_twelve_short_sources_join_without_duration_drift(self):
        plan = build_creation_plan([str(p) for p in [self.red, self.blue] * 6], "1.5秒", settings=self.settings)
        self.assertEqual(len(plan["sources"]), 12)
        self.assertAlmostEqual(sum(c["frames"] for c in plan["clips"]), 45)
        report = render_creation_plan(plan, self.folder / "twelve.mp4")
        self.assertTrue(report["qc"]["passed"])
        self.assertAlmostEqual(report["duration"], 1.5, delta=0.08)

    def test_original_audio_muted_and_music_is_actually_rendered(self):
        silent_plan = build_creation_plan([str(self.red)], "1秒，不要原声", settings=self.settings)
        silent_path = self.folder / "muted.mp4"
        render_creation_plan(silent_plan, silent_path)
        self.assertLess(self.audio_rms(silent_path, 0.2, 0.5), 0.001)
        music_plan = build_creation_plan([str(self.red)], "1秒，不要原声，加一点轻音乐", settings=self.settings)
        music_path = self.folder / "music.mp4"
        report = render_creation_plan(music_plan, music_path)
        self.assertTrue(report["qc"]["passed"])
        self.assertGreater(self.audio_rms(music_path, 0.2, 0.5), 0.004)
        self.assertIn("原创", report["music_source"])

    def test_title_drawn_and_portrait_geometry(self):
        plan = build_creation_plan([str(self.red)], '1秒竖屏，标题“汽车托管”',
                                   settings={**self.settings, "width": 90, "height": 160})
        output = self.folder / "titled.mp4"
        report = render_creation_plan(plan, output)
        self.assertTrue(report["qc"]["passed"])
        self.assertEqual((report["metadata"]["width"], report["metadata"]["height"]), (90, 160))
        # Upper black padding now contains bright glyph pixels from the title.
        raw = self.run_ffmpeg("-ss", "0.5", "-i", str(output), "-frames:v", "1",
                              "-vf", "crop=90:50:0:0,format=gray", "-f", "rawvideo", "-", binary=True)
        self.assertGreater(sum(value > 180 for value in raw), 20)
        self.assertEqual(report["title_applied"], "汽车托管")

    def test_source_change_and_overwrite_are_rejected(self):
        source = self.folder / "mutable.mp4"
        shutil.copy2(self.red, source)
        plan = build_creation_plan([str(source)], settings=self.settings)
        with self.assertRaises(CreativeError):
            render_creation_plan(plan, source)
        with source.open("ab") as handle:
            handle.write(b"modified")
        with self.assertRaises(CreativeError):
            render_creation_plan(plan, self.folder / "changed.mp4")

    def test_pace_changes_actual_pause_timeline_without_muting_speech(self):
        source = self.folder / "pause-pattern.mp4"
        self.run_ffmpeg("-f", "lavfi", "-i", "color=c=red:s=160x90:r=30:d=4",
                        "-f", "lavfi", "-i", "aevalsrc=if(between(t\\,1\\,3)\\,0\\,0.12*sin(2*PI*440*t)):s=48000:d=4",
                        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source))
        preserve = build_creation_plan([str(source)], "保留完整", settings=self.settings)
        gentle = build_creation_plan([str(source)], "保留原声，清理静音", settings=self.settings)
        self.assertAlmostEqual(preserve["duration"], 4, delta=0.04)
        self.assertLess(gentle["duration"], 3)
        output = self.folder / "pace.mp4"
        report = render_creation_plan(gentle, output)
        self.assertTrue(report["qc"]["passed"])
        self.assertGreater(self.audio_rms(output, 0.2, 0.4), 0.03)

    def test_failed_final_qc_does_not_replace_existing_deliverable(self):
        plan = build_creation_plan([str(self.red)], "1秒", settings=self.settings)
        output = self.folder / "existing.mp4"
        output.write_bytes(b"existing deliverable")
        from studio import media
        actual_check = media._check_output
        def check(path, active_plan, width, height):
            return {"passed": False, "checks": []} if Path(path).name == "finished.mp4" else actual_check(path, active_plan, width, height)
        with patch("studio.creative.media._check_output", side_effect=check), self.assertRaises(CreativeError):
            render_creation_plan(plan, output)
        self.assertEqual(output.read_bytes(), b"existing deliverable")

    def test_no_late_fps_change_or_unbounded_sources(self):
        plan = build_creation_plan([str(self.red)], settings=self.settings)
        with self.assertRaises(CreativeError):
            render_creation_plan(plan, self.folder / "fps.mp4", {"fps": 25})
        with self.assertRaises(CreativeError):
            build_creation_plan([str(self.red)] * 13)
        with self.assertRaises(CreativeError):
            build_creation_plan(["https://example.com/video.mp4"])


if __name__ == "__main__":
    unittest.main()
