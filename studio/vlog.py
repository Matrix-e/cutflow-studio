"""原声 Vlog production adapter.

This mode keeps the selected clips' real audio and uses the existing checked
editorial renderer. The plan is durable and explicit so a later ASR planner can
replace only the planning step without changing the queue or delivery contract.
"""
from __future__ import annotations

from pathlib import Path
from . import creative
from . import media as source_media
import shutil
from uuid import uuid4


def prepare_sources(paths, work_dir, settings):
    prepared = []
    for value in paths:
        path = Path(value)
        info = source_media.inspect_media(path)
        if info['color_transfer'] not in ('smpte2084', 'arib-std-b67'):
            prepared.append(str(path));continue
        folder = Path(work_dir)/'sdr';folder.mkdir(parents=True,exist_ok=True)
        if shutil.disk_usage(folder).free < max(path.stat().st_size*2, 1024**3):
            raise VlogError('GPU 数据盘空间不足以转换 HDR 素材')
        output = folder/(uuid4().hex+'.mp4')
        tone='zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=hable:desat=2,zscale=t=bt709:m=bt709:r=tv,format=yuv420p'
        codec=['-c:v','h264_nvenc','-preset','p4','-cq','20'] if (settings or {}).get('encoder')=='nvenc' else ['-c:v','libx264','-preset','fast','-crf','20']
        source_media._run([source_media._tool('ffmpeg'),'-v','error','-nostdin','-y','-filter_threads','2',
            *source_media.INPUT_OPTIONS,'-i',str(path),'-map','0:v:0','-map','0:a:0?', '-vf',tone,
            *codec,'-color_primaries','bt709','-color_trc','bt709','-colorspace','bt709',
            '-c:a','aac','-ar','48000','-movflags','+faststart',str(output)],max(300,info['duration']*6))
        checked=source_media.inspect_media(output)
        if checked['color_transfer']!='bt709' or abs(checked['duration']-info['duration'])>.15 or checked['has_audio']!=info['has_audio']:
            raise VlogError('HDR 转换后的时长、色彩或音轨检查未通过')
        prepared.append(str(output))
    return prepared


class VlogError(RuntimeError):
    pass


def build_vlog_plan(paths, brief, options=None, settings=None, *, work_dir=None,
                    stop_event=None, on_stage=None):
    if not paths:
        raise VlogError("原声 Vlog 至少需要一段视频素材")
    if stop_event is not None and stop_event.is_set():
        raise VlogError("任务已取消")
    requested = dict(options or {})
    opts = {key: value for key, value in requested.items() if key in creative.DEFAULT_CHOICES}
    opts['target_duration'] = None
    opts["keep_original_audio"] = True
    opts.setdefault("music", True)
    opts.setdefault("pace", "preserve")
    if on_stage:
        on_stage("planning_visuals")
    prepared = prepare_sources(paths, work_dir, settings)
    plan = creative.build_creation_plan(prepared, brief or "按原声内容剪成自然 Vlog", opts, settings or {})
    plan["vlog_mode"] = "native_audio_basic"
    plan["warnings"] = list(dict.fromkeys([*(plan.get("warnings") or []),
        "已保留所选素材原声；当前版本按所选顺序拼接，未进行语义选段。" ]))
    if requested.get('allow_supplemental_visuals') or requested.get('allow_generated_dialogue'):
        from .supplements import build
        plan['supplement'] = build(paths, brief, requested, work_dir, stop_event, on_stage)
        plan['warnings'].append('片尾增加生成衔接段；原片原声完整保留，新增旁白可在制作说明中查看。')
    return plan


def render_vlog_plan(plan, output_path, settings=None, *, on_stage=None):
    if plan.get("kind") != "ordered_composition" or plan.get('vlog_mode') != 'native_audio_basic':
        raise VlogError("原声 Vlog 方案无效")
    if on_stage:
        on_stage("compositing")
    supplement = plan.get('supplement')
    base_output = str(Path(supplement['work'])/'originals.mp4') if supplement else output_path
    report = creative.render_creation_plan(plan, base_output, settings)
    if supplement:
        from .supplements import append_video
        result = append_video(base_output, output_path, supplement, settings or {})
        from . import media as checks
        qc=checks._check_output(Path(output_path), {'duration':plan['duration']+supplement['duration'], 'settings':plan['settings'], 'source_info':{'has_audio':True}}, plan['settings']['width'], plan['settings']['height'])
        report.update(duration=result['duration'], metadata=result, qc=qc, output_path=str(output_path), supplemental_script=supplement['spoken_script'])
    if on_stage:
        on_stage("checking")
    report["warnings"] = list(dict.fromkeys([*(plan.get("warnings") or []), *(report.get("warnings") or [])]))
    return report
