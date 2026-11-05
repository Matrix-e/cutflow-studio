import hashlib
import base64
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import wave
import math
import struct
import threading

from studio import auto_assembly as assembly, media, narration, narrated


def source(index=0, duration=30, segments=None, usable=True):
    return {'source_index':index,'coverage':duration,'info':{'fps':30},
            'segments':segments or [],'samples':[{'time':float(n),'score':.8,'usable':usable} for n in range(math.ceil(duration))]}


def audio(duration=18):
    return {'duration':duration,'segments':[{'start':0,'end':duration/2-.1,'text':'交车之前确认合同和费用。'},
        {'start':duration/2,'end':duration,'text':'车辆交接拍照，保留记录。'}]}


class SelectionTests(unittest.TestCase):
    def test_sentence_boundaries_frames_and_no_timeline_drift(self):
        for duration in (2.8,6.2,9.01,18.033,45.1,151.119):
            units=assembly._narration_units(audio(duration),30)
            self.assertEqual(sum(s['frames'] for s in units),math.ceil(duration*30-1e-8))
            self.assertEqual(units[0]['start_frame'],0)
            for previous,current in zip(units,units[1:]):
                self.assertEqual(previous['end_frame'],current['start_frame'])
            if duration>=3:
                self.assertTrue(all(90<=s['frames']<=180 for s in units))

    def test_text_match_selects_middle_not_file_start(self):
        sources=[source(0,30,[{'start':12,'end':19,'text':'交车之前确认合同和费用。'}]),
                 source(1,30,[{'start':20,'end':26,'text':'车辆交接拍照，保留记录。'}])]
        shots=assembly._choose_shots(sources,audio(9),30)
        self.assertEqual([s['source_index'] for s in shots],[0,1])
        self.assertTrue(all(s['source_in']>0 for s in shots))
        self.assertTrue(all(s['selection_basis']=='speech_text_match' for s in shots))

    def test_repeated_topic_cannot_reuse_source_interval(self):
        sources=[source(0,45,[{'start':10,'end':30,'text':'车辆交接拍照，保留记录。'}]),source(1,30)]
        shots=assembly._choose_shots(sources,audio(36),30)
        for index in range(2):
            used=sorted((s['source_in'],s['source_out']) for s in shots if s['source_index']==index)
            for first,second in zip(used,used[1:]):self.assertLessEqual(first[1],second[0])
        self.assertEqual(sum(s['frames'] for s in shots),1080)
        self.assertEqual(len({s['source_index'] for s in shots}),2)

    def test_silent_footage_is_only_technical_broll_and_balanced(self):
        shots=assembly._choose_shots([source(0),source(1),source(2)],audio(27),30)
        self.assertEqual(len({s['source_index'] for s in shots}),3)
        self.assertTrue(all(s['selection_basis']=='technical_broll' and not s['source_speech'] for s in shots))

    def test_insufficient_or_all_unusable_does_not_loop_or_hold(self):
        for sources,code in [([source(duration=5)],'material_shortfall'),
                             ([source(usable=False)],'no_usable_material')]:
            with self.assertRaises(assembly.AutoAssemblyError) as caught:
                assembly._choose_shots(sources,audio(18),30)
            self.assertEqual(caught.exception.code,code)

    def test_bad_samples_are_never_selected(self):
        item=source(duration=30)
        for sample in item['samples']:
            if sample['time']<10:sample['usable']=False
        shots=assembly._choose_shots([item],audio(9),30)
        self.assertTrue(all(s['source_in']>=10 for s in shots))

    def test_flat_or_black_frames_are_rejected(self):
        size=assembly.SAMPLE_WIDTH*assembly.SAMPLE_HEIGHT
        for value in (0,50,100,255):
            self.assertFalse(assembly._frame_quality(bytes([value])*size,1080)['usable'])
        self.assertTrue(assembly._frame_quality(bytes(range(256))*(size//256)+bytes(range(size%256)),1080)['usable'])

    def test_audio_offsets_map_to_source_video_clock(self):
        self.assertIn('adelay=500',assembly._audio_alignment({'audio_start':2.5,'video_start':2}))
        self.assertIn('atrim=start=0.250000',assembly._audio_alignment({'audio_start':1.75,'video_start':2}))


class AnalysisCacheTests(unittest.TestCase):
    def test_timestamp_cache_is_reused_but_source_change_invalidates(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);video=root/'one.mp4';video.write_bytes(b'one');work=root/'work';work.mkdir()
            info={'duration':20,'has_audio':True,'field_order':'progressive','fps':30}
            sample=[{'time':1,'score':.8,'usable':True}]
            with patch.object(media,'inspect_media',return_value=info),\
                 patch.object(assembly,'_sample_quality',return_value=(sample,1)) as quality,\
                 patch.object(assembly,'_timed_speech',return_value=([{'start':5,'end':10,'text':'交接合同'}],'recognized_unverified')) as speech:
                first=assembly._analyze_source(video,0,root,work,None)
                second=assembly._analyze_source(video,1,root,work,None)
                self.assertFalse(first['cached']);self.assertTrue(second['cached'])
                self.assertEqual(second['source_index'],1);self.assertEqual(speech.call_count,1)
                self.assertEqual(second['clock'],'source_video_seconds')
                video.write_bytes(b'changed source')
                third=assembly._analyze_source(video,0,root,work,None)
                self.assertFalse(third['cached']);self.assertEqual(quality.call_count,2)

    def test_missing_asr_model_does_not_pretend_to_have_transcript(self):
        with patch.object(assembly.captions,'configuration_status',return_value={'configured':False}):
            with self.assertRaises(assembly.AutoAssemblyError) as caught:
                assembly._timed_speech('video',{'has_audio':True},10,Path('.'),None)
        self.assertEqual(caught.exception.code,'analysis_unavailable')


class ThumbnailTests(unittest.TestCase):
    def test_optional_thumbnail_failure_or_missing_image_is_not_a_plan_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'prepared.mp4';path.write_bytes(b'placeholder')
            stale=path.with_suffix('.thumbnail.jpg');stale.write_bytes(b'old thumbnail')
            with patch.object(assembly,'_run',side_effect=assembly.AutoAssemblyError('decode failed')):
                self.assertIsNone(assembly._shot_thumbnail(path,120,30))
            self.assertFalse(stale.exists())
            with patch.object(assembly,'_run'):
                self.assertIsNone(assembly._shot_thumbnail(path,120,30))

    def test_cancellation_is_not_swallowed_as_missing_thumbnail(self):
        event=threading.Event();event.set()
        with self.assertRaises(assembly.AutoAssemblyError) as caught:
            assembly._shot_thumbnail(Path('unused.mp4'),120,30,event)
        self.assertEqual(caught.exception.code,'cancelled')


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'FFmpeg required')
class AssemblyIntegrationTests(unittest.TestCase):
    def test_thumbnail_is_middle_of_actual_prepared_clip_and_at_most_320_wide(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'prepared.mp4'
            media._run([media._tool('ffmpeg'),'-v','error','-y','-f','lavfi','-i',
                'color=red:size=640x360:rate=30:duration=1','-f','lavfi','-i',
                'color=blue:size=640x360:rate=30:duration=3','-filter_complex',
                '[0:v][1:v]concat=n=2:v=1:a=0[v]','-map','[v]','-c:v','libx264',str(path)])
            uri=assembly._shot_thumbnail(path,120,30)
            self.assertTrue(uri.startswith('data:image/jpeg;base64,'))
            image=path.with_suffix('.thumbnail.jpg')
            self.assertEqual(base64.b64decode(uri.split(',',1)[1]),image.read_bytes())
            probe=media._run([media._tool('ffprobe'),'-v','error','-show_entries','stream=width,height','-of','json',str(image)])
            info=json.loads(probe.stdout)['streams'][0]
            self.assertLessEqual(info['width'],320)
            # The original opening is red; the selected shot's middle is blue.
            raw=Path(temp)/'pixel.rgb'
            media._run([media._tool('ffmpeg'),'-v','error','-y','-i',str(image),'-vf',
                'scale=1:1','-frames:v','1','-pix_fmt','rgb24','-f','rawvideo',str(raw)])
            red,green,blue=raw.read_bytes()
            self.assertGreater(blue,200);self.assertLess(red,40)

    def test_real_no_audio_sources_prepare_and_render_exact_script_once(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);paths=[]
            for i in range(2):
                path=root/f'source-{i}.mp4'
                media._run([media._tool('ffmpeg'),'-v','error','-y','-f','lavfi','-i',
                    f'testsrc2=size=160x90:rate=30:duration=9','-an','-c:v','libx264','-pix_fmt','yuv420p',str(path)])
                paths.append(str(path))
            script=' 交车之前确认合同和费用。\n车辆交接拍照，保留记录。\n'
            def fake_voice(text,voice_id,output,**kwargs):
                # Deterministic audible audio fixture; no speech quality claim.
                rate=24000;duration=9.017
                with wave.open(str(output),'wb') as handle:
                    handle.setnchannels(1);handle.setsampwidth(2);handle.setframerate(rate)
                    handle.writeframes(b''.join(struct.pack('<h',int(8000*math.sin(2*math.pi*440*n/rate))) for n in range(round(duration*rate))))
                measured=narration.inspect_audio(output)
                segments=[{'start':0,'end':4.3,'text':'交车之前确认合同和费用。'},
                          {'start':4.5,'end':measured['duration'],'text':'车辆交接拍照，保留记录。'}]
                srt=output.with_suffix('.srt');srt.write_text(narration.make_srt(segments),encoding='utf-8')
                return {**measured,'segments':segments,'output_path':str(output),'srt_path':str(srt),
                        'sha256':narration._sha(output),'srt_sha256':narration._sha(srt),
                        'script_sha256':hashlib.sha256(text.strip().encode('utf-8')).hexdigest(),
                        'voice_id':voice_id,'style':kwargs['style'],'warnings':[]}
            events=[]
            with patch.object(narration,'synthesize',side_effect=fake_voice) as synthesize:
                plan=assembly.build_auto_assembly(paths,script,{'music':False,'subtitles':False},
                    {'width':160,'height':90,'fps':30},work_dir=root/'plan',data=root/'data',on_stage=events.append)
                synthesize.assert_called_once()
                self.assertEqual(plan['spoken_script'],script)
                self.assertEqual(plan['assembly']['script'],script)
                self.assertEqual(plan['assembly']['script_sha256'],hashlib.sha256(script.encode('utf-8')).hexdigest())
                self.assertFalse(plan['loop_visuals'])
                self.assertEqual(plan['assembly']['stats']['total_frames'],271)
                self.assertEqual(len({s['source_index'] for s in plan['assembly']['shots']}),2)
                self.assertTrue(all(s['selection_basis']=='technical_broll' for s in plan['assembly']['shots']))
                self.assertTrue(all(s['thumbnail'].startswith('data:image/jpeg;base64,') for s in plan['assembly']['shots']))
                self.assertEqual(assembly._count_frames(plan['assembly']['visual_master']),271)
                # Exercise the existing final renderer: it must not invoke TTS again.
                result=narrated.render_narrated_plan(plan,root/'finished.mp4')
                synthesize.assert_called_once()
                self.assertTrue(result['qc']['passed'])
                self.assertEqual(assembly._count_frames(root/'finished.mp4'),271)
            self.assertIn('transcribing',events);self.assertIn('preparing_voice',events)


if __name__=='__main__':unittest.main()
