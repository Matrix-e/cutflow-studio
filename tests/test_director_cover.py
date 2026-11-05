"""Cover regression with actual original/finished frames, including HDR pixels."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from uuid import uuid4

from PIL import Image
import pytest

from studio import media
from studio.director_service import DirectorService,DirectorServiceError


pytestmark=pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'),reason='FFmpeg is required')


def ffmpeg(*arguments):
    process=subprocess.run([shutil.which('ffmpeg'),'-v','error','-y',*map(str,arguments)],
                           capture_output=True,timeout=90)
    assert process.returncode==0,process.stderr.decode('utf-8',errors='replace')


def source_validator(inbox,relative):
    path=(inbox/relative).resolve()
    if not path.is_relative_to(inbox.resolve()) or not path.is_file():raise ValueError('unsafe')
    return path


def make_record(tmp_path,mode='vlog'):
    inbox=tmp_path/'inbox';inbox.mkdir()
    source=inbox/'selected.mp4'
    ffmpeg('-f','lavfi','-i','color=c=blue:s=160x90:r=10:d=2',
           '-f','lavfi','-i','color=c=green:s=160x90:r=10:d=2',
           '-filter_complex','[1:v]drawbox=x=10:y=15:w=15:h=30:color=white:t=fill[v];[0:v][v]concat=n=2:v=1:a=0[out]',
           '-map','[out]','-c:v','libx264','-pix_fmt','yuv420p',source)
    other=inbox/'not-selected.mp4'
    ffmpeg('-f','lavfi','-i','color=c=yellow:s=160x90:r=10:d=4',
           '-c:v','libx264','-pix_fmt','yuv420p',other)
    service=DirectorService(tmp_path/'data',inbox,validate_source=source_validator)
    request={'sources':[other.name,source.name],'goal':'现场片段','mode':mode,'title':'',
             'options':{'aspect':'9:16'},'parent_id':None,'revision':None}
    record=service.create(uuid4(),request)
    plan={'mode':mode,'title':'One title','theme':'现场片段','posting_copy':'已选片段',
          'plan_sha256':'locked-plan','actual_script':'真实内容',
          'scenes':[{'source_index':1,'source_in':2,'source_out':3.8,
                     'source':'../../arbitrary.mp4','path':'../../arbitrary.mp4'}]}
    service.update(record['id'],plan=plan)
    folder=service.root/record['id'];folder.mkdir(exist_ok=True)
    output=folder/'finished.mp4'
    # The finished frame deliberately carries a large baked-in lower overlay.
    # Extracting the finished video, even at another timestamp, cannot pass.
    ffmpeg('-f','lavfi','-i','color=c=red:s=160x284:r=10:d=1',
           '-vf','drawbox=x=0:y=230:w=160:h=40:color=white:t=fill',
           '-c:v','libx264','-pix_fmt','yuv420p',output)
    srt=folder/'finished.srt';srt.write_text('1\n00:00:00,000 --> 00:00:01,000\n真实内容\n',encoding='utf-8')
    return service,service.get(record['id']),plan,output,srt,folder


@pytest.mark.parametrize('mode',['vlog','narrated'])
def test_cover_uses_locked_original_range_not_baked_finished_frames(tmp_path,mode):
    service,record,plan,output,srt,folder=make_record(tmp_path,mode)
    before={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in (*service.inbox.glob('*.mp4'),output)}
    artifacts=service._deliver(record,output,plan,{'srt_path':str(srt)},{'passed':True},folder)
    by_kind={item['kind']:Path(item['path']) for item in artifacts}
    with Image.open(by_kind['cover']) as cover:
        assert cover.size==(160,284)
        # Actual selected source at 2–3.8s is green; other source is yellow,
        # the earlier source range is blue, and the finished movie is red.
        center=cover.convert('RGB').getpixel((80,140))
        assert center[1]>80 and center[1]>center[0]*2 and center[1]>center[2]*2
        assert max(cover.convert('RGB').getpixel((80,250)))<5  # no baked lower overlay
        # White source detail at the left edge remains inside a fit/pad image.
        subject=cover.convert('RGB').getpixel((17,120))
        assert min(subject)>230
    report=json.loads(by_kind['record'].read_text(encoding='utf-8'))
    evidence=report['cover_source']
    assert evidence['source']=='selected.mp4' and evidence['source_index']==1
    assert 2<evidence['source_time']<3.8 and evidence['geometry']=='fit_without_crop'
    assert evidence['title_layers']==1 and evidence['inherits_finished_video_overlays'] is False
    assert evidence['color_workflow'] is None and len(evidence['candidates'])==3
    assert all(hashlib.sha256(path.read_bytes()).hexdigest()==digest for path,digest in before.items())


def test_screenshot_first_scene_cover_uses_frozen_source_and_keeps_whole_interface(tmp_path):
    from PIL import ImageDraw
    service,record,plan,output,srt,folder=make_record(tmp_path,'narrated')
    image_path=folder/'brand-assets'/'interface.png';image_path.parent.mkdir()
    picture=Image.new('RGB',(100,160),'cyan');draw=ImageDraw.Draw(picture)
    draw.rectangle((0,0,99,9),fill='white');draw.rectangle((0,150,99,159),fill='orange');picture.save(image_path)
    asset={'id':'a'*32,'path':str(image_path),'width':100,'height':160,
           'sha256':hashlib.sha256(image_path.read_bytes()).hexdigest(),'source_reference':'confirmed interface fixture'}
    record['snapshot']['brand_context']={'brand_assets':[asset]}
    plan['scenes']=[{'source_index':2,'kind':'brand_screenshot','brand_media_id':asset['id'],
                     'brand_asset':{**asset,'path':'../../arbitrary.png'}}]
    record['plan']=plan
    artifacts=service._deliver(record,output,plan,{'srt_path':str(srt)},{'passed':True},folder)
    paths={x['kind']:Path(x['path']) for x in artifacts}
    with Image.open(paths['cover']) as cover:
        assert cover.getpixel((80,160))[1:]==(255,255)  # actual source cyan, finished frame is red
        assert min(cover.getpixel((80,68)))>240  # interface top preserved below title
        assert cover.getpixel((80,263))[0]>240 and cover.getpixel((80,263))[1]>100  # bottom preserved
    provenance=json.loads(paths['record'].read_text(encoding='utf-8'))['cover_source']
    assert provenance['kind']=='frozen_brand_screenshot'
    assert provenance['image_sha256']==asset['sha256'] and provenance['title_layers']==1
    assert hashlib.sha256(image_path.read_bytes()).hexdigest()==asset['sha256']


@pytest.mark.parametrize('transfer',['smpte2084','arib-std-b67'])
def test_cover_maps_real_hdr_samples_before_final_canvas(tmp_path,transfer):
    inbox=tmp_path/'inbox';inbox.mkdir();source=inbox/'hdr.mkv'
    conversion=('zscale=pin=bt709:tin=bt709:min=bt709:rin=tv:'
                f'p=bt2020:t={transfer}:m=bt2020nc:r=tv:npl=100,format=yuv420p10le')
    ffmpeg('-f','lavfi','-i','testsrc2=s=160x90:r=10:d=1','-vf',conversion,
           '-c:v','ffv1','-pix_fmt','yuv420p10le','-color_primaries','bt2020',
           '-color_trc',transfer,'-colorspace','bt2020nc','-color_range','tv',source)
    service=DirectorService(tmp_path/'data',inbox,validate_source=source_validator)
    plan={'scenes':[{'source_index':0,'source_in':0,'source_out':.9}]}
    record={'plan':plan,'snapshot':{'sources':service._paths_snapshot([source.name])}}
    folder=tmp_path/'frames';folder.mkdir()
    cover,evidence=service._cover_frame(record,plan,{'width':160,'height':284},folder)
    assert evidence['color_workflow']['applied'] is True
    assert evidence['color_workflow']['input']['color_transfer']==transfer
    # Independent actual-frame comparison proves pixels were tone-mapped;
    # merely tagging the PNG or downsizing raw HDR values would fail.
    expected=tmp_path/'expected.png'
    ffmpeg('-ss',str(evidence['source_time']),'-i',source,'-frames:v','1',
           '-vf',media.HDR_TO_SDR_FILTER+',format=rgb24',expected)
    with Image.open(cover) as actual,Image.open(expected) as reference:
        assert actual.size==(160,284)
        assert actual.getbbox()==(0,97,160,187)
        assert actual.crop(actual.getbbox()).convert('RGB').tobytes()==reference.convert('RGB').tobytes()


def test_cover_refuses_modified_source_and_out_of_range_scene(tmp_path):
    service,record,plan,output,srt,folder=make_record(tmp_path)
    bad={**plan,'scenes':[{'source_index':1,'source_in':0,'source_out':10}]}
    with pytest.raises(DirectorServiceError,match='校验不一致'):
        service._cover_frame({**record,'plan':bad},bad,{'width':160,'height':284},folder)
    source=service.inbox/'selected.mp4';source.write_bytes(source.read_bytes()+b'changed')
    with pytest.raises(DirectorServiceError) as failure:
        service._cover_frame(record,plan,{'width':160,'height':284},folder)
    assert failure.value.code=='source_changed'
