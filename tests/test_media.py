"""Real FFmpeg tests: timing, silence, full decode, captions and safe inputs."""
import array
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from studio.media import MediaError, _nvenc_available, build_plan, inspect_media, remap_srt, render_plan


class EncoderProbeTests(unittest.TestCase):
    def test_nvenc_probe_uses_safe_video_geometry_and_frame_rate(self):
        _nvenc_available.cache_clear()
        try:
            with patch("studio.media._tool", return_value="ffmpeg"), patch("studio.media._run") as runner:
                self.assertTrue(_nvenc_available())
            arguments = runner.call_args.args[0]
            self.assertEqual(arguments[arguments.index("-i") + 1], "color=size=640x360:rate=30")
            self.assertEqual(arguments[arguments.index("-pix_fmt") + 1], "yuv420p")
        finally:
            _nvenc_available.cache_clear()


class SubtitleTests(unittest.TestCase):
    def test_cue_mapping_joins_removed_pause_without_duplicate_text(self):
        clips = [dict(source_in=1, source_out=3, timeline_start=0),
                 dict(source_in=5, source_out=7, timeline_start=2)]
        result = remap_srt("1\n00:00:02,000 --> 00:00:06,000\n完整的一句话\n", clips)
        self.assertIn("00:00:01,000 --> 00:00:03,000", result)
        self.assertEqual(result.count("完整的一句话"), 1)

    def test_removed_cue_disappears(self):
        self.assertEqual(remap_srt("1\n00:00:03,100 --> 00:00:04,900\nnoise\n",
            [dict(source_in=0, source_out=3, timeline_start=0)]), "")

    def test_invalid_timestamp_is_rejected(self):
        with self.assertRaises(MediaError):
            remap_srt("1\nbad --> invalid\nwords", [])


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is required")
class MediaIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="cutflow-media-tests-")
        cls.directory = Path(cls.temp.name)
        cls.source = cls.directory / "speech-pattern.mp4"
        # Four seconds of audible tone separated by exactly measured silences.
        cls._ffmpeg("-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=8",
                    "-f", "lavfi", "-i", "aevalsrc=if(between(t\\,1\\,3)+between(t\\,5\\,7)\\,0.15*sin(2*PI*440*t)\\,0):s=48000:d=8",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "192k", str(cls.source))
        cls.settings = dict(width=320, height=180, crf=22)

    @classmethod
    def _ffmpeg(cls, *args):
        result = subprocess.run([shutil.which("ffmpeg"), "-hide_banner", "-v", "error", "-y", *args],
                                capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode:
            raise RuntimeError(result.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_silence_edit_retains_tones_and_produces_valid_av(self):
        before = hashlib.sha256(self.source.read_bytes()).hexdigest()
        plan = build_plan(self.source, self.settings)
        self.assertEqual(len(plan["clips"]), 2)
        self.assertAlmostEqual(plan["duration"], 4.8, delta=0.1)
        self.assertLessEqual(plan["clips"][0]["source_in"], 1)
        self.assertGreaterEqual(plan["clips"][0]["source_out"], 3)
        self.assertLessEqual(plan["clips"][1]["source_in"], 5)
        self.assertGreaterEqual(plan["clips"][1]["source_out"], 7)
        json.dumps(plan)
        result = render_plan(plan, self.directory / "edited.mp4")
        self.assertTrue(result["qc"]["passed"], result["qc"])
        self.assertAlmostEqual(result["duration"], 4.8, delta=0.15)
        self.assertEqual(result["metadata"]["audio_sample_rate"], 48000)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), before)
        self.assertTrue(any(c["name"] == "full_decode" and c["passed"] for c in result["qc"]["checks"]))

    def test_no_audio_preserves_duration(self):
        silent = self.directory / "no-audio.mp4"
        self._ffmpeg("-i", str(self.source), "-t", "1.2", "-an", "-c:v", "copy", str(silent))
        plan = build_plan(silent, self.settings)
        self.assertFalse(plan["source_info"]["has_audio"])
        self.assertEqual(len(plan["clips"]), 1)
        result = render_plan(plan, self.directory / "no-audio-output.mp4")
        self.assertFalse(result["metadata"]["has_audio"])
        self.assertTrue(result["qc"]["passed"])

    def test_manual_ranges_reject_reorder_and_overrun(self):
        for ranges in ([[4, 5], [1, 2]], [[0, 9]], [[0, float("inf")]]):
            with self.assertRaises(MediaError):
                build_plan(self.source, {**self.settings, "remove_silence": False, "source_ranges": ranges})

    def test_original_cannot_be_overwritten(self):
        plan = build_plan(self.source, {**self.settings, "remove_silence": False})
        with self.assertRaises(MediaError):
            render_plan(plan, self.source)

    def test_srt_sidecar_is_retimed_to_real_edit(self):
        source_srt = self.directory / "source.srt"
        source_srt.write_text("1\n00:00:05,000 --> 00:00:07,000\n第二段内容\n", encoding="utf-8")
        plan = build_plan(self.source, {**self.settings, "srt_path": str(source_srt)})
        report = render_plan(plan, self.directory / "subtitled.mp4")
        rendered_srt = Path(report["srt_path"]).read_text(encoding="utf-8")
        self.assertIn("第二段内容", rendered_srt)
        self.assertNotIn("00:00:05,000", rendered_srt)
        self.assertTrue(report["qc"]["passed"])

    def test_many_non_frame_aligned_cuts_do_not_accumulate_hidden_padding(self):
        plan = build_plan(self.source, {**self.settings, "remove_silence": False,
            "source_ranges": [[i * 0.3, i * 0.3 + 0.101] for i in range(24)]})
        result = render_plan(plan, self.directory / "many-cuts.mp4")
        self.assertEqual(len(plan["clips"]), 24)
        self.assertTrue(all(clip["frames"] == 4 for clip in plan["clips"]))
        self.assertAlmostEqual(plan["duration"], 3.2, delta=0.001)
        self.assertAlmostEqual(result["duration"], plan["duration"], delta=1/30)
        self.assertAlmostEqual(result["metadata"]["audio_duration"], plan["duration"], delta=0.05)
        self.assertTrue(result["qc"]["passed"])

    def test_disguised_playlist_is_not_followed(self):
        fake = self.directory / "playlist-disguised.mp4"
        fake.write_text("#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10,\nhttp://127.0.0.1:9/never-request\n#EXT-X-ENDLIST\n")
        with self.assertRaises(MediaError):
            inspect_media(fake)
        playlist = self.directory / "playlist.m3u8"
        playlist.write_text(fake.read_text())
        with self.assertRaises(MediaError) as caught:
            inspect_media(playlist)
        self.assertIn("whitelist", str(caught.exception))

    def test_source_changed_after_plan_is_rejected(self):
        plan = build_plan(self.source, {**self.settings, "remove_silence": False})
        plan["source_info"]["modified_ns"] -= 1
        with self.assertRaisesRegex(MediaError, "Source changed"):
            render_plan(plan, self.directory / "changed-source.mp4")

    def test_source_subtitles_cannot_be_overwritten(self):
        source_srt = self.directory / "collision.srt"
        source_srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nsource words\n", encoding="utf-8")
        plan = build_plan(self.source, {**self.settings, "srt_path": str(source_srt)})
        with self.assertRaisesRegex(MediaError, "overwrite source subtitles"):
            render_plan(plan, self.directory / "collision.mp4")

    def test_audio_start_offset_is_preserved_through_render(self):
        source = self.directory / "offset-audio.mp4"
        self._ffmpeg("-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=3",
                     "-itsoffset", "1", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1",
                     "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(source))
        plan = build_plan(source, {**self.settings, "remove_silence": False, "normalize_audio": False})
        result = render_plan(plan, self.directory / "offset-output.mp4")
        pcm = subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-i", result["output_path"],
                              "-vn", "-ac", "1", "-ar", "48000", "-f", "s16le", "-"],
                             capture_output=True, check=True).stdout
        samples = array.array("h", pcm)
        audible = [i / 48000 for i in range(0, len(samples), 480)
                   if max(abs(value) for value in samples[i:i+480]) > 1000]
        self.assertAlmostEqual(audible[0], 1.0, delta=0.06)
        self.assertAlmostEqual(audible[-1], 2.0, delta=0.06)
        self.assertTrue(result["qc"]["passed"])

    def test_entirely_silent_track_preserves_source_for_review(self):
        source = self.directory / "all-silence.mp4"
        self._ffmpeg("-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=2",
                     "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-t", "2",
                     "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(source))
        plan = build_plan(source, self.settings)
        self.assertEqual(len(plan["clips"]), 1)
        self.assertAlmostEqual(plan["duration"], 2.0, delta=0.05)
        self.assertTrue(any("静音阈值" in warning for warning in plan["warnings"]))
        result = render_plan(plan, self.directory / "all-silence-output.mp4")
        self.assertTrue(result["qc"]["passed"])

    def test_small_output_falls_back_to_cpu_before_nvenc_initialization(self):
        plan = build_plan(self.source, {**self.settings, "remove_silence": False,
            "encoder": "nvenc", "width": 128, "height": 128, "source_ranges": [[1, 2]]})
        with patch("studio.media._nvenc_available", return_value=True) as hardware_probe:
            result = render_plan(plan, self.directory / "tiny-output.mp4")
        hardware_probe.assert_not_called()
        self.assertEqual(result["encoder"], "libx264")
        self.assertTrue(result["qc"]["passed"])
        self.assertTrue(any("输出尺寸较小" in warning for warning in result["warnings"]))


if __name__ == "__main__":
    unittest.main()
