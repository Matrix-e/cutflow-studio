"""Authenticated, bounded portrait library. Imports never create video jobs."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from uuid import UUID

from fastapi import Depends, HTTPException, Request
from fastapi.responses import Response

from . import presenter

MAX_BYTES = 8 * 1024 * 1024
MAX_ITEMS = 100


def _id(value):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(400, "肖像编号无效。") from None


def _label(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 80 or any(ord(c) < 32 for c in value):
        raise HTTPException(400, "请填写 1–80 字的形象名称。")
    return value.strip()


def _filename(value):
    if (not isinstance(value, str) or not value or len(value) > 160 or any(c in value for c in '/\\:<>"|?*')
            or value.endswith((' ', '.')) or any(ord(c) < 32 for c in value)
            or Path(value).suffix.lower() not in {".jpg", ".jpeg", ".png"}
            or re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", value, re.I)):
        raise HTTPException(400, "请选择名称正常的 JPG 或 PNG 肖像照片。")
    return value


def _image_bytes(raw):
    if not raw or len(raw) > MAX_BYTES:
        raise HTTPException(413, "肖像照片不能超过 8 MiB。")
    try:
        from PIL import Image, ImageOps, UnidentifiedImageError
    except ImportError:
        raise HTTPException(503, "肖像图片处理组件尚未安装。") from None
    try:
        with Image.open(io.BytesIO(raw)) as image:
            if image.format not in {"PNG", "JPEG"} or getattr(image, "n_frames", 1) != 1:
                raise ValueError("image format")
            if min(image.size) < 256 or max(image.size) > 8192 or image.width * image.height > 16_000_000:
                raise ValueError("image dimensions")
            image.load()
            image = ImageOps.exif_transpose(image).convert("RGB")
            image.thumbnail((2048, 2048))
            output = io.BytesIO()
            image.save(output, format="PNG")
            encoded = output.getvalue()
            if len(encoded) > MAX_BYTES:
                raise HTTPException(413, "图片解码后过大，请缩小照片尺寸再上传。")
            return encoded, image.size
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
        raise HTTPException(422, "照片无效；请使用 256 像素以上、清晰的单人 JPG 或 PNG。") from None


class PresenterLibrary:
    def __init__(self, data: Path):
        self.root = Path(data).resolve() / "presenters"
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink():
            raise HTTPException(400, "肖像目录不可使用链接。")
        self.database = self.root / "library.sqlite3"
        if self.database.is_symlink():
            raise HTTPException(400, "肖像数据库不可使用链接。")
        with self.connection() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS portraits(
                id TEXT PRIMARY KEY,kind TEXT NOT NULL,name TEXT NOT NULL,raw_sha256 TEXT NOT NULL,
                sha256 TEXT NOT NULL,image BLOB NOT NULL,width INTEGER NOT NULL,height INTEGER NOT NULL,
                consent_confirmed INTEGER NOT NULL,provenance TEXT NOT NULL,face_validated INTEGER NOT NULL DEFAULT 0,
                face_error TEXT,created_at TEXT NOT NULL)""")

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.database, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _get(self, identifier):
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM portraits WHERE id=?", (_id(identifier),)).fetchone()
        if row is None:
            raise HTTPException(404, "该肖像不存在，请重新选择。")
        return row

    @staticmethod
    def _entry(row, ready):
        validated = row["face_validated"] == 1
        provenance = json.loads(row['provenance'])
        voice = provenance.get('recommended_voice_id') if row['kind'] == 'public' else None
        if voice not in {'serena', 'uncle_fu', 'vivian', 'dylan', 'eric'}:
            voice = None
        reason = (None if ready and validated else row["face_error"] if row["face_error"] else
                  "肖像已保存，等待本地模型部署后验证人脸。" if not validated else presenter.ERRORS["not_configured"])
        return {"id": row["id"], "portrait_id": row["id"], "kind": row["kind"], "name": row["name"],
                "ready": ready and validated, "reason": reason, "preview_url": f"/api/presenters/{row['id']}/image",
                "outfits": [], "recommended_voice_id": voice, "created_at": row["created_at"]}

    def catalog(self):
        config = presenter.configuration_status()
        with self.connection() as conn:
            rows = conn.execute("SELECT id,kind,name,provenance,face_validated,face_error,created_at FROM portraits ORDER BY created_at DESC LIMIT ?", (MAX_ITEMS,)).fetchall()
        return {"presenters": [self._entry(row, config["configured"]) for row in rows], "backgrounds": [], "outfits": [],
                "configuration": config, "capabilities": dict(presenter.CAPABILITIES), "max_bytes": MAX_BYTES}

    def snapshot(self, identifier):
        row = self._get(identifier)
        return {"id": row["id"], "kind": row["kind"], "sha256": row["sha256"], "name": row["name"],
                "consent_confirmed": row["consent_confirmed"] == 1, "face_validated": row["face_validated"] == 1}

    def import_bytes(self, raw, *, identifier, name, filename, consent_confirmed, kind="personal", provenance=None):
        identifier, name = _id(identifier), _label(name)
        _filename(filename)
        if consent_confirmed is not True:
            raise HTTPException(403, "请先确认你本人或已取得肖像使用授权。")
        if kind not in {"personal", "public"}:
            raise HTTPException(400, "肖像类型无效。")
        if not raw or len(raw) > MAX_BYTES:
            raise HTTPException(413, "肖像照片不能超过 8 MiB。")
        if kind == "public" and not (isinstance(provenance, dict) and provenance.get("synthetic") is True
                                    and provenance.get("generator") and provenance.get("license")):
            raise HTTPException(400, "公共形象需要真实的合成来源和许可记录。")
        digest = hashlib.sha256(raw).hexdigest()
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM portraits WHERE id=?", (identifier,)).fetchone()
        if row is not None:
            if (row["raw_sha256"], row["name"], row["kind"]) != (digest, name, kind):
                raise HTTPException(409, "这个肖像编号已用于另一张照片或名称。")
            return {"presenter": self._entry(row, presenter.configuration_status()["configured"]), "duplicate": True}
        encoded, (width, height) = _image_bytes(raw)
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM portraits WHERE id=?", (identifier,)).fetchone()
            if row is not None:
                if (row["raw_sha256"], row["name"], row["kind"]) != (digest, name, kind):
                    raise HTTPException(409, "这个肖像编号已用于另一张照片或名称。")
                duplicate = True
            else:
                if conn.execute("SELECT COUNT(*) FROM portraits").fetchone()[0] >= MAX_ITEMS:
                    raise HTTPException(409, "肖像库已达到 100 张上限。")
                conn.execute("""INSERT INTO portraits(id,kind,name,raw_sha256,sha256,image,width,height,consent_confirmed,provenance,created_at)
                                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                             (identifier, kind, name, digest, hashlib.sha256(encoded).hexdigest(), encoded, width, height, 1,
                              json.dumps(provenance or {"user_authorized": True}, ensure_ascii=False), datetime.now(timezone.utc).isoformat()))
                duplicate = False
        # Upload authorizes image validation, never video generation. Imports made
        # before deployment remain useful; the explicit validate endpoint handles
        # those after the runtime becomes available.
        status = presenter.configuration_status()
        if status.get("model_exists"):
            try:
                return {**self.validate(identifier), "duplicate": duplicate}
            except HTTPException as error:
                if error.status_code != 503:
                    raise
        return {"presenter": self._entry(self._get(identifier), status["configured"]), "duplicate": duplicate}

    def image(self, identifier):
        row = self._get(identifier)
        content = bytes(row["image"])
        if hashlib.sha256(content).hexdigest() != row["sha256"]:
            raise HTTPException(409, "肖像文件校验失败，请重新上传。")
        return content

    def resolve(self, identifier, *, require_ready=True):
        row = self._get(identifier)
        if require_ready and (row["face_validated"] != 1 or not presenter.configuration_status()["configured"]):
            raise HTTPException(409, "该肖像或人物模型尚未验证完成。")
        target = self.root / (row["id"] + ".png")
        if target.is_symlink():
            raise HTTPException(409, "肖像缓存不可使用链接。")
        if target.exists():
            if presenter._sha(target) != row["sha256"]:
                raise HTTPException(409, "肖像缓存校验失败。")
            return target
        handle, temporary = tempfile.mkstemp(prefix=".portrait-", suffix=".part", dir=self.root)
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(self.image(identifier))
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError:
                if target.is_symlink() or presenter._sha(target) != row["sha256"]:
                    raise HTTPException(409, "肖像缓存内容不一致。") from None
        finally:
            Path(temporary).unlink(missing_ok=True)
        return target

    def validate(self, identifier):
        row = self._get(identifier)
        try:
            result = presenter.inspect_portrait(self.resolve(identifier, require_ready=False))
            valid = result.get("face_validated") is True
            reason = None if valid else presenter.ERRORS["invalid_portrait"]
        except presenter.PresenterError as error:
            if error.code == "not_configured":
                raise HTTPException(503, str(error)) from None
            valid, reason = False, str(error)
        with self.connection() as conn:
            conn.execute("UPDATE portraits SET face_validated=?,face_error=? WHERE id=?", (int(valid), reason, row["id"]))
        return {"presenter": self._entry(self._get(identifier), presenter.configuration_status()["configured"])}

    def register_public(self, image_path, *, identifier, name, provenance):
        path = Path(image_path)
        if path.is_symlink() or path.stat().st_size > MAX_BYTES:
            raise HTTPException(400, "公共肖像源文件无效。")
        return self.import_bytes(path.read_bytes(), identifier=identifier, name=name, filename=path.name,
                                 consent_confirmed=True, kind="public", provenance=provenance)


def register_presenter_routes(app, authorize, data):
    library = PresenterLibrary(Path(data))

    @app.get("/api/presenters", dependencies=[Depends(authorize)])
    def catalog():
        return library.catalog()

    @app.post("/api/presenters/import", dependencies=[Depends(authorize)])
    async def import_portrait(request: Request):
        params = request.query_params
        if set(params) != {"filename", "name", "id", "consent_confirmed"} or any(len(params.getlist(k)) != 1 for k in params):
            raise HTTPException(400, "肖像上传参数无效。")
        if params["consent_confirmed"] != "true":
            raise HTTPException(403, "请先确认肖像使用授权。")
        identifier, name, filename = _id(params["id"]), _label(params["name"]), _filename(params["filename"])
        if request.headers.get("content-type", "").split(";", 1)[0].strip() not in {"image/jpeg", "image/png", "application/octet-stream"}:
            raise HTTPException(415, "请上传 JPG 或 PNG 照片。")
        try:
            length = int(request.headers.get("content-length", "0"))
            if length < 0 or length > MAX_BYTES:
                raise HTTPException(413, "肖像照片不能超过 8 MiB。")
        except ValueError:
            raise HTTPException(400, "照片长度无效。") from None
        content = bytearray()
        async for chunk in request.stream():
            if len(content) + len(chunk) > MAX_BYTES:
                raise HTTPException(413, "肖像照片不能超过 8 MiB。")
            content.extend(chunk)
        if length and len(content) != length:
            raise HTTPException(400, "照片未完整上传，请使用原编号重试。")
        return library.import_bytes(bytes(content), identifier=identifier, name=name, filename=filename, consent_confirmed=True)

    @app.get("/api/presenters/{portrait_id}/image", dependencies=[Depends(authorize)])
    def image(portrait_id: str):
        return Response(library.image(portrait_id), media_type="image/png", headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})

    @app.post("/api/presenters/{portrait_id}/validate", dependencies=[Depends(authorize)])
    def validate(portrait_id: str):
        return library.validate(portrait_id)

    return library
