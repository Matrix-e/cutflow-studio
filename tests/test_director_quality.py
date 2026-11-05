from pathlib import Path
from unittest.mock import patch
import json
import shutil
import subprocess
import tempfile
import unittest

from studio import director_quality as quality


class DirectorQualityTests(unittest.TestCase):
    def test_missing_dependencies_are_reported_together(self):
        with tempfile.TemporaryDirectory() as root, patch('studio.local_story_provider.get_provider',return_value=None), \
             patch.object(quality.captions,'configuration_status',return_value={'configured':False}), \
             patch.object(quality.narration,'configuration_status',return_value={'configured':False}):
            result=quality.preflight([], 'auto', {},data=root,work_dir=root,active=False)
        self.assertFalse(result['ready'])
        self.assertTrue({'sources','planning','vision','asr','voice'}.issubset({c['id'] for c in result['blockers']}))
        self.assertTrue(all(c['solution'] for c in result['blockers']))
        self.assertIsNone(result['verified_at'])

    def test_subtitle_parser_keeps_time_and_text(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root)/'captions.srt'
            p.write_text('1\n00:00:00,250 --> 00:00:02,000\n没有完成交车。\n',encoding='utf-8')
            self.assertEqual(quality._segments(p),[{'start':.25,'end':2.,'text':'没有完成交车。'}])

    def test_black_and_tail_freeze_have_exact_positions(self):
        self.assertEqual(quality._intervals('black_start:1.25 black_end:2 black_duration:0.75','black',5),
                         [{'start':1.25,'end':2.,'duration':.75}])
        self.assertEqual(quality._intervals('lavfi.freezedetect.freeze_start: 3.2','freeze',5),
                         [{'start':3.2,'end':5,'duration':1.7999999999999998}])

    def test_missing_actual_media_cannot_pass(self):
        with tempfile.TemporaryDirectory() as root:
            result=quality.verify_delivery(Path(root)/'missing.mp4',{},work_dir=root,data=root)
            self.assertFalse(result['passed'])
            self.assertEqual(result['issues'][0]['code'],'inspection_complete')
            self.assertTrue((Path(root)/'quality/report.json').exists())


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'Requires actual FFmpeg')
class DirectorQualityMediaTests(unittest.TestCase):
    """Real encode/decode and signal inspection; only model inference is replaced.

    Model answers intentionally claim everything is good, to establish that
    independent media checks still reject broken delivery files.
    """
    script='这里是实际现场的记录。'

    @classmethod
    def setUpClass(cls):
        cls.temporary=tempfile.TemporaryDirectory();cls.root=Path(cls.temporary.name)
        cls.ffmpeg=quality.media._tool('ffmpeg')
        for name,visual,audio in [
            ('normal','testsrc2=s=320x180:r=24:d=3','sine=frequency=440:sample_rate=48000:duration=3'),
            ('silent','testsrc2=s=320x180:r=24:d=3','anullsrc=r=48000:cl=mono'),
            ('black','color=black:s=320x180:r=24:d=3','sine=frequency=440:sample_rate=48000:duration=3'),
            ('freeze','color=gray:s=320x180:r=24:d=3','sine=frequency=440:sample_rate=48000:duration=3')]:
            subprocess.run([cls.ffmpeg,'-v','error','-y','-f','lavfi','-i',visual,
                '-f','lavfi','-i',audio,'-t','3','-c:v','libx264','-preset','ultrafast',
                '-pix_fmt','yuv420p','-c:a','aac','-movflags','+faststart',str(cls.root/(name+'.mp4'))],
                check=True,capture_output=True,timeout=60)
        subprocess.run([cls.ffmpeg,'-v','error','-y','-i',str(cls.root/'normal.mp4'),
            '-itsoffset','0.7','-i',str(cls.root/'normal.mp4'),'-map','0:v:0','-map','1:a:0',
            '-c','copy',str(cls.root/'delayed.mp4')],check=True,capture_output=True,timeout=60)

    @classmethod
    def tearDownClass(cls):cls.temporary.cleanup()

    def inspect(self, filename='normal', *, mutate=None, asr=None, subtitle='normal'):
        with tempfile.TemporaryDirectory() as work:
            folder=Path(work);srt=folder/'final.srt'
            srt.write_text('1\n00:00:00,200 --> 00:00:02,700\n'+self.script+'\n',encoding='utf-8')
            plan={'mode':'vlog','theme':'现场记录','viewpoint':'用现场细节说明过程','audience_value':'了解真实过程',
                  'actual_script':self.script,'scenes':[{'visual_description':'抽象运动测试画面','spoken_text':self.script}],
                  'content_review':{'passed':True},'options':{'aspect':'16:9','subtitles':True},'srt_path':str(srt)}
            report={'duration':3,'srt_path':str(srt),'timeline':[{'timeline_start':0,'timeline_end':3}]}
            if subtitle=='shifted':srt.write_text('1\n00:00:01,800 --> 00:00:02,900\n'+self.script+'\n',encoding='utf-8')
            if mutate:mutate(plan,report,folder)
            inference=asr or {'segments':[{'start':.2,'end':2.7,'text':self.script}]}
            provider=lambda *_:json.dumps({'scene_matches':True,'subject_intact':True,'caption_readable':True,'reason':'controlled model answer'})
            with patch('studio.local_story_provider.get_provider',return_value=provider), \
                 patch.object(quality.captions,'_transcribe',return_value=inference):
                result=quality.verify_delivery(self.root/(filename+'.mp4'),plan,
                    work_dir=folder,data=folder,render_result=report)
            self.assertTrue((folder/'quality/report.json').is_file())
            return result

    def test_actual_nondefective_media_can_pass_independent_technical_checks(self):
        result=self.inspect();self.assertTrue(result['passed'],result['issues'])

    def test_actual_silence_fails_despite_positive_model_answers(self):
        result=self.inspect('silent');self.assertFalse(result['passed'])
        self.assertIn('audible_sound',{i['code'] for i in result['issues']})

    def test_actual_black_frames_fail_and_report_time_range(self):
        result=self.inspect('black');self.assertFalse(result['passed'])
        issue=next(i for i in result['issues'] if i['code']=='black_frames')
        self.assertTrue(issue['ranges']);self.assertGreater(issue['ranges'][0]['duration'],2.5)

    def test_actual_frozen_picture_fails_and_reports_tail(self):
        result=self.inspect('freeze');self.assertFalse(result['passed'])
        issue=next(i for i in result['issues'] if i['code']=='frozen_frames')
        self.assertTrue(issue['ranges']);self.assertGreater(issue['ranges'][0]['duration'],2.5)

    def test_wrong_delivered_aspect_fails_using_actual_metadata(self):
        result=self.inspect(mutate=lambda p,r,f:p['options'].update(aspect='9:16'))
        self.assertFalse(result['passed']);self.assertIn('aspect',{i['code'] for i in result['issues']})

    def test_equal_stream_lengths_do_not_hide_actual_audio_clock_offset(self):
        info=quality.media.inspect_media(self.root/'delayed.mp4')
        self.assertGreater(info['audio_start']-info['video_start'],.6)
        self.assertLess(abs(info['audio_duration']-info['duration']),.05)
        result=self.inspect('delayed');self.assertFalse(result['passed'], 'A 0.7 s audio delay must fail independently of ASR text')

    def test_matching_subtitle_text_with_wrong_audio_timing_cannot_pass(self):
        result=self.inspect(subtitle='shifted',asr={'segments':[{'start':.2,'end':1.2,'text':self.script}]})
        self.assertFalse(result['passed'], 'Subtitle cues are in bounds but do not cover the actual spoken sentence')

    def test_repaired_render_subtitle_is_verified_instead_of_stale_plan_subtitle(self):
        def change(plan,report,folder):
            wrong=folder/'stale.srt';wrong.write_text('1\n00:00:00,200 --> 00:00:10,000\n'+self.script+'\n',encoding='utf-8')
            plan['srt_path']=str(wrong)
        result=self.inspect(mutate=change)
        self.assertTrue(result['passed'],result['issues'])

    def test_music_only_without_locked_speech_cannot_be_marked_complete(self):
        def change(plan,report,folder):
            plan.pop('actual_script');plan['scenes'][0].pop('spoken_text');plan['options']['subtitles']=False;plan.pop('srt_path');report.pop('srt_path')
            (folder/'final.srt').unlink()
        result=self.inspect(mutate=change)
        self.assertFalse(result['passed'],'A video with no locked speech content cannot prove an actual spoken deliverable')

    def test_caption_repair_uses_retained_timeline_after_pause_removal(self):
        with tempfile.TemporaryDirectory() as work:
            timeline=[{'source_in':0,'source_out':7,'timeline_start':0,'timeline_end':3,
                       'speech':[{'start':0,'end':1,'text':'出发了。'},{'start':5,'end':7,'text':'到达现场。'}],
                       'retained_ranges':[{'source_in':0,'source_out':1,'timeline_start':0,'timeline_end':1},
                                          {'source_in':5,'source_out':7,'timeline_start':1,'timeline_end':3}]}]
            result=quality.repair_delivery(self.root/'normal.mp4',{},
                {'issues':[{'code':'subtitle_file'}]},work_dir=work,data=work,
                render_result={'timeline':timeline,'subtitles_burned':False})
            self.assertTrue(Path(result['output_path']).is_file())
            cues=quality._segments(result['srt_path'])
            self.assertEqual([(x['start'],x['end']) for x in cues],[(0,1),(1,3)])


if __name__=='__main__':unittest.main()
