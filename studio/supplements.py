"""Offline generated end-card footage and bridging narration for original-audio Vlogs."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from uuid import uuid4
from . import media, narration

BASE = Path('/root/autodl-tmp/cutflow-studio')

def configuration_status():
    idea = bool(os.environ.get('CUTFLOW_IDEA_PYTHON') and os.environ.get('CUTFLOW_IDEA_MODEL_DIR'))
    image = (BASE/'presenter-runtime/bin/python').is_file() and (BASE/'models/sdxl/unet/diffusion_pytorch_model.fp16.safetensors').is_file()
    return {'visuals': idea and image, 'dialogue': idea and narration.configuration_status()['configured'],
            'placement': '片尾衔接段', 'visual_kind': '生成图片加轻微运镜'}

def run_model(mode, payload, work, stop_event=None):
    executable = str(BASE/'presenter-runtime/bin/python') if mode=='image' else os.environ['CUTFLOW_IDEA_PYTHON']
    request = work/(mode+'-request.json')
    request.write_text(json.dumps(payload,ensure_ascii=False),encoding='utf-8')
    env = dict(os.environ, HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', TOKENIZERS_PARALLELISM='false')
    with (work/(mode+'.log')).open('wb') as log:
        process = subprocess.Popen([executable,'-m','studio.supplement_model',mode,str(request)],
            cwd=Path(__file__).resolve().parent.parent,env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=log)
        try:
            deadline=time.monotonic()+480
            while process.poll() is None:
                if stop_event is not None and stop_event.wait(.25):
                    raise RuntimeError('补充生成已取消')
                if stop_event is None:time.sleep(.25)
                if time.monotonic()>deadline:raise RuntimeError('补充生成超时，请稍后重试')
            if process.returncode:raise RuntimeError('GPU 补充生成失败，请查看任务日志后重试')
        finally:
            if process.poll() is None:process.kill();process.wait()

def build(paths, brief, options, work_dir, stop_event=None, on_stage=None):
    visual=bool(options.get('allow_supplemental_visuals'));dialogue=bool(options.get('allow_generated_dialogue'))
    caps=configuration_status()
    if (visual and not caps['visuals']) or (dialogue and not caps['dialogue']):
        raise RuntimeError('所选补充生成模型尚未配置完整')
    work=Path(work_dir)/('supplement-'+uuid4().hex);work.mkdir(parents=True)
    evidence=[]
    from .asset_insights import fingerprint
    data=Path(os.environ.get('CUTFLOW_DATA',str(BASE/'data')))
    for path in paths:
        try:
            result=json.loads((data/'asset-insights'/fingerprint(Path(path))/'result.json').read_text(encoding='utf-8'))
            if result.get('status')=='ready' and result.get('transcript'):
                evidence.append(result['transcript'][:1800])
        except (OSError,ValueError):pass
    if on_stage:on_stage('planning_supplement')
    run_model('plan',{'brief':brief,'evidence':evidence,'dialogue':dialogue,'output':str(work/'plan.json')},work,stop_event)
    proposal=json.loads((work/'plan.json').read_text(encoding='utf-8'))
    text=proposal['spoken_script'] if dialogue else ''
    if dialogue and (not text.strip() or len(text)>160):raise RuntimeError('补充台词长度不合适，请修改想法后重试')
    image=work/'visual.png'
    if visual:
        if on_stage:on_stage('generating_visuals')
        run_model('image',{'prompt':proposal['image_prompt'],'output':str(image)},work,stop_event)
    else:
        info=media.inspect_media(paths[-1])
        media._run([media._tool('ffmpeg'),'-v','error','-nostdin','-y','-ss',str(max(0,info['duration']-.2)),
                    *media.INPUT_OPTIONS,'-i',str(paths[-1]),'-frames:v','1',str(image)],60)
    audio=None;duration=5.0
    if dialogue:
        if on_stage:on_stage('preparing_voice')
        audio=narration.synthesize(text,'serena',work/'voice.wav',style='warm',stop_event=stop_event)
        duration=audio['duration']+.35
        if duration>35:raise RuntimeError('补充旁白过长，请缩短想法后重试')
    return {'work':str(work),'image':str(image),'image_sha256':hashlib.sha256(image.read_bytes()).hexdigest(),
            'audio':audio,'duration':duration,'spoken_script':text,'image_prompt':proposal['image_prompt'],
            'generated_visual':visual,'placement':'after_originals','basis':'cached_speech' if evidence else 'user_brief_only'}

def append_video(original, destination, supplement, settings):
    info=media.inspect_media(original);w,h=info['display_width'],info['display_height'];fps=info['fps']
    image=Path(supplement['image'])
    if hashlib.sha256(image.read_bytes()).hexdigest()!=supplement['image_sha256']:raise RuntimeError('补充画面已变化')
    tail=Path(supplement['work'])/'tail.mp4';duration=supplement['duration']
    vf=f'scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},zoompan=z=min(zoom+0.0002\\,1.04):d=1:s={w}x{h}:fps={fps},format=yuv420p,setsar=1'
    cmd=[media._tool('ffmpeg'),'-v','error','-nostdin','-y','-loop','1','-framerate',str(fps),'-i',str(image)]
    if supplement['audio']:
        audio=Path(supplement['audio']['output_path'])
        if narration._sha(audio)!=supplement['audio']['sha256']:raise RuntimeError('补充音频已变化')
        cmd+=['-i',str(audio)]
    else:cmd+=['-f','lavfi','-i','anullsrc=r=48000:cl=stereo']
    encoder=['-c:v','h264_nvenc','-preset','p4','-cq','20'] if settings.get('encoder')=='nvenc' else ['-c:v','libx264','-preset','fast','-crf','20']
    media._run(cmd+['-t',str(duration),'-vf',vf,'-af','apad','-ar','48000','-ac','2',*encoder,'-c:a','aac','-movflags','+faststart',str(tail)],300)
    media._run([media._tool('ffmpeg'),'-v','error','-nostdin','-y','-i',str(original),'-i',str(tail),
        '-filter_complex','[0:v]setpts=PTS-STARTPTS[v0];[0:a]asetpts=PTS-STARTPTS[a0];[1:v]setpts=PTS-STARTPTS[v1];[1:a]asetpts=PTS-STARTPTS[a1];[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]',
        '-map','[v]','-map','[a]',*encoder,'-c:a','aac','-ar','48000','-movflags','+faststart',str(destination)],600)
    media._run([media._tool('ffmpeg'),'-v','error','-xerror','-i',str(destination),'-f','null','-'],600)
    result=media.inspect_media(destination)
    if not result['has_audio'] or abs(result['duration']-info['duration']-duration)>.3:raise RuntimeError('补充成片时长或声音检查失败')
    return result
