import hashlib
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from studio.caption_model import validate_offsets, CaptionTimingError
from studio.captions import add_captions, CaptionError, _validated_segments, _run


class CaptionTimingTests(unittest.TestCase):
    def test_chunk_offset_and_endpoint_rounding(self):
        result=validate_offsets([{'text':' 最后一段 ','timestamp':(0,4.04)}],chunk_start=60,chunk_duration=4.01)
        self.assertEqual(result,[{'start':60,'end':64.01,'text':'最后一段'}])

    def test_rejects_unknown_overlapping_or_hallucinated_timestamps(self):
        for offsets in ([{'text':'时间未知','timestamp':(0,None)}],
                        [{'text':'越界','timestamp':(0,29.98)}],
                        [{'text':'甲','timestamp':(0,2)},{'text':'乙','timestamp':(1,3)}],
                        [{'text':'重复重复重复重复重复重复','timestamp':(0,2)}]):
            with self.subTest(offsets=offsets),self.assertRaises(CaptionTimingError):
                validate_offsets(offsets,chunk_start=0,chunk_duration=4)

    def test_rejects_partial_clock_and_empty_transcript(self):
        with self.assertRaises(CaptionError):
            _validated_segments({'clock':'final_video_seconds','duration':2,'segments':[]},2)
        with self.assertRaises(CaptionError):
            _validated_segments({'clock':'final_video_seconds','duration':1,'segments':[{'start':0,'end':1,'text':'甲'}]},2)

    def test_cancellation_terminates_child(self):
        with tempfile.TemporaryDirectory() as folder:
            stop=threading.Event();timer=threading.Timer(.15,stop.set);timer.start()
            try:
                with self.assertRaisesRegex(CaptionError,'取消'):
                    _run([sys.executable,'-c','import time;time.sleep(30)'],timeout=60,
                         log_path=Path(folder)/'log',stop_event=stop)
            finally:timer.cancel()


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'FFmpeg required')
class CaptionRenderTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name).resolve()
        self.video=self.root/'final.mp4'
        subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','color=c=blue:s=320x320:r=30',
            '-f','lavfi','-i','sine=frequency=400:sample_rate=48000','-t','2',
            '-c:v','libx264','-c:a','aac','-pix_fmt','yuv420p',str(self.video)],check=True)
        self.report={'clock':'final_video_seconds','duration':2,'segments':[{'start':.1,'end':1.8,'text':'托管之前，先核对交接记录。'}]}

    def tearDown(self):self.temp.cleanup()

    def audio_hash(self,path):
        return subprocess.run(['ffmpeg','-v','error','-i',str(path),'-map','0:a:0','-c:a','copy',
            '-f','hash','-hash','sha256','-'],capture_output=True,check=True).stdout

    def test_real_burn_preserves_audio_and_source_backup(self):
        original=self.video.read_bytes();audio=self.audio_hash(self.video)
        with patch('studio.captions._transcribe',return_value=self.report):
            result=add_captions(self.video,self.root/'work')
        self.assertTrue(result['qc']['passed'])
        self.assertTrue(result['subtitles_burned'])
        self.assertEqual(Path(result['uncaptioned_path']).read_bytes(),original)
        self.assertEqual(audio,self.audio_hash(self.video))
        self.assertIn('托管之前',Path(result['srt_path']).read_text(encoding='utf-8'))
        self.assertNotEqual(hashlib.sha256(self.video.read_bytes()).digest(),hashlib.sha256(original).digest())

    def test_bad_recognition_does_not_replace_output(self):
        original=self.video.read_bytes()
        with patch('studio.captions._transcribe',return_value={**self.report,'segments':[]}):
            with self.assertRaises(CaptionError):add_captions(self.video,self.root/'work')
        self.assertEqual(self.video.read_bytes(),original)


if __name__=='__main__':unittest.main()
