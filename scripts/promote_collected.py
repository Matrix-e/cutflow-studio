"""Explicit rights-confirmed adoption of verified local collection downloads."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.start_local import environment

MAX_PROVENANCE = 65536


def _is_link(path: Path) -> bool:
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


def _root(path: Path, *, create=False) -> Path:
    absolute = path.absolute()
    for part in [absolute, *absolute.parents]:
        if _is_link(part):
            raise ValueError("素材目录不允许软链接或目录联接。")
    if create:
        absolute.mkdir(parents=True, exist_ok=True)
    root = absolute.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("素材目录不是文件夹。")
    return root


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _open_regular(path: Path, root: Path):
    if _is_link(path) or not path.resolve(strict=True).is_relative_to(root):
        raise ValueError("素材路径不允许链接或越过采集目录。")
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("素材必须是普通文件。")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    try:
        if _identity(os.fstat(descriptor)) != _identity(before):
            raise ValueError("素材正在发生变化，请稍后重试。")
        return os.fdopen(descriptor, "rb"), before
    except Exception:
        os.close(descriptor)
        raise


def _unchanged(path: Path, before):
    if _is_link(path) or _identity(path.lstat()) != _identity(before):
        raise ValueError("素材或来源记录在采用过程中发生变化。")


def _digest(path: Path, root: Path) -> str:
    stream, before = _open_regular(path, root)
    digest = hashlib.sha256()
    with stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
        if _identity(os.fstat(stream.fileno())) != _identity(before):
            raise ValueError("素材正在变化。")
    _unchanged(path, before)
    return digest.hexdigest()


def _read_json(path: Path, root: Path):
    stream, before = _open_regular(path, root)
    with stream:
        raw = stream.read(MAX_PROVENANCE + 1)
    if len(raw) > MAX_PROVENANCE:
        raise ValueError("来源记录过大。")
    _unchanged(path, before)
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("来源记录格式无效。")
    return value, raw, before


def _matching_sidecar(value: dict, provenance: dict, rights: str, note: str) -> bool:
    adoption = value.get("adoption", {})
    return (value.get("source_provenance") == provenance and adoption.get("rights_status") == rights
            and adoption.get("rights_note") == note and adoption.get("verified_sha256") == provenance["sha256"])


def adopt(collected_dir: Path, inbox_dir: Path, item_id: str, rights_status: str, rights_note: str, *, probe=None) -> dict:
    if not isinstance(item_id, str) or not re.fullmatch(r"[0-9]{1,32}", item_id):
        raise ValueError("视频 ID 必须只包含数字，不能填写路径。")
    if rights_status not in {"owned", "licensed"}:
        raise ValueError("请明确选择本人拥有版权或已取得适用授权。")
    note = rights_note.strip() if isinstance(rights_note, str) else ""
    if not 5 <= len(note) <= 2000 or any(ord(char) < 32 for char in note):
        raise ValueError("请填写 5 至 2000 字的权属或授权依据说明。")
    collected = _root(collected_dir)
    inbox = _root(inbox_dir, create=True)
    if collected.is_relative_to(inbox):
        raise ValueError("采集原件不能已经位于自动剪辑投递箱内。")
    target_dir = _root(inbox / "抖音已选", create=True)
    if not target_dir.is_relative_to(inbox):
        raise ValueError("目标目录越过素材投递箱。")
    source = collected / (item_id + ".mp4")
    provenance_path = collected / (item_id + ".provenance.json")
    provenance, original_json, provenance_stat = _read_json(provenance_path, collected)
    expected_hash, expected_size = provenance.get("sha256"), provenance.get("size_bytes")
    if (provenance.get("version") != 1 or provenance.get("item_id") != item_id
            or provenance.get("source_url") != f"https://www.douyin.com/video/{item_id}"
            or not isinstance(expected_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_hash)
            or not isinstance(expected_size, int) or isinstance(expected_size, bool) or expected_size <= 0):
        raise ValueError("来源记录的视频 ID、地址、大小或校验信息无效。")
    target, sidecar = target_dir / (item_id + ".mp4"), target_dir / (item_id + ".provenance.json")
    if target.exists() or _is_link(target):
        if (_digest(source, collected) != expected_hash or _digest(target, target_dir) != expected_hash or not sidecar.exists()
                or not _matching_sidecar(_read_json(sidecar, target_dir)[0], provenance, rights_status, note)):
            raise ValueError("投递箱中已有不同的同名文件或授权记录，已保留现有内容。")
        return {"status": "already_adopted", "path": str(target), "provenance": str(sidecar)}
    existing_sidecar = sidecar.exists() or _is_link(sidecar)
    if existing_sidecar and not _matching_sidecar(_read_json(sidecar, target_dir)[0], provenance, rights_status, note):
        raise ValueError("投递箱中已有不同的授权记录，已保留现有内容。")
    nonce = uuid.uuid4().hex
    temporary = target_dir / (f".{item_id}-{nonce}.mp4.part")
    temporary_sidecar = target_dir / (f".{item_id}-{nonce}.provenance.part")
    created_sidecar_identity = None
    completed = False
    try:
        stream, source_stat = _open_regular(source, collected)
        digest, copied = hashlib.sha256(), 0
        with stream, temporary.open("xb") as output:
            while block := stream.read(1024 * 1024):
                output.write(block)
                digest.update(block)
                copied += len(block)
            output.flush()
            os.fsync(output.fileno())
            if _identity(os.fstat(stream.fileno())) != _identity(source_stat):
                raise ValueError("素材在复制过程中发生变化。")
        _unchanged(source, source_stat)
        _unchanged(provenance_path, provenance_stat)
        if copied != expected_size or digest.hexdigest() != expected_hash:
            raise ValueError("素材和采集记录的 SHA-256 或大小不一致，已停止采用。")
        if probe is None:
            from studio.media import inspect_media
            probe = inspect_media
        metadata = probe(temporary)
        duration = float(metadata.get("duration", 0))
        if not metadata.get("video_codec") or not math.isfinite(duration) or duration <= 0:
            raise ValueError("素材未通过 ffprobe 视频完整性检查。")
        if _digest(temporary, target_dir) != expected_hash:
            raise ValueError("待采用文件在检查过程中发生变化。")
        _unchanged(source, source_stat)
        if _read_json(provenance_path, collected)[1] != original_json:
            raise ValueError("来源记录在采用过程中发生变化。")
        if _root(target_dir) != target_dir or not target_dir.is_relative_to(inbox):
            raise ValueError("目标目录在采用过程中发生变化。")
        adoption = {"version": 1, "source_provenance": provenance,
                    "adoption": {"rights_status": rights_status, "rights_note": note,
                                 "adopted_at": datetime.now(timezone.utc).isoformat(), "verified_sha256": expected_hash,
                                 "probe": {key: metadata.get(key) for key in ("duration", "video_codec", "width", "height")}}}
        with temporary_sidecar.open("x", encoding="utf-8") as handle:
            json.dump(adoption, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        if not existing_sidecar:
            os.link(temporary_sidecar, sidecar)
            created_sidecar_identity = _identity(sidecar.lstat())
        elif not _matching_sidecar(_read_json(sidecar, target_dir)[0], provenance, rights_status, note):
            raise ValueError("目标授权记录在采用过程中发生变化。")
        # Commit the video last. Both creates are atomic and refuse replacement.
        os.link(temporary, target)
        completed = True
        return {"status": "adopted", "path": str(target), "provenance": str(sidecar), "sha256": expected_hash}
    finally:
        temporary.unlink(missing_ok=True)
        temporary_sidecar.unlink(missing_ok=True)
        if not completed and created_sidecar_identity is not None:
            try:
                if _identity(sidecar.lstat()) == created_sidecar_identity:
                    sidecar.unlink()
            except OSError:
                pass


def main():
    try:
        env = environment()
        collected = Path(env.get("CUTFLOW_DATA", str(ROOT / "data"))) / "collected" / "douyin"
        inbox = Path(env.get("CUTFLOW_INBOX", str(ROOT / "素材投递箱")))
        if not collected.is_dir():
            print("还没有本地采集文件。请先在抖音采集任务中保存视频。")
            return 1
        print("已采集视频（采用前请确认画面内容与商用授权）：")
        found = False
        for path in sorted(collected.glob("*.provenance.json"))[:50]:
            try:
                item, _, _ = _read_json(path, collected.resolve())
                item_id = item.get("item_id", "")
                if not re.fullmatch(r"[0-9]{1,32}", item_id) or not (collected / (item_id + ".mp4")).is_file():
                    continue
                title = re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", str(item.get("title", "")))[:70]
                print(f"{item_id}  {title}")
                found = True
            except (OSError, ValueError, TypeError):
                continue
        if not found:
            print("没有同时具备视频文件和来源记录的可采用素材。")
            return 1
        item_id = input("输入要采用的视频 ID：").strip()
        choice = input("权属：1=本人拥有版权；2=已取得适用于本次用途的授权；其他=取消：").strip()
        if choice not in {"1", "2"}:
            print("已取消，没有复制素材。")
            return 0
        note = input("填写权属或授权依据（例如原创拍摄说明、许可合同编号和范围）：").strip()
        result = adopt(collected, inbox, item_id, "owned" if choice == "1" else "licensed", note)
        print("素材已在投递箱中：" + result["path"])
        print("来源与授权记录已保留。后续剪辑按工作台设置运行；本工具不直接发布。")
        return 0
    except (KeyboardInterrupt, EOFError):
        print("已取消采用，没有发布内容。")
        return 1
    except ValueError as error:
        print("未采用：" + str(error))
        return 1
    except Exception:
        print("采用未完成。请检查素材完整性、FFmpeg 和目录权限；现有文件不会被覆盖。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
