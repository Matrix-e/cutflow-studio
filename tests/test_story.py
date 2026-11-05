import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from studio.story import _source_evidence,_scene_timing,_exact_visual_plan


class StoryEvidenceTests(unittest.TestCase):
    def test_missing_cache_runs_bounded_asr_and_marks_unverified(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'new.mp4';source.write_bytes(b'new')
            def worker(command,**kwargs):
                destination=Path(command[-2]);(destination/'speech.json').write_text(
                    json.dumps({'summary':'讨论交接记录','transcript':'交接时先核对车辆记录。'},ensure_ascii=False),encoding='utf-8')
            with patch('studio.story.media.inspect_media',return_value={'duration':420,'has_audio':True}),\
                 patch('studio.captions.configuration_status',return_value={'configured':True,'python':'python','model':'local'}),\
                 patch('studio.story.run_process',side_effect=worker) as run:
                result=_source_evidence(source,0,root,root/'work')
            self.assertEqual(run.call_count,1)
            self.assertEqual(result['speech_status'],'recognized_unverified')
            self.assertIn('300.0',result['speech_scope']);self.assertIn('未经人工校对',result['speech_scope'])
            self.assertFalse(result['speech_cached'])

    def test_missing_model_cannot_silently_claim_source_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'new.mp4';source.write_bytes(b'new')
            with patch('studio.story.media.inspect_media',return_value={'duration':20,'has_audio':True}),\
                 patch('studio.captions.configuration_status',return_value={'configured':False}),\
                 self.assertRaisesRegex(RuntimeError,'不能凭文件名'):
                _source_evidence(source,0,root,root/'work')

    def test_silent_source_is_explicit_not_invented(self):
        with patch('studio.story.media.inspect_media',return_value={'duration':20,'has_audio':False}),\
             patch('studio.story.run_process') as run:
            result=_source_evidence('silent.mp4',0,'unused','unused')
        self.assertEqual(result['speech'],'');self.assertEqual(result['speech_status'],'no_audio');run.assert_not_called()


class StoryTimingTests(unittest.TestCase):
    def test_cumulative_rounding_avoids_per_scene_drift(self):
        scenes=[{'narration':'一。'},{'narration':'二。'},{'narration':'三。'}]
        audio={'duration':3.049,'segments':[{'start':0},{'start':1.016},{'start':2.033}]}
        timing=_scene_timing(scenes,audio,30)
        self.assertEqual([s['end_frame'] for s in timing],[30,61,92])
        self.assertEqual(sum(s['frames'] for s in timing),92)
        for scene in timing[:-1]:self.assertLessEqual(abs(scene['timeline_end']-scene['audio_end']),1/60)
        self.assertGreaterEqual(timing[-1]['timeline_end'],audio['duration'])

    def test_generic_proportional_plan_is_not_allowed_to_shift_scene_boundaries(self):
        timing=[{'frames':30,'start_frame':0,'end_frame':30},{'frames':61,'start_frame':30,'end_frame':91}]
        base={'settings':{'fps':30},'clips':[{'source_index':0,'frames':31,'timeline_start':0,'timeline_end':31/30},
            {'source_index':1,'frames':60,'timeline_start':31/30,'timeline_end':91/30}]}
        with patch('studio.story.creative.build_creation_plan',return_value=base) as build,\
             self.assertRaisesRegex(RuntimeError,'未与旁白边界对齐'):
            _exact_visual_plan(['a.mp4','b.mp4'],timing,'9:16',{})
        self.assertIsNone(build.call_args.args[2]['target_duration'])


if __name__=='__main__':unittest.main()
