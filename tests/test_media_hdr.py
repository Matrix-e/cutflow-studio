"""Actual PQ/HLG pixels, tone mapping, audio, full decode and composition tests."""
import hashlib
from pathlib import Path
import shutil
import subprocess

import pytest

from studio import media,creative


pytestmark=pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'),reason='FFmpeg is required')


def ffmpeg(*args):
    result=subprocess.run([shutil.which('ffmpeg'),'-v','error','-y',*map(str,args)],capture_output=True,timeout=90)
    assert result.returncode==0,result.stderr.decode('utf-8',errors='replace')
    return result.stdout


def hdr_source(folder,transfer):
    target=folder/(transfer+'.mkv')
    # Transform known SDR samples into a real 10-bit BT.2020 HDR encoding. Merely
    # adding transfer tags to SDR pixels would not exercise the failed workflow.
    conversion=('zscale=pin=bt709:tin=bt709:min=bt709:rin=tv:'
                f'p=bt2020:t={transfer}:m=bt2020nc:r=tv:npl=100,format=yuv420p10le')
    ffmpeg('-f','lavfi','-i','testsrc2=size=192x128:rate=10:duration=1.2',
           '-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=1.2',
           '-vf',conversion,'-c:v','ffv1','-level','3','-pix_fmt','yuv420p10le',
           '-color_primaries','bt2020','-color_trc',transfer,'-colorspace','bt2020nc','-color_range','tv',
           '-c:a','pcm_s16le',target)
    info=media.inspect_media(target)
    assert info['color_transfer']==transfer and info['color_primaries']=='bt2020'
    return target


def rgb_frame(path,extra_filter=None):
    args=['-i',str(path),'-frames:v','1']
    if extra_filter:args+=['-vf',extra_filter]
    return ffmpeg(*args,'-f','rawvideo','-pix_fmt','rgb24','-')


@pytest.mark.parametrize('transfer',['smpte2084','arib-std-b67'])
def test_real_hdr_pixels_are_tonemapped_with_audio_and_full_decode(tmp_path,transfer):
    source=hdr_source(tmp_path,transfer);original=hashlib.sha256(source.read_bytes()).hexdigest()
    settings={'target_aspect':'original','width':192,'height':128,'fps':10,'encoder':'cpu',
              'normalize_audio':False,'remove_silence':False,'source_ranges':[[0,.4],[.6,1.2]]}
    plan=media.build_plan(source,settings);report=media.render_plan(plan,tmp_path/'sdr.mp4')
    assert report['qc']['passed'] is True
    checks={item['name']:item for item in report['qc']['checks']}
    assert checks['hdr_to_sdr_color']['passed'] and checks['full_decode']['passed']
    assert report['color_workflow']['input']['color_transfer']==transfer
    assert report['metadata']['color_transfer']=='bt709'
    assert report['metadata']['color_primaries']=='bt709' and report['metadata']['color_space']=='bt709'
    assert report['metadata']['color_range']=='tv'
    assert report['metadata']['audio_codec']=='aac' and abs(report['duration']-1)<.05
    assert [(c['source_in'],c['source_out']) for c in report['rendered_clips']]==[(0,.4),(.6,1.2)]
    assert hashlib.sha256(source.read_bytes()).hexdigest()==original
    converted=rgb_frame(report['output_path'])
    unconverted=rgb_frame(source,'format=yuv420p')
    assert len(converted)==len(unconverted)==192*128*3
    mean_difference=sum(abs(a-b) for a,b in zip(converted,unconverted))/len(converted)
    assert mean_difference>5,mean_difference  # A metadata-only relabel cannot pass.
    assert max(converted)-min(converted)>150 and 20<sum(converted)/len(converted)<220


def test_hdr_common_creation_path_keeps_canvas_and_audio(tmp_path):
    source=hdr_source(tmp_path,'arib-std-b67')
    plan=creative.build_creation_plan([str(source)],'',{'aspect':'9:16','pace':'preserve',
        'target_duration':1,'keep_original_audio':True,'music':False},
        {'width':192,'height':342,'fps':10,'encoder':'cpu','normalize_audio':False})
    result=creative.render_creation_plan(plan,tmp_path/'vertical.mp4')
    assert result['qc']['passed'] and result['metadata']['color_transfer']=='bt709'
    assert (result['metadata']['width'],result['metadata']['height'])==(192,342)
    rgb=rgb_frame(result['output_path'])
    top=rgb[:192*40*3];middle=rgb[192*140*3:192*200*3]
    assert sum(top)/len(top)<3  # Fit/pad keeps the whole landscape image.
    assert sum(middle)/len(middle)>20
    assert result['metadata']['has_audio'] and abs(result['duration']-1)<.05


def test_hdr_missing_matrix_is_not_silently_guessed():
    with pytest.raises(media.MediaError,match='metadata'):
        media._hdr_workflow({'color_transfer':'smpte2084','color_primaries':'bt2020','color_space':None})
