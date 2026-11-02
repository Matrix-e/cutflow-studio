"""Create a reviewed source bundle; explicitly upload it with strict SSH trust.

This script does not log in by password, alter SSH trust, install packages, or
start any process on the GPU. Default invocation is an offline dry run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import zipfile
from pathlib import Path, PurePosixPath

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from studio.sync import SFTPTransport, stable_hash

ROOT_FILES = {"README.md", "requirements.txt", "requirements-browser.txt", "pyproject.toml", ".env.example", "config/preset.json"}
SOURCE_RULES = {
    "studio": {".py"}, "tests": {".py"}, "scripts": {".py", ".sh", ".ps1", ".cmd"},
    "web": {".html", ".css", ".js", ".svg", ".png", ".jpg", ".webp", ".woff2"},
    "docs": {".md"},
}


def source_files(root: Path):
    """Allowlist source; never package user media, data, env secrets, or keys."""
    for name in sorted(ROOT_FILES):
        path = root / name
        if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root):
            yield path
    for directory, suffixes in SOURCE_RULES.items():
        base = root / directory
        if not base.is_dir() or base.is_symlink() or getattr(base, "is_junction", lambda: False)() or not base.resolve().is_relative_to(root):
            continue
        for folder, dirs, names in os.walk(base, followlinks=False):
            dirs[:] = [name for name in dirs if not name.startswith(".") and name != "__pycache__"
                       and not (Path(folder) / name).is_symlink()
                       and not getattr(Path(folder) / name, "is_junction", lambda: False)()]
            for name in sorted(names):
                path = Path(folder) / name
                if path.suffix.lower() in suffixes and not path.is_symlink() and not name.startswith(".") and path.resolve().is_relative_to(root):
                    yield path


def build_bundle(root: Path, output_dir: Path) -> tuple[Path, list[dict]]:
    root = root.resolve(strict=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    snapshot = []
    for path in sorted(source_files(root)):
        before = path.stat()
        content = path.read_bytes()
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise RuntimeError("源码仍在编辑，请稍后重新打包。")
        snapshot.append((path.relative_to(root).as_posix(), content))
    if not any(name == "studio/server.py" for name, _ in snapshot):
        raise RuntimeError("没有找到完整项目源码。")
    manifest = [{"path": name, "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)} for name, content in snapshot]
    identity = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    archive = output_dir / f"cutflow-source-{identity[:16]}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        # Fixed timestamps make identical source snapshots produce identical ZIPs.
        for name, content in snapshot + [("source-manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))]:
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            bundle.writestr(info, content)
    return archive, manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description="GPU 源码部署包：默认只离线打包；--upload 才会连接。")
    parser.add_argument("--host", default=os.environ.get("CUTFLOW_SSH_HOST", "connect.westb.seetacloud.com"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("CUTFLOW_SSH_PORT", "10893")))
    parser.add_argument("--user", default=os.environ.get("CUTFLOW_SSH_USER", "root"))
    parser.add_argument("--key", default=os.environ.get("CUTFLOW_SSH_KEY", ""), help="本机私钥路径；不接受私钥内容")
    parser.add_argument("--known-hosts", default=os.environ.get("CUTFLOW_SSH_KNOWN_HOSTS", ""))
    parser.add_argument("--remote-root", default="/root/autodl-tmp/cutflow-studio/releases")
    parser.add_argument("--output-dir", default=str(PROJECT / "data" / "deploy"))
    parser.add_argument("--upload", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.upload and args.dry_run:
        parser.error("--upload 与 --dry-run 不能同时使用。")
    remote_root = PurePosixPath(args.remote_root)
    if not remote_root.is_absolute() or ".." in remote_root.parts or "\\" in args.remote_root or any(ord(ch) < 32 for ch in args.remote_root):
        parser.error("远端目录必须为安全的 Linux 绝对路径。")
    if not 1 <= args.port <= 65535:
        parser.error("SSH 端口无效。")
    transport = None
    try:
        archive, manifest = build_bundle(PROJECT, Path(args.output_dir))
        digest, size = stable_hash(archive)
        target = str(remote_root / digest[:16] / archive.name)
        print(json.dumps({"status": "packaged", "archive": str(archive), "files": len(manifest), "bytes": size, "sha256": digest,
                          "excluded": ["user media", "runtime data", "secrets", "SSH keys"], "remote_path": target}, ensure_ascii=False), flush=True)
        if not args.upload:
            print("离线检查完成；未连接 GPU。审核部署包后可加 --upload 上传。", flush=True)
            return 0
        if not args.key or not args.known_hosts:
            raise ValueError("上传需要私钥路径和已核验的 known_hosts 路径。")
        transport = SFTPTransport(args.host, args.user, args.key, args.known_hosts, args.port)
        transport.upload(archive, target, digest, size)
        print(json.dumps({"status": "uploaded_verified", "remote_path": target, "sha256": digest,
                          "next_step": "在 GPU 解压至新的发布目录，运行 bash scripts/remote-install.sh；本脚本未远程执行任何代码。"}, ensure_ascii=False), flush=True)
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__,
                          "message": "未完成部署；请检查源码包、SSH 连通性、密钥及已核验的主机指纹。凭证未写入部署包。"}, ensure_ascii=False), flush=True)
        return 1
    finally:
        if transport:
            try:
                transport.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
