"""Atomic local video imports. A mode-bearing directory travels with the media."""
from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import subprocess
import uuid

from .media import INPUT_OPTIONS

MAX_BYTES = 2 * 1024**3
SPACE_RESERVE = 64 * 1024**2
VIDEO_EXTENSIONS = {'.mp4', '.mov', '.mkv', '.webm', '.m4v', '.avi', '.mts'}


class ImportProblem(ValueError):
    def __init__(self, detail: str, status: int = 400):
        super().__init__(detail)
        self.status = status


def validate_input(identifier: str, name: str, mode: str):
    try:
        if str(uuid.UUID(identifier)) != identifier.lower():
            raise ValueError()
    except (ValueError, AttributeError):
        raise ImportProblem('导入编号无效，请重新选择文件。') from None
    if mode not in {'gentle', 'preserve'}:
        raise ImportProblem('请选择口播精剪或保留完整。')
    if (not name or len(name) > 180 or name.startswith(('.', '~')) or name.endswith((' ', '.'))
            or any(ord(c) < 32 or c in '<>:"/\\|?*' for c in name)
            or re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])', name.split('.')[0], re.I)
            or Path(name).suffix.lower() not in VIDEO_EXTENSIONS):
        raise ImportProblem('文件名或格式不支持，请选择 MP4、MOV、MKV、WebM、M4V、AVI 或 MTS 视频。')
    return identifier.lower()


def probe_video(path: Path):
    binary = shutil.which('ffprobe')
    if not binary:
        raise ImportProblem('本机缺少视频检查组件，请使用项目启动器打开工作台。', 503)
    try:
        result = subprocess.run([binary, '-v', 'error', *INPUT_OPTIONS, '-show_format', '-show_streams',
                                 '-of', 'json', str(path)], capture_output=True, timeout=30,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0)
        data = json.loads(result.stdout) if result.returncode == 0 else {}
        videos = [s for s in data.get('streams', []) if s.get('codec_type') == 'video'
                  and not s.get('disposition', {}).get('attached_pic')]
        duration = 0
        for candidate in (videos[0].get('duration') if videos else None, data.get('format', {}).get('duration')):
            try:
                value = float(candidate)
            except (TypeError, ValueError):
                continue
            if math.isfinite(value) and value > 0:
                duration = value
                break
        if not videos or not math.isfinite(duration) or duration <= 0 or int(videos[0].get('width', 0)) <= 0 or int(videos[0].get('height', 0)) <= 0:
            raise ValueError()
    except (OSError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired):
        raise ImportProblem('视频检查未通过，文件可能未下载完整或不是有效视频。') from None


def _not_link(path: Path):
    if path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
        raise ImportProblem('素材目录包含链接，已停止导入。', 409)


def _digest(path: Path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while block := stream.read(1024**2):
            digest.update(block)
    return digest.hexdigest()


class LocalLibrary:
    def __init__(self, inbox: Path, data_dir: Path, *, target: dict | None = None, max_bytes: int = MAX_BYTES):
        _not_link(inbox)
        self.inbox, self.data = inbox.resolve(), data_dir.resolve()
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.data.mkdir(parents=True, exist_ok=True)
        self.db = self.data / 'local-imports.sqlite3'
        self.max_bytes = max_bytes
        target = target if target is not None else os.environ
        remote = PurePosixPath(target.get('CUTFLOW_SSH_REMOTE', '/root/autodl-tmp/cutflow-studio/inbox'))
        if not remote.is_absolute() or '..' in remote.parts:
            raise ImportProblem('同步目标配置无效。', 503)
        self.remote_root = remote
        identity = ':'.join([target.get('CUTFLOW_SSH_HOST', ''), target.get('CUTFLOW_SSH_PORT', '22'),
                             target.get('CUTFLOW_SSH_USER', 'root'), str(remote)])
        self.target_id = hashlib.sha256(identity.encode()).hexdigest()
        with self.connection() as con:
            con.execute('''CREATE TABLE IF NOT EXISTS imports (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, mode TEXT NOT NULL, source TEXT NOT NULL,
                created_at TEXT NOT NULL, size INTEGER, sha256 TEXT)''')

    @contextmanager
    def connection(self):
        with closing(sqlite3.connect(self.db, timeout=10)) as con, con:
            con.row_factory = sqlite3.Row
            yield con

    def _directory(self, mode, identifier):
        current = self.inbox
        _not_link(current)
        for part in ('工作台导入', mode, identifier):
            current = current / part
            _not_link(current)
            current.mkdir(exist_ok=True)
            _not_link(current)
            if not current.is_dir() or not current.resolve().is_relative_to(self.inbox):
                raise ImportProblem('导入目录不可用。', 409)
        return current

    def _lease(self, identifier):
        folder = self.data / '.import-locks'
        _not_link(folder)
        folder.mkdir(exist_ok=True)
        path = folder / (identifier + '.lock')
        _not_link(path)
        stream = path.open('a+b')
        try:
            if os.fstat(stream.fileno()).st_size == 0:
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return stream
        except OSError:
            stream.close()
            raise ImportProblem('这个文件正在导入，请等待完成后再查看。', 409) from None

    def begin(self, identifier, name, mode, content_length=None):
        identifier = validate_input(identifier, name, mode)
        if content_length is not None and (content_length <= 0 or content_length > self.max_bytes):
            raise ImportProblem('单个视频须大于 0 字节且不超过 2 GiB。', 413)
        lease = self._lease(identifier)
        try:
            directory = self._directory(mode, identifier)
            if shutil.disk_usage(directory).free < (content_length or 0) + SPACE_RESERVE:
                raise ImportProblem('素材盘可用空间不足，请释放空间后重新导入。', 507)
            source = (directory / name).relative_to(self.inbox).as_posix()
            with self.connection() as con:
                con.execute('BEGIN IMMEDIATE')
                row = con.execute('SELECT * FROM imports WHERE id=?', (identifier,)).fetchone()
                if row and (row['name'], row['mode'], row['source']) != (name, mode, source):
                    raise ImportProblem('这个导入编号已用于另一份文件或剪辑方式，请重新选择文件。', 409)
                if not row:
                    con.execute('INSERT INTO imports(id,name,mode,source,created_at) VALUES(?,?,?,?,?)',
                                (identifier, name, mode, source, datetime.now(timezone.utc).isoformat()))
            return Upload(self, identifier, name, mode, directory, lease, content_length)
        except BaseException:
            lease.close()
            raise

    def receipt(self, row):
        value = {key: row[key] for key in ('id', 'name', 'mode', 'size', 'sha256', 'source', 'created_at')}
        value.update(status='waiting_sync', remote_source=None)
        sync_db = self.data / 'sync.sqlite3'
        if sync_db.is_file():
            try:
                with closing(sqlite3.connect(sync_db.as_uri() + '?mode=ro', uri=True, timeout=1)) as con:
                    found = con.execute('SELECT destination FROM transfers_v2 WHERE relative_path=? AND sha256=? AND target_id=?',
                                        (row['source'], row['sha256'], self.target_id)).fetchone()
                if found:
                    relative = PurePosixPath(found[0]).relative_to(self.remote_root)
                    if relative.parts and '..' not in relative.parts:
                        value.update(status='synced', remote_source=relative.as_posix())
            except (sqlite3.Error, ValueError, OSError):
                pass
        return value

    def listing(self):
        with self.connection() as con:
            rows = con.execute('SELECT * FROM imports WHERE sha256 IS NOT NULL ORDER BY created_at DESC LIMIT 500').fetchall()
        return {'inbox': str(self.inbox), 'imports': [self.receipt(row) for row in rows], 'max_bytes': self.max_bytes}


class Upload:
    def __init__(self, library, identifier, name, mode, directory, lease, content_length):
        self.library, self.id, self.name, self.mode = library, identifier, name, mode
        self.directory, self.lease, self.expected = directory, lease, content_length
        self.temporary = directory / ('.' + uuid.uuid4().hex + '.part')
        self.stream = self.temporary.open('xb')
        self.sha = hashlib.sha256()
        self.size = 0

    def write(self, chunk):
        if self.size + len(chunk) > self.library.max_bytes:
            raise ImportProblem('单个视频不能超过 2 GiB。', 413)
        if shutil.disk_usage(self.directory).free < len(chunk) + SPACE_RESERVE:
            raise ImportProblem('素材盘可用空间不足，本次导入已停止。', 507)
        self.stream.write(chunk)
        self.sha.update(chunk)
        self.size += len(chunk)

    def finish(self):
        if self.size == 0 or self.expected is not None and self.size != self.expected:
            raise ImportProblem('视频没有完整接收，请重新导入。')
        self.stream.flush()
        os.fsync(self.stream.fileno())
        identity = os.fstat(self.stream.fileno())
        self.stream.close()
        probe_video(self.temporary)
        if self.library._directory(self.mode, self.id) != self.directory:
            raise ImportProblem('导入目录发生变化，已停止。', 409)
        _not_link(self.temporary)
        current = self.temporary.stat()
        if (current.st_ino, current.st_size, current.st_mtime_ns) != (identity.st_ino, identity.st_size, identity.st_mtime_ns):
            raise ImportProblem('导入文件发生变化，已停止。', 409)
        digest, destination = self.sha.hexdigest(), self.directory / self.name
        with self.library.connection() as con:
            con.execute('BEGIN IMMEDIATE')
            previous = con.execute('SELECT * FROM imports WHERE id=?', (self.id,)).fetchone()
            if previous['sha256'] is not None and (previous['sha256'], previous['size']) != (digest, self.size):
                raise ImportProblem('这个导入编号的文件内容不一致，已保留原文件。', 409)
            _not_link(destination)
            duplicate = destination.exists()
            if duplicate:
                if not destination.is_file() or destination.stat().st_size != self.size or _digest(destination) != digest:
                    raise ImportProblem('同名文件内容不一致，已保留原文件。', 409)
            else:
                # Hard-link creation is atomic and never overwrites an existing name.
                # A crash before the receipt commit is recovered by verifying that name.
                os.link(self.temporary, destination)
            con.execute('UPDATE imports SET size=?,sha256=? WHERE id=?', (self.size, digest, self.id))
            row = con.execute('SELECT * FROM imports WHERE id=?', (self.id,)).fetchone()
        return {'import': self.library.receipt(row), 'duplicate': duplicate}

    def close(self):
        # Cleanup cannot replace the meaningful validation/stream error. A
        # leftover .part is ignored by both sync and the GPU watcher.
        try:
            self.stream.close()
        except OSError:
            pass
        try:
            if self.temporary.parent.resolve().is_relative_to(self.library.inbox):
                self.temporary.unlink(missing_ok=True)
        except OSError:
            pass
        finally:
            self.lease.close()
