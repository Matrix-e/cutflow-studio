"""Stable-file, SHA-256 verified, append-only SFTP ingress for GPU workers."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
import sqlite3
import stat
import time
import uuid
from pathlib import Path, PurePosixPath

EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".mts", ".wav", ".mp3", ".m4a", ".srt"}
EXCLUDED = {"outputs", "renders", "data", "cache", ".git", "__pycache__"}


def candidate_files(root: Path):
    """Do not traverse links or hidden/cache/output directories."""
    root = root.resolve(strict=True)
    for folder, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if name not in EXCLUDED and not name.startswith(".") and not (Path(folder) / name).is_symlink() and not getattr(Path(folder) / name, "is_junction", lambda: False)()]
        for name in sorted(names):
            path = Path(folder) / name
            if name.startswith((".", "~")) or path.is_symlink() or path.suffix.lower() not in EXTENSIONS:
                continue
            if path.is_file() and path.resolve().is_relative_to(root):
                yield path


def stable_hash(path: Path) -> tuple[str, int]:
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise RuntimeError("文件仍在写入，等待下一轮同步。")
    return digest.hexdigest(), after.st_size


class SFTPTransport:
    def __init__(self, host: str, username: str, key_file: str, known_hosts: str, port: int = 22):
        if not host or not username or not key_file or not known_hosts:
            raise ValueError("请配置 SSH 主机、用户名、密钥路径与已核验的 known_hosts。")
        import paramiko
        self.client = paramiko.SSHClient()
        self.client.load_host_keys(str(Path(known_hosts).expanduser().resolve(strict=True)))
        self.client.set_missing_host_key_policy(paramiko.RejectPolicy())
        try:
            self.client.connect(hostname=host, port=port, username=username,
                                key_filename=str(Path(key_file).expanduser().resolve(strict=True)),
                                look_for_keys=False, allow_agent=False, timeout=15, banner_timeout=15, auth_timeout=15)
            self.sftp = self.client.open_sftp()
            self.sftp.get_channel().settimeout(60)
        except Exception:
            self.client.close()
            raise

    def close(self):
        try:
            self.sftp.close()
        finally:
            self.client.close()

    def _mkdirs(self, folder: str):
        current = "/"
        for part in PurePosixPath(folder).parts[1:]:
            current = posixpath.join(current, part)
            try:
                attrs = self.sftp.lstat(current)
                if not stat.S_ISDIR(attrs.st_mode):
                    raise RuntimeError("远端路径包含非目录或软链接。")
            except FileNotFoundError:
                self.sftp.mkdir(current)

    def _digest_remote(self, path: str) -> str:
        digest = hashlib.sha256()
        with self.sftp.open(path, "rb") as stream:
            while block := stream.read(1024 * 1024):
                digest.update(block)
        return digest.hexdigest()

    def upload(self, source: Path, destination: str, expected_hash: str, expected_size: int):
        self._mkdirs(posixpath.dirname(destination))
        try:
            attrs = self.sftp.lstat(destination)
            if not stat.S_ISREG(attrs.st_mode):
                raise RuntimeError("远端目标不是普通文件。")
            if attrs.st_size == expected_size and self._digest_remote(destination) == expected_hash:
                return
            raise RuntimeError("远端同名素材校验不一致，已停止以保护已有素材。")
        except FileNotFoundError:
            pass
        temporary = destination + "." + uuid.uuid4().hex + ".part"
        self.sftp.put(str(source), temporary, confirm=True)
        if self._digest_remote(temporary) != expected_hash:
            raise RuntimeError("传输校验未通过；临时文件未入库。")
        self.sftp.rename(temporary, destination)


class SyncAgent:
    def __init__(self, root: Path, remote_root: str, state: Path, *, settle_seconds: float = 15, clock=time.monotonic):
        self.root = root.resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("素材投递箱必须是目录。")
        remote = PurePosixPath(remote_root)
        if not remote.is_absolute() or ".." in remote.parts or "\\" in remote_root or any(ord(char) < 32 for char in remote_root):
            raise ValueError("GPU 目录必须为不包含 .. 的 Linux 绝对路径。")
        self.remote_root, self.settle, self.clock = str(remote), settle_seconds, clock
        self.observed: dict[str, tuple[int, int, float]] = {}
        self.hashed: dict[str, tuple[tuple[int, int], str, int]] = {}
        target_identity = ":".join([os.environ.get("CUTFLOW_SSH_HOST", ""), os.environ.get("CUTFLOW_SSH_PORT", "22"), os.environ.get("CUTFLOW_SSH_USER", "root"), self.remote_root])
        self.target_id = hashlib.sha256(target_identity.encode()).hexdigest()
        state.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(state)
        self.db.execute("CREATE TABLE IF NOT EXISTS transfers_v2 (relative_path TEXT, sha256 TEXT, target_id TEXT, bytes INTEGER, destination TEXT, synced_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(relative_path,sha256,target_id))")

    def close(self):
        self.db.close()

    def scan(self, transport=None, *, dry_run=False) -> list[dict]:
        results, seen = [], set()
        for path in candidate_files(self.root):
            relative = path.relative_to(self.root).as_posix()
            seen.add(relative)
            try:
                info, now = path.stat(), self.clock()
                signature = (info.st_size, info.st_mtime_ns)
                old = self.observed.get(relative)
                if old is None or old[:2] != signature:
                    self.observed[relative] = (*signature, now)
                    self.hashed.pop(relative, None)
                    continue
                if info.st_size == 0 or now - old[2] < self.settle:
                    continue
                cached = self.hashed.get(relative)
                if cached and cached[0] == signature:
                    digest, size = cached[1:]
                else:
                    digest, size = stable_hash(path)
                    self.hashed[relative] = (signature, digest, size)
                previous = self.db.execute("SELECT 1 FROM transfers_v2 WHERE relative_path=? AND sha256=? AND target_id=?", (relative, digest, self.target_id)).fetchone()
                if previous:
                    continue
                rel = PurePosixPath(relative)
                target = str(PurePosixPath(self.remote_root) / rel.parent / f"{rel.stem}--{digest[:16]}{rel.suffix.lower()}")
                if not dry_run:
                    if transport is None:
                        raise RuntimeError("未配置 SFTP 连接。")
                    transport.upload(path, target, digest, size)
                    # Detect producer changes after upload. A changing source is never marked synced.
                    if stable_hash(path) != (digest, size):
                        raise RuntimeError("素材上传期间发生变化，等待稳定后重新同步。")
                    self.db.execute("INSERT OR IGNORE INTO transfers_v2(relative_path,sha256,target_id,bytes,destination) VALUES(?,?,?,?,?)", (relative, digest, self.target_id, size, target))
                    self.db.commit()
                results.append({"path": relative, "sha256": digest, "bytes": size, "destination": target, "status": "dry_run" if dry_run else "synced"})
            except (OSError, RuntimeError) as exc:
                # Do not print credentials or transport exception details.
                results.append({"path": relative, "status": "failed", "message": "同步未完成；保留原文件，将在下一轮重试。", "error_type": type(exc).__name__})
        self.observed = {key: value for key, value in self.observed.items() if key in seen}
        self.hashed = {key: value for key, value in self.hashed.items() if key in seen}
        return results


def main(argv=None):
    parser = argparse.ArgumentParser(description="CutFlow 素材自动同步；不删除任何原文件。")
    project = Path(__file__).resolve().parents[1]
    selected_inbox = Path(r"C:\Users\Administrator\Desktop\空间同步\BaiduSyncdisk\公司材料空间\视频素材")
    parser.add_argument("--source", default=os.environ.get("CUTFLOW_INBOX", str(selected_inbox if selected_inbox.is_dir() else project / "素材投递箱")))
    parser.add_argument("--remote", default=os.environ.get("CUTFLOW_SSH_REMOTE", "/root/autodl-tmp/cutflow-studio/inbox"))
    parser.add_argument("--state", default=str(project / "data" / "sync.sqlite3"))
    parser.add_argument("--interval", type=float, default=5)
    parser.add_argument("--settle", type=float, default=15)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.interval < 1 or args.settle < 1:
        parser.error("interval 和 settle 至少为 1 秒。")
    agent = SyncAgent(Path(args.source), args.remote, Path(args.state), settle_seconds=args.settle)
    transport = None
    failures = 0
    try:
        # Prime stability observations, even for --once; never ingest a still-writing file.
        agent.scan(dry_run=True)
        time.sleep(args.settle)
        while True:
            try:
                if not args.dry_run and transport is None:
                    transport = SFTPTransport(os.environ.get("CUTFLOW_SSH_HOST", ""), os.environ.get("CUTFLOW_SSH_USER", "root"),
                                              os.environ.get("CUTFLOW_SSH_KEY", ""), os.environ.get("CUTFLOW_SSH_KNOWN_HOSTS", ""),
                                              int(os.environ.get("CUTFLOW_SSH_PORT", "22")))
                results = agent.scan(transport, dry_run=args.dry_run)
                failed = any(item["status"] == "failed" for item in results)
                print(json.dumps({"results": results}, ensure_ascii=False), flush=True)
                if failed:
                    failures += 1
                    if transport:
                        transport.close()
                        transport = None
                else:
                    failures = 0
                if args.once:
                    return 1 if failed else 0
            except Exception:
                failures += 1
                if transport:
                    try:
                        transport.close()
                    except Exception:
                        pass
                    transport = None
                print(json.dumps({"status": "connection_failed", "message": "连接不可用；请检查 SSH 配置、已核验的主机指纹与依赖。"}, ensure_ascii=False), flush=True)
                if args.once:
                    return 1
            time.sleep(min(args.interval * (2 ** min(failures, 5)), 120))
    except KeyboardInterrupt:
        return 0
    finally:
        if transport:
            try:
                transport.close()
            except Exception:
                pass
        agent.close()


if __name__ == "__main__":
    raise SystemExit(main())
