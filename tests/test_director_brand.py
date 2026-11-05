import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

from PIL import Image,ImageDraw
import pytest

from studio import director_brand as brand,media
from studio.director_media import DirectorMediaError,digest


def asset(tmp_path):
    p=tmp_path/'screen.png'
    image=Image.new('RGB',(180,360),'white');draw=ImageDraw.Draw(image)
    draw.rectangle((0,0,179,34),fill='blue');draw.rectangle((10,270,170,340),fill='green')
    image.save(p)
    return {'id':'a'*32,'name':'托管问答','kind':'miniapp_screenshot','path':str(p),
            'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'width':180,'height':360,
            'fact_ids':['hosting-entry'],'source_reference':'pagesC/carpark/smartHosting.vue',
            'page_route':'pagesC/carpark/smartHosting','purpose':'打开托管问答入口'}


def test_freeze_retains_provenance_and_rejects_changed_original(tmp_path,monkeypatch):
    a=asset(tmp_path);data=tmp_path/'data';work=data/'productions'/'one';work.mkdir(parents=True)
    monkeypatch.setattr('studio.brand_media.production_assets',lambda data:[a])
    monkeypatch.setattr('studio.brand_media.resolve_screenshot',lambda *args,**kwargs:Path(a['path']))
    context={'revision':3,'confirmed_facts':[{'id':'hosting-entry','text':'提供托管问答入口','status':'confirmed'}]}
    frozen=brand.snapshot_brand_assets(data,context,work,{'brand_usage':'required'})
    row=frozen['brand_assets'][0]
    assert row['source_reference']==a['source_reference'] and row['fact_ids']==['hosting-entry']
    assert Path(row['path']).parent==work/'brand-assets'
    assert 'brand_assets' not in context
    Path(row['path']).write_bytes(b'changed')
    with pytest.raises(DirectorMediaError,match='变化'):brand.validate_frozen_assets(frozen,work)


def test_required_brand_cannot_silently_use_unrelated_fact(tmp_path,monkeypatch):
    a=asset(tmp_path);monkeypatch.setattr('studio.brand_media.production_assets',lambda data:[a])
    with pytest.raises(DirectorMediaError,match='没有已确认事实'):
        brand.snapshot_brand_assets(tmp_path,{'confirmed_facts':[{'id':'unrelated','text':'其他'}]},tmp_path/'job',{'brand_usage':'required'})
    off=brand.snapshot_brand_assets(tmp_path,{},tmp_path/'job',{'brand_usage':'off'})
    assert off['brand_assets']==[] and not (tmp_path/'job').exists()


def test_requested_screenshots_are_never_silently_dropped(tmp_path,monkeypatch):
    a=asset(tmp_path);monkeypatch.setattr('studio.brand_media.production_assets',lambda data:[a])
    with pytest.raises(DirectorMediaError,match='没有与本次已确认知识关联'):
        brand.snapshot_brand_assets(tmp_path,{'confirmed_facts':[]},tmp_path/'job',
            {'brand_screenshot_ids':[a['id']]})
    with pytest.raises(DirectorMediaError,match='最多指定四张'):
        brand.snapshot_brand_assets(tmp_path,{},tmp_path/'job',{'brand_screenshot_ids':[str(i)*32 for i in range(5)]})


def test_actual_image_evidence_binds_fact_and_keeps_source_video_catalog(tmp_path):
    a=asset(tmp_path);e={'sources':[{'index':0}], 'speech':[], 'visuals':[],'sha256':'old'}
    calls=[]
    def provider(messages,tokens):
        calls.append(messages)
        return json.dumps({'usable':True,'description':'托管问答入口','visible_text':'托管申请','objects':['入口按钮']})
    out=brand.enrich_evidence(e,{'brand_assets':[a],'brand_usage':'required'},data=tmp_path,work_dir=tmp_path,provider=provider)
    v=out['visuals'][0]
    assert v['id']=='B0-0' and v['kind']=='brand_screenshot' and v['source_index']==1
    assert v['fact_ids']==['hosting-entry'] and out['sources']==e['sources']
    assert out['sha256']==digest({k:v for k,v in out.items() if k!='sha256'})
    assert 'data:image/jpeg;base64,' in calls[0][0]['content'][1]['image_url']['url']
    assert e['visuals']==[] and e['sha256']=='old'
    again=brand.enrich_evidence(e,{'brand_assets':[a]},data=tmp_path,work_dir=tmp_path,provider=provider)
    assert len(calls)==1 and again['visuals']==out['visuals']


def test_required_unusable_real_image_is_blocked(tmp_path):
    a=asset(tmp_path)
    def provider(messages,tokens):return json.dumps({'usable':False,'description':'错误页','visible_text':'错误','objects':[]})
    with pytest.raises(DirectorMediaError,match='没有通过实际画面'):
        brand.enrich_evidence({'sources':[],'speech':[],'visuals':[]},
            {'brand_assets':[a],'brand_usage':'required'},data=tmp_path,work_dir=tmp_path,provider=provider)


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'),reason='Real FFmpeg required')
def test_real_mixed_render_keeps_entire_screenshot_and_exact_frames(tmp_path):
    a=asset(tmp_path);source=tmp_path/'source.mp4'
    subprocess.run([shutil.which('ffmpeg'),'-v','error','-y','-f','lavfi','-i','testsrc2=s=180x320:r=10:d=1.2',
                    '-c:v','libx264','-pix_fmt','yuv420p',str(source)],check=True,capture_output=True,timeout=45)
    shots=[{'source_index':0,'source_in':0.,'source_out':1.,'frames':10,'timeline_start':0.,'timeline_end':1.},
           {'kind':'brand_screenshot','source_index':1,'source_in':0.,'source_out':2.,'frames':20,
            'timeline_start':1.,'timeline_end':3.,'brand_asset':a,'brand_media_id':a['id']}]
    master,cfg=brand.prepare_brand_visuals([source],[{'info':media.inspect_media(source)}],shots,tmp_path/'render',
        {'aspect':'9:16'},{'encoder':'cpu','width':180,'height':320,'fps':10,'crf':18})
    assert brand.assembly._count_frames(master)==30
    assert abs(media.inspect_media(master)['duration']-3)<.05
    assert shots[1]['planned_static'] and shots[1]['brand_image_sha256']==a['sha256']
    rect=shots[1]['brand_image_rect'];assert rect['fit']=='contain' and rect['crop'] is False
    frame=tmp_path/'frame.png'
    subprocess.run([shutil.which('ffmpeg'),'-v','error','-y','-ss','2','-i',str(master),'-frames:v','1',str(frame)],
                   check=True,capture_output=True,timeout=45)
    with Image.open(frame) as im:
        # Real top and bottom UI colors survive the encode, proving fit not crop.
        top=im.getpixel((rect['x']+rect['width']//2,rect['y']+int(rect['height']*.04)))
        bottom=im.getpixel((rect['x']+rect['width']//2,rect['y']+int(rect['height']*.84)))
    assert top[2]>130 and top[2]>top[0]*2
    assert bottom[1]>60 and bottom[1]>bottom[0]*2
    assert hashlib.sha256(Path(a['path']).read_bytes()).hexdigest()==a['sha256']
