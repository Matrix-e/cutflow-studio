"""Real mixed video/image exports: planned static UI is not an accidental freeze."""
import copy
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image, ImageDraw

from studio import director_quality as quality
from studio.director_brand import _canvas


@pytest.fixture(scope='module')
def mixed_media(tmp_path_factory):
    if not shutil.which('ffmpeg'):pytest.skip('actual FFmpeg required')
    data=tmp_path_factory.mktemp('mixed-brand-quality')
    source=data/'productions'/'version'/'brand-assets'/'fixture.png';source.parent.mkdir(parents=True)
    picture=Image.new('RGB',(200,280),'white');draw=ImageDraw.Draw(picture)
    draw.rectangle((0,0,199,45),fill=(30,88,66))
    for i,color in enumerate(['navy','orange','teal']):
        draw.rectangle((14,70+i*57,49,104+i*57),fill=color)
        draw.rectangle((65,74+i*57,182,80+i*57),fill='black')
        draw.rectangle((65,91+i*57,152,96+i*57),fill='gray')
    picture.save(source)
    asset={'id':'a'*32,'path':str(source),'width':200,'height':280,
           'sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'source_reference':'isolated test fixture'}
    canvas=data/'canvas.png';rect=_canvas(asset,320,180,canvas)
    wrong=data/'wrong.png'
    with Image.open(canvas) as image:
        draw=ImageDraw.Draw(image);draw.rectangle((rect['x'],rect['y'],rect['x']+rect['width']-1,rect['y']+rect['height']-1),fill='white')
        image.save(wrong)
    for name,image,first in [('mixed',canvas,'testsrc2=s=320x180:r=24:d=2'),
                             ('wrong',wrong,'testsrc2=s=320x180:r=24:d=2'),
                             ('video_freeze',canvas,'color=gray:s=320x180:r=24:d=2')]:
        command=[quality.media._tool('ffmpeg'),'-v','error','-y','-f','lavfi','-i',first,
                 '-loop','1','-framerate','24','-i',str(image),'-f','lavfi','-i','testsrc2=s=320x180:r=24:d=2',
                 '-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=7',
                 '-filter_complex','[1:v]trim=duration=3,setpts=PTS-STARTPTS,format=yuv420p[img];[0:v][img][2:v]concat=n=3:v=1:a=0[v]',
                 '-map','[v]','-map','3:a','-t','7','-c:v','libx264','-preset','ultrafast','-pix_fmt','yuv420p',
                 '-c:a','aac',str(data/(name+'.mp4'))]
        subprocess.run(command,check=True,capture_output=True,timeout=60)
    return data,asset,rect


def check_export(fixture,work,name='mixed',mutate=None):
    data,asset,rect=fixture
    speech='展示实际界面，帮助了解入口。'
    scenes=[{'visual_description':'动态测试画面','spoken_text':speech},
            {'kind':'brand_screenshot','brand_media_id':asset['id'],'brand_asset':copy.deepcopy(asset),
             'visual_description':'真实界面示例','spoken_text':speech},
            {'visual_description':'动态测试画面','spoken_text':speech}]
    plan={'theme':'认识界面','viewpoint':'只说明界面，不证明业务结果','audience_value':'识别入口',
          'actual_script':speech,'scenes':scenes,'brand_assets_snapshot':[copy.deepcopy(asset)],
          'content_review':{'passed':True},'options':{'aspect':'16:9','subtitles':False}}
    timeline=[{'timeline_start':0.,'timeline_end':2.},
              {**copy.deepcopy(scenes[1]),'timeline_start':2.,'timeline_end':5.,'planned_static':True,
               'brand_image_sha256':asset['sha256'],'brand_image_rect':copy.deepcopy(rect)},
              {'timeline_start':5.,'timeline_end':7.}]
    report={'duration':7.,'timeline':timeline}
    if mutate:mutate(plan,report)
    provider=lambda *_:json.dumps({'scene_matches':True,'subject_intact':True,'caption_readable':True,'reason':'controlled positive'})
    with patch('studio.local_story_provider.get_provider',return_value=provider),patch.object(quality.captions,'_transcribe',
        return_value={'segments':[{'start':.2,'end':6.8,'text':speech}]}):
        return quality.verify_delivery(data/(name+'.mp4'),plan,work_dir=work,data=data,render_result=report)


def test_real_screenshot_static_scene_passes_with_pixel_and_source_evidence(mixed_media,tmp_path):
    result=check_export(mixed_media,tmp_path)
    assert result['passed'],result['issues']
    evidence=json.loads((tmp_path/'quality/planned-static-review.json').read_text())
    assert evidence['detected_freezes'] and evidence['verified_screenshot_intervals']==[[2.,5.]]
    assert not evidence['unplanned_freezes']


def test_wrong_exported_ui_fails_despite_model_saying_match(mixed_media,tmp_path):
    result=check_export(mixed_media,tmp_path,'wrong')
    assert not result['passed']
    assert 'brand_screenshot_identity' in {x['code'] for x in result['issues']}


def test_video_freeze_outside_verified_image_is_still_blocked(mixed_media,tmp_path):
    result=check_export(mixed_media,tmp_path,'video_freeze')
    issue=next(x for x in result['issues'] if x['code']=='frozen_frames')
    assert any(x['start']<1 and x['end']<=2.1 for x in issue['ranges'])


def test_claimed_image_hash_cannot_exempt_frozen_video(mixed_media,tmp_path):
    result=check_export(mixed_media,tmp_path,mutate=lambda p,r:r['timeline'][1].update(brand_image_sha256='0'*64))
    assert not result['passed']
    assert 'inspection_complete' in {x['code'] for x in result['issues']}


def test_missing_snapshot_cannot_use_arbitrary_model_path(mixed_media,tmp_path):
    result=check_export(mixed_media,tmp_path,mutate=lambda p,r:p.update(brand_assets_snapshot=[]))
    assert not result['passed']


def test_freeze_crossing_image_and_video_keeps_unplanned_remainder():
    result=quality._unplanned_freezes([{'start':0.,'end':8.}],[(2.,5.)])
    assert result==[{'start':0.,'end':2.,'duration':2.},{'start':5.,'end':8.,'duration':3.}]
