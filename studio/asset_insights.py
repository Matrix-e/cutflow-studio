"""Cached, bounded source analysis. No filename-based content claims."""
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import threading

from .media import INPUT_OPTIONS, _tool, inspect_media
from .assembly_service import PIPELINE_LOCK


LEGACY_NOTICE = '原声自动摘要（未经校对，非事实认证）：'


def _clock(value):
    seconds = max(0, int(value))
    return f'{seconds//60}:{seconds%60:02}'


def _director_source_fingerprint(path):
    # Keep the source-version contract used by the completed analysis checkpoint.
    path = Path(path).resolve(); stat = path.stat()
    key = f'assembly-v1:{path}:{stat.st_size}:{stat.st_mtime_ns}:{stat.st_ctime_ns}'
    return hashlib.sha256(key.encode('utf-8')).hexdigest()


def read_director_excerpt(data, path):
    """Read only committed source evidence; never summarize plans as events.

    A completed analysis is sufficient even if the later plan/render is blocked.
    The checkpoint digest and current source fingerprint must both match.
    Raw excerpts remain explicitly unverified machine transcriptions.
    """
    root = Path(data).resolve(); database = root/'productions.sqlite3'
    if not database.is_file():return None
    try:
        current = _director_source_fingerprint(path)
        with closing(sqlite3.connect(database.as_uri()+'?mode=ro', uri=True, timeout=.2)) as db:
            rows = db.execute("SELECT id,checkpoints_json FROM productions WHERE checkpoints_json!='{}' ORDER BY updated_at DESC LIMIT 80").fetchall()
        for identifier, checkpoints in rows:
            if not isinstance(identifier,str) or not re.fullmatch(r'[a-fA-F0-9-]{36}',identifier):continue
            try:
                saved = json.loads(checkpoints)
                if not isinstance(saved,dict):continue
                checkpoint = saved.get('analysis')
                if not isinstance(checkpoint,dict) or not re.fullmatch(r'[a-f0-9]{64}',str(checkpoint.get('sha256',''))):continue
                evidence_file = root/'productions'/identifier/'analysis.json'
                if evidence_file.is_symlink() or not evidence_file.resolve().is_relative_to(root/'productions') or evidence_file.stat().st_size>8*1024*1024:continue
                raw = evidence_file.read_bytes()
                if hashlib.sha256(raw).hexdigest()!=checkpoint['sha256']:continue
                evidence = json.loads(raw)
                if not isinstance(evidence,dict) or not isinstance(evidence.get('sources'),list) or not isinstance(evidence.get('speech'),list):continue
                source = next((s for s in evidence['sources'] if isinstance(s,dict) and s.get('fingerprint')==current),None)
                if not source:continue
                coverage = float(source['coverage']); duration = float(source['duration'])
                if not (math.isfinite(coverage) and math.isfinite(duration) and 0<coverage<=duration+.01):continue
                segments = [s for s in evidence['speech'] if isinstance(s,dict) and s.get('source_index')==source['index']]
                if not all(isinstance(s.get('text'),str) and s['text'].strip()
                           and all(isinstance(s.get(k),(int,float)) and not isinstance(s[k],bool) and math.isfinite(s[k]) for k in ('start','end'))
                           and 0<=s['start']<s['end']<=coverage+.01 for s in segments):continue
                segments.sort(key=lambda s:s['start'])
                if any(a['end']>b['start']+.05 for a,b in zip(segments,segments[1:])):continue
                # Complete, chronological quoted utterances only. Do not silently
                # remove negation/intent or invent a synthesized event summary.
                candidates=[s for s in segments if len(s['text'])<=600]
                indexes=sorted(set([0,len(candidates)//2,len(candidates)-1])) if candidates else []
                selected=[{k:candidates[i][k] for k in ('start','end','text')} for i in indexes]
                if selected:
                    summary='原声摘录（机器识别，未经校对）：\n'+'\n'.join(
                        f"{_clock(s['start'])}–{_clock(s['end'])} 原声：“{s['text']}”" for s in selected)
                else:
                    summary='本次分析未找到适合直接摘录的清晰短句；请播放原片确认，不据此推断事件结果。'
                return {'summary':summary,'summary_kind':'director_speech_excerpt','speech_excerpts':selected,
                    'summary_basis':f'来自已保存并校验的素材分析；原声覆盖前 {coverage:.1f} 秒。摘录不代表事件或业务事实已获认证，未做人工纠错。',
                    'analysis_reference':{'production_id':identifier,'source_fingerprint':current,'checkpoint_sha256':checkpoint['sha256']},
                    'speech_coverage':coverage,'analyzed_ranges':source.get('sampled_ranges',[]),
                    'human_verified':False}
            except (OSError,ValueError,TypeError,KeyError,StopIteration):continue
    except (OSError,ValueError,TypeError,sqlite3.Error):pass
    return None


def legacy_summary_notice(result):
    result=dict(result)
    if result.get('status')=='ready' and result.get('has_audio') is not False:
        text=str(result.get('summary') or '')
        if text and not text.startswith(LEGACY_NOTICE):result['summary']=LEGACY_NOTICE+text
        result['summary_kind']='legacy_auto_summary';result['human_verified']=False
        basis=str(result.get('summary_basis') or '')
        notice='原声自动摘要不能证明计划已经发生或业务事实已确认。'
        result['summary_basis']=basis if notice in basis else basis+' '+notice
    return result


def fingerprint(path):
    s = path.stat()
    return hashlib.sha256(f'v1:{path}:{s.st_size}:{s.st_mtime_ns}'.encode()).hexdigest()


def frame_review(info, samples):
    short = min(info['display_width'], info['display_height'])
    resolution = 40 if short >= 1080 else 32 if short >= 720 else 20 if short >= 480 else 10
    usable = sum(1 for pixels in samples if 25 <= sum(pixels)/len(pixels) <= 230
                 and sum(p < 8 or p > 247 for p in pixels)/len(pixels) < .75)
    exposure = round(60 * usable / len(samples))
    score = resolution + exposure
    return {'score': score, 'label': '优先考虑' if score >= 85 else '可作补充' if score >= 60 else '建议复核',
            'reasons': [f"分辨率 {info['display_width']}×{info['display_height']}（{resolution}/40）",
                        f'3 张抽帧中 {usable} 张亮度处于初评范围（{exposure}/60）'],
            'scope': '画面技术初评：仅分辨率与抽帧曝光，不代表内容相关性、构图或整段稳定性。'}


def analyze_visuals(path, folder):
    info = inspect_media(path)
    frames, samples = [], []
    for i, fraction in enumerate((.15, .5, .85)):
        second = max(0, min(info['duration'] * fraction, info['duration'] - .05))
        output = folder / f'{i}.jpg'
        command = [_tool('ffmpeg'), '-v', 'error', '-nostdin', '-y', '-ss', str(second),
                   *INPUT_OPTIONS, '-i', str(path), '-frames:v', '1', '-vf', 'scale=320:-2', '-q:v', '4', str(output)]
        subprocess.run(command, capture_output=True, check=True, timeout=60)
        raw = subprocess.run([_tool('ffmpeg'), '-v', 'error', '-nostdin', '-i', str(output),
                              '-vf', 'scale=64:64,format=gray', '-f', 'rawvideo', '-'],
                             capture_output=True, check=True, timeout=15).stdout
        if len(raw) != 4096:
            raise ValueError('Incomplete frame')
        samples.append(raw)
        frames.append({'time': round(second, 1), 'image': 'data:image/jpeg;base64,' + base64.b64encode(output.read_bytes()).decode()})
    return {'frames': frames, 'duration': round(info['duration'], 1),
            'recommendation': frame_review(info, samples), 'has_audio': info['has_audio']}


class AssetInsights:
    def __init__(self, data):
        self.data = Path(data)
        self.root = Path(data) / 'asset-insights'
        self.root.mkdir(parents=True, exist_ok=True)
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='asset-insights')
        self.lock = threading.Lock()
        self.pending = set()

    def read(self, path, enqueue=False):
        key = fingerprint(path)
        folder = self.root / key
        result = {'status': 'pending', 'summary': '等待分析原声内容', 'frames': []}
        try:
            result = json.loads((folder/'result.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            pass
        excerpt=read_director_excerpt(self.data,path)
        if excerpt:
            result={**result,**excerpt}
            if result.get('frames'):result['status']='ready'
        else:
            result=legacy_summary_notice(result)
        with self.lock:
            if key in self.pending:
                result['status'] = 'analyzing'
            elif enqueue and result['status'] not in ('ready', 'partial') and len(self.pending) < 32:
                self.pending.add(key)
                self.pool.submit(self.run, path, key)
                result['status'] = 'analyzing'
        return result

    def save(self, folder, result):
        temp = folder/'result.tmp'
        temp.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
        temp.replace(folder/'result.json')

    def run(self, path, key):
        folder = self.root/key
        folder.mkdir(exist_ok=True)
        result = {'status': 'failed', 'frames': [], 'summary': '分析未完成，请点击重试。'}
        try:
            result = analyze_visuals(path, folder)
            result.update(status='analyzing', summary='画面已就绪，正在识别原声并总结…')
            self.save(folder, result)
            excerpt=read_director_excerpt(self.data,path)
            if excerpt:
                result.update(excerpt,status='ready')
            elif not result['has_audio']:
                result.update(status='ready', summary='这段素材没有音轨，可作为补充画面。请结合上方关键帧确认内容。', summary_basis='音轨检测；未进行视觉语义识别')
            else:
                python = os.environ.get('CUTFLOW_IDEA_PYTHON', '')
                model = os.environ.get('CUTFLOW_ASSET_WHISPER_MODEL', '/root/autodl-tmp/cutflow-studio/models/whisper-qa')
                if not Path(python).is_file() or not Path(model).is_dir():
                    raise RuntimeError('Local speech model unavailable')
                env = dict(os.environ, HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', TOKENIZERS_PARALLELISM='false')
                result.update(summary='画面已就绪，等待当前制作任务释放分析资源。')
                self.save(folder,result)
                with PIPELINE_LOCK:
                    # A production may have completed its analysis while this
                    # background card waited. Reuse that evidence first.
                    excerpt=read_director_excerpt(self.data,path)
                    if excerpt:result.update(excerpt,status='ready')
                    else:
                        run = subprocess.run([python, '-m', 'studio.asset_insights_model', str(path), str(folder), model],
                                             cwd=Path(__file__).resolve().parent.parent, env=env,
                                             capture_output=True, timeout=600)
                        if run.returncode:
                            raise RuntimeError('Speech analysis failed')
                        result.update(json.loads((folder/'speech.json').read_text(encoding='utf-8')))
                        result['status'] = 'ready'
            if fingerprint(path) != key:
                raise RuntimeError('Source changed')
        except Exception:
            result.update(status='partial' if result.get('frames') else 'failed',
                          summary='原声总结暂未完成；可查看画面，稍后点击重试。', summary_basis='未生成内容总结')
        finally:
            self.save(folder, result)
            with self.lock:
                self.pending.discard(key)

    def retry(self, path):
        key = fingerprint(path)
        with self.lock:
            if key not in self.pending:
                (self.root/key/'result.json').unlink(missing_ok=True)
        return self.read(path, enqueue=True)
