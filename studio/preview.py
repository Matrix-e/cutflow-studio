"""On-demand browser-compatible previews; delivery MP4 files are read-only."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
import uuid

MAX_SOURCE_BYTES = 2 * 1024**3
MAX_PREVIEW_BYTES = 512 * 1024**2
MAX_CACHE_BYTES = 2 * 1024**3
DISK_RESERVE_BYTES = 64 * 1024**2
ENCODE_TIMEOUT = 300


class PreviewError(RuntimeError):
    pass


def _signature(source: Path) -> str:
    stat = source.stat()
    return hashlib.sha256(json.dumps([str(source), stat.st_dev, stat.st_ino, stat.st_size,
                                      stat.st_mtime_ns, stat.st_ctime_ns]).encode()).hexdigest()


def _probe(path: Path) -> dict:
    tool = shutil.which('ffprobe')
    if not tool:
        raise PreviewError('缺少视频检查组件，暂时无法生成兼容预览。')
    try:
        response = subprocess.run([tool, '-v', 'error', '-protocol_whitelist', 'file',
                                   '-format_whitelist', 'mov,matroska,webm', '-show_streams', '-show_format',
                                   '-of', 'json', str(path)], capture_output=True, timeout=15,
                                  creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0)
        if response.returncode:
            raise ValueError()
        info = json.loads(response.stdout)
        video = next(s for s in info['streams'] if s.get('codec_type') == 'video')
        duration = float(info['format']['duration'])
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError()
        return {'duration': duration, 'video': video,
                'audio': next((s for s in info['streams'] if s.get('codec_type') == 'audio'), None)}
    except (OSError, ValueError, KeyError, StopIteration, subprocess.TimeoutExpired):
        raise PreviewError('视频检查未通过，兼容预览未生成。') from None


def render_preview(source: Path, temporary: Path, stop_event: threading.Event):
    tool = shutil.which('ffmpeg')
    if not tool:
        raise PreviewError('缺少视频编码组件，暂时无法生成兼容预览。')
    if source.stat().st_size > MAX_SOURCE_BYTES:
        raise PreviewError('成片超过 2 GiB，本版不生成兼容预览；原成片仍可下载。')
    before = _probe(source)
    command = [tool, '-hide_banner', '-v', 'error', '-nostdin', '-y',
               '-protocol_whitelist', 'file', '-format_whitelist', 'mov,matroska,webm', '-i', str(source),
               '-map', '0:v:0', '-map', '0:a:0?', '-sn', '-dn',
               '-vf', "scale=w='min(1280,iw)':h='min(720,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1",
               '-r', '30', '-c:v', 'libvpx-vp9', '-deadline', 'realtime', '-cpu-used', '6',
               '-row-mt', '1', '-threads', '4', '-b:v', '0', '-crf', '34', '-pix_fmt', 'yuv420p',
               '-c:a', 'libopus', '-b:a', '96k', '-ar', '48000', '-ac', '2',
               '-fs', str(MAX_PREVIEW_BYTES), '-f', 'webm', str(temporary)]
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0)
    deadline = time.monotonic() + ENCODE_TIMEOUT
    try:
        while process.poll() is None:
            if stop_event.is_set():
                raise PreviewError('服务已停止，本次兼容预览未完成。')
            if time.monotonic() >= deadline:
                raise PreviewError('兼容预览超过 5 分钟处理时限，原成片仍可下载。')
            if shutil.disk_usage(temporary.parent).free < DISK_RESERVE_BYTES:
                raise PreviewError('磁盘空间不足，已停止生成兼容预览。')
            try:
                process.wait(timeout=0.25)
            except subprocess.TimeoutExpired:
                pass
        if process.returncode:
            raise PreviewError('兼容预览编码未完成，请下载原成片查看。')
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
    if not temporary.is_file() or not 0 < temporary.stat().st_size < MAX_PREVIEW_BYTES:
        raise PreviewError('兼容预览超过 512 MiB 或未完整生成，原成片仍可下载。')
    after = _probe(temporary)
    if (after['video'].get('codec_name') != 'vp9'
            or int(after['video'].get('width', 0)) > 1280 or int(after['video'].get('height', 0)) > 720
            or abs(after['duration'] - before['duration']) > 0.15
            or before['audio'] is not None and (after['audio'] or {}).get('codec_name') != 'opus'):
        raise PreviewError('兼容预览完整性检查未通过，原成片仍可下载。')


class PreviewManager:
    def __init__(self, data: Path, *, renderer=None):
        self.data = Path(data).resolve()
        self.renders = self.data / 'renders'
        self.cache = self.data / 'previews'
        self.cache.mkdir(parents=True, exist_ok=True)
        self.renderer = renderer or render_preview
        self.lock = threading.Lock()
        self.pending = queue.Queue(maxsize=8)
        self.states = {}
        self.thread = None
        self.stop_event = threading.Event()

    def _identity(self, job_id: str, source: Path):
        source = Path(source).resolve()
        if not source.is_relative_to(self.renders) or not source.is_file():
            raise PreviewError('该任务没有可播放的成片。')
        signature = _signature(source)
        key = hashlib.sha256((job_id + ':' + signature).encode()).hexdigest()
        return source, signature, key, self.cache / (key + '.webm')

    @staticmethod
    def _response(job_id, state):
        result = {'status': state['status']}
        if state.get('error'):
            result['error'] = state['error']
        if state['status'] == 'ready':
            result['video_url'] = f'/api/jobs/{job_id}/video?preview=webm'
        return result

    def status(self, job_id: str, source: Path):
        _, _, key, target = self._identity(job_id, source)
        with self.lock:
            if target.is_file() and not target.is_symlink() and target.stat().st_size > 0:
                return self._response(job_id, {'status': 'ready'})
            state = self.states.get(key, {'status': 'absent'})
            if state['status'] == 'ready':
                state = {'status': 'absent'}
            return self._response(job_id, state)

    def request(self, job_id: str, source: Path):
        source, signature, key, target = self._identity(job_id, source)
        with self.lock:
            if target.is_file() and not target.is_symlink() and target.stat().st_size > 0:
                return self._response(job_id, {'status': 'ready'})
            previous = self.states.get(key)
            if previous and previous['status'] != 'ready':
                return self._response(job_id, previous)
            if self.stop_event.is_set():
                return {'status': 'failed', 'error': '服务正在停止，请重新连接后查看。'}
            if self.pending.full():
                return {'status': 'failed', 'error': '兼容预览队列已满，请稍后手动重试。'}
            self.states[key] = {'status': 'queued'}
            self.pending.put_nowait((source, signature, key, target))
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._run, name='cutflow-preview', daemon=True)
                self.thread.start()
            return {'status': 'queued'}

    def video_path(self, job_id: str, source: Path) -> Path | None:
        _, _, _, target = self._identity(job_id, source)
        if target.is_file() and not target.is_symlink() and target.stat().st_size > 0:
            return target
        return None

    def _run(self):
        while not self.stop_event.is_set():
            try:
                source, signature, key, target = self.pending.get(timeout=0.25)
            except queue.Empty:
                continue
            temporary = self.cache / ('.' + key + '.' + uuid.uuid4().hex + '.part.webm')
            try:
                with self.lock:
                    self.states[key] = {'status': 'rendering'}
                # Do not evict media a user may currently be playing. Refuse more
                # cache work at the limit without affecting official delivery.
                used = sum(path.stat().st_size for path in self.cache.glob('*.webm') if path.is_file())
                if used + MAX_PREVIEW_BYTES > MAX_CACHE_BYTES:
                    raise PreviewError('兼容预览缓存已达到空间限制，原成片仍可下载。')
                if shutil.disk_usage(self.cache).free < MAX_PREVIEW_BYTES + DISK_RESERVE_BYTES:
                    raise PreviewError('磁盘可用空间不足，暂时无法生成兼容预览。')
                if _signature(source) != signature:
                    raise PreviewError('原成片已更新，请重新打开任务生成新预览。')
                self.renderer(source, temporary, self.stop_event)
                if self.stop_event.is_set():
                    raise PreviewError('服务已停止，本次兼容预览未完成。')
                if _signature(source) != signature:
                    raise PreviewError('原成片已更新，本次预览不会用于新版本。')
                if not temporary.is_file() or not 0 < temporary.stat().st_size < MAX_PREVIEW_BYTES:
                    raise PreviewError('兼容预览文件未完整生成。')
                os.replace(temporary, target)
                with self.lock:
                    self.states[key] = {'status': 'ready'}
            except Exception as error:
                message = str(error) if isinstance(error, PreviewError) else '兼容预览生成失败，原成片仍可下载。'
                with self.lock:
                    self.states[key] = {'status': 'failed', 'error': message}
            finally:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    # A locked temporary file must not kill the sole worker or
                    # strand later requests. It is never served as ready media.
                    pass
                finally:
                    self.pending.task_done()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)
