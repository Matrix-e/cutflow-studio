"""Offline still-portrait lip sync using MuseTalk 1.5, Whisper, VAE and YuNet.

This adapter intentionally does not import the upstream DWPose, S3FD or BiSeNet
preprocessing stack. Its geometric lower-face blend is our own, narrower first
release. It changes mouth pixels, not clothing, scenery, or whole-body motion.
The MuseTalk latent/audio conditioning follows the upstream MIT implementation:
https://github.com/TMElyralab/MuseTalk (Tencent Music Entertainment Group, 2024).
Retain that notice together with the downloaded models' licenses.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import wave

REVISION = "musetalk15-yunet-still-v1"
MAX_SECONDS = 120
TIMEOUT = 900
CAPABILITIES = {"still_portrait_lipsync": True, "video_lipsync": False,
                "background_replacement": False, "outfit_generation": False,
                "outfit_selection": True, "whole_body_animation": False}
ERRORS = {"not_configured": "本地人物口型模型尚未部署并通过实际生成验证。",
          "invalid_portrait": "请提供清晰、无遮挡的单人正面肖像，脸部至少 160 像素。",
          "consent_required": "使用本人或已授权肖像前，请确认肖像授权。",
          "invalid_audio": "口播音频必须是完整、非静音的单声道 WAV，时长不超过 120 秒。",
          "invalid_request": "人物制作参数无效。", "busy": "人物口型正在生成，请稍后重试。",
          "cancelled": "人物口型生成已取消。", "timeout": "人物口型生成超时，未交付不完整视频。",
          "gpu_unavailable": "人物口型模型需要可用的 GPU。", "resource_limit": "GPU 空闲显存不足，请等待其他任务完成。",
          "generation_failed": "人物口型生成失败，请检查本地模型环境。",
          "invalid_output": "人物视频未通过时长、画面或音频完整性检查。"}
_lock = threading.Lock()


class PresenterError(RuntimeError):
    def __init__(self, code="generation_failed"):
        self.code = code if code in ERRORS else "generation_failed"
        super().__init__(ERRORS[self.code])


def model_files(root: Path) -> list[Path]:
    def choice(*paths):
        return next((root / p for p in paths if (root / p).is_file()), root / paths[0])
    return [root / "musetalkV15/musetalk.json", root / "musetalkV15/unet.pth",
            root / "sd-vae/config.json", choice("sd-vae/diffusion_pytorch_model.safetensors", "sd-vae/diffusion_pytorch_model.bin"),
            root / "whisper/config.json", root / "whisper/preprocessor_config.json",
            choice("whisper/model.safetensors", "whisper/pytorch_model.bin"),
            root / "yunet/face_detection_yunet_2023mar.onnx"]


def _signature(root: Path) -> str:
    rows = []
    for path in model_files(root):
        info = path.stat()
        if not path.is_file() or path.is_symlink() or info.st_size < 20:
            raise ValueError("incomplete model")
        rows.append([path.relative_to(root).as_posix(), info.st_size, info.st_mtime_ns])
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


def configuration_status() -> dict:
    root_value, executable = os.environ.get("CUTFLOW_PRESENTER_MODEL_DIR", ""), os.environ.get("CUTFLOW_PRESENTER_PYTHON", "")
    models, verified = False, False
    if root_value:
        root = Path(root_value)
        try:
            signature = _signature(root)
            models = True
            marker = json.loads((root / "runtime-validation.json").read_text(encoding="utf-8"))
            verified = (marker.get("revision") == REVISION and marker.get("model_signature") == signature
                        and marker.get("python") == executable and marker.get("qc_passed") is True)
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    ready = models and verified and bool(executable and Path(executable).is_file())
    return {"configured": ready, "ready": ready, "provider": "local_musetalk", "revision": REVISION,
            "model_exists": models, "runtime_verified": verified, "capabilities": dict(CAPABILITIES),
            "reason": None if ready else ERRORS["not_configured"]}


def list_presenters(data_dir=None) -> list[dict]:
    from .presenters_service import PresenterLibrary
    return PresenterLibrary(Path(data_dir or os.environ.get("CUTFLOW_DATA", "data"))).catalog()["presenters"]


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _kill(process):
    if process.poll() is None:
        if os.name != "nt":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
    process.communicate(timeout=5)


def _child(payload, *, stop_event=None, timeout=TIMEOUT):
    executable = os.environ.get("CUTFLOW_PRESENTER_PYTHON", "")
    if not executable or not Path(executable).is_file():
        raise PresenterError("not_configured")
    env = {**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1",
           "TOKENIZERS_PARALLELISM": "false", "OMP_NUM_THREADS": "2", "PYTHONIOENCODING": "utf-8"}
    process = subprocess.Popen([executable, "-m", "studio.presenter", "--worker"], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                               errors="replace", cwd=Path(__file__).resolve().parent.parent, env=env,
                               start_new_session=os.name != "nt",
                               creationflags=0x08000000 if os.name == "nt" else 0)
    pending = json.dumps(payload, ensure_ascii=False)
    deadline = time.monotonic() + timeout
    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                raise PresenterError("cancelled")
            if time.monotonic() >= deadline:
                raise PresenterError("timeout")
            try:
                stdout, _ = process.communicate(input=pending, timeout=.25)
                break
            except subprocess.TimeoutExpired:
                pending = None
        try:
            result = json.loads(stdout)
        except (ValueError, TypeError):
            raise PresenterError() from None
        if process.returncode or result.get("error"):
            raise PresenterError(result.get("error"))
        return result
    finally:
        if process.poll() is None:
            _kill(process)


def inspect_portrait(path) -> dict:
    return _child({"action": "inspect", "portrait": str(Path(path).resolve())}, timeout=45)


def _inspect_output(path, duration):
    from .media import inspect_media
    item = inspect_media(Path(path))
    if (not item.get("has_audio") or abs(item["duration"] - duration) > .3 or item["duration"] < .25
            or min(item["width"], item["height"]) < 256):
        raise PresenterError("invalid_output")
    decoded = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-protocol_whitelist", "file,pipe",
                              "-i", str(path), "-f", "null", "-"], capture_output=True, timeout=max(30, duration * 3),
                             creationflags=0x08000000 if os.name == "nt" else 0)
    if decoded.returncode:
        raise PresenterError("invalid_output")
    return item


def render_presenter(portrait_path, audio_path, output_path, *, work_dir=None, consent_confirmed=False,
                     preset_id=None, stop_event=None, _validation=False) -> dict:
    if consent_confirmed is not True:
        raise PresenterError("consent_required")
    if stop_event is not None and stop_event.is_set():
        raise PresenterError("cancelled")
    portrait, audio, output = Path(portrait_path).resolve(), Path(audio_path).resolve(), Path(output_path).resolve()
    if (not portrait.is_file() or not audio.is_file() or portrait.suffix.lower() not in {".png", ".jpg", ".jpeg"}
            or output.suffix.lower() != ".mp4" or output.exists() or output in {portrait, audio}):
        raise PresenterError("invalid_request")
    from .narration import inspect_audio
    try:
        info = inspect_audio(audio)
        if not info["qc"]["passed"] or not .3 <= info["duration"] <= MAX_SECONDS:
            raise ValueError("audio")
    except (RuntimeError, ValueError, KeyError):
        raise PresenterError("invalid_audio") from None
    status = configuration_status()
    if not status["configured"] and not (_validation and status["model_exists"]):
        raise PresenterError("not_configured")
    if not _lock.acquire(False):
        raise PresenterError("busy")
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".presenter-", dir=output.parent) as temporary:
            folder = Path(temporary)
            staged = folder / "presenter.mp4"
            result = _child({"action": "render", "portrait": str(portrait), "audio": str(audio),
                             "output": str(staged), "duration": info["duration"]}, stop_event=stop_event)
            inspected = _inspect_output(staged, info["duration"])
            if result.get("frames", 0) < 2 or result.get("mouth_variation", 0) <= .2:
                raise PresenterError("invalid_output")
            os.link(staged, output)  # Create-only, even if another writer won the race.
            return {"output_path": str(output), "sha256": _sha(output), "duration": inspected["duration"],
                    "width": inspected["width"], "height": inspected["height"], "provider": "local_musetalk",
                    "revision": REVISION, "preset_id": preset_id, "qc": {"passed": True, "lip_sync_human_review_required": True},
                    "warnings": ["静态肖像口型同步；未生成眨眼、头部或肢体动作。", "嘴部边缘、牙齿及语音同步仍需观看审核。"]}
    finally:
        _lock.release()


def validate_runtime(portrait_path, audio_path, output_path, *, consent_confirmed=False):
    result = render_presenter(portrait_path, audio_path, output_path, consent_confirmed=consent_confirmed, _validation=True)
    root = Path(os.environ["CUTFLOW_PRESENTER_MODEL_DIR"])
    marker = {"revision": REVISION, "model_signature": _signature(root), "python": os.environ["CUTFLOW_PRESENTER_PYTHON"],
              "qc_passed": result["qc"]["passed"], "output_sha256": result["sha256"], "validated_at": time.time()}
    temporary = root / (".runtime-validation-" + os.urandom(8).hex() + ".json")
    temporary.write_text(json.dumps(marker), encoding="utf-8")
    temporary.replace(root / "runtime-validation.json")
    return result


def _face(image, model):
    import cv2
    height, width = image.shape[:2]
    scale = min(1.0, 640 / max(width, height))
    reduced = cv2.resize(image, (round(width * scale), round(height * scale)))
    detector = cv2.FaceDetectorYN.create(str(model), "", (reduced.shape[1], reduced.shape[0]), .85, .3, 5000)
    _, faces = detector.detect(reduced)
    if faces is None or len(faces) != 1:
        raise PresenterError("invalid_portrait")
    face = faces[0].copy()
    face[:14] /= scale
    x, y, w, h = map(float, face[:4])
    right, left, nose = face[4:6], face[6:8], face[8:10]
    if (min(w, h) < 160 or abs(float(right[1] - left[1])) > h * .12
            or abs(float(nose[0]) - (float(right[0] + left[0]) / 2)) > w * .18):
        raise PresenterError("invalid_portrait")
    # Match MuseTalk's nose-centred face crop without a pose/parser checkpoint.
    bottom = min(height, math.ceil(y + h + .04 * h))
    top = max(0, math.floor(2 * float(nose[1]) - bottom))
    x1, x2 = max(0, math.floor(x)), min(width, math.ceil(x + w))
    if x2 - x1 < 160 or bottom - top < 160 or top == 0 or bottom == height:
        raise PresenterError("invalid_portrait")
    return (x1, top, x2, bottom)


def _worker(request):
    import cv2
    import numpy as np
    from PIL import Image, ImageOps
    root = Path(os.environ.get("CUTFLOW_PRESENTER_MODEL_DIR", ""))
    image_path = Path(request["portrait"])
    try:
        with Image.open(image_path) as source:
            if source.format not in {"JPEG", "PNG"} or source.width * source.height > 16_000_000:
                raise PresenterError("invalid_portrait")
            source = ImageOps.exif_transpose(source).convert("RGB")
            source.thumbnail((1280, 1280))
            image = cv2.cvtColor(np.asarray(source), cv2.COLOR_RGB2BGR)
    except (OSError, ValueError):
        raise PresenterError("invalid_portrait") from None
    box = _face(image, root / "yunet/face_detection_yunet_2023mar.onnx")
    if request.get("action") == "inspect":
        return {"face_validated": True, "width": image.shape[1], "height": image.shape[0], "face_bbox": list(box)}
    import torch
    from diffusers import AutoencoderKL, UNet2DConditionModel
    from transformers import WhisperFeatureExtractor, WhisperModel
    if not torch.cuda.is_available():
        raise PresenterError("gpu_unavailable")
    if torch.cuda.mem_get_info()[0] < 5 * 1024 ** 3:
        raise PresenterError("resource_limit")
    torch.set_num_threads(2)
    torch.manual_seed(0)
    device, dtype = torch.device("cuda:0"), torch.float16
    vae = AutoencoderKL.from_pretrained(str(root / "sd-vae"), local_files_only=True).eval().to(device, dtype)
    config = json.loads((root / "musetalkV15/musetalk.json").read_text())
    unet = UNet2DConditionModel.from_config(config)
    unet.load_state_dict(torch.load(root / "musetalkV15/unet.pth", map_location="cpu", weights_only=True))
    unet.eval().to(device, dtype)
    extractor = WhisperFeatureExtractor.from_pretrained(str(root / "whisper"), local_files_only=True)
    whisper = WhisperModel.from_pretrained(str(root / "whisper"), local_files_only=True).eval().to(device, dtype)
    output, audio = Path(request["output"]), Path(request["audio"])
    pcm = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-protocol_whitelist", "file,pipe", "-i", str(audio),
                          "-ac", "1", "-ar", "16000", "-f", "f32le", "pipe:1"], capture_output=True, check=True, timeout=60).stdout
    samples = np.frombuffer(pcm, dtype="<f4")
    fps, frames = 25, math.ceil(len(samples) / 16000 * 25)
    if not 2 <= frames <= MAX_SECONDS * fps:
        raise PresenterError("invalid_audio")
    x1, y1, x2, y2 = box
    crop = cv2.resize(image[y1:y2, x1:x2], (256, 256), interpolation=cv2.INTER_LANCZOS4)
    rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB).astype(np.float32) / 255
    with torch.inference_mode():
        tensor = torch.from_numpy(rgb).permute(2, 0, 1).unsqueeze(0).to(device, dtype)
        masked = tensor.clone()
        masked[:, :, 128:] = 0
        latent = torch.cat([vae.encode(masked * 2 - 1).latent_dist.sample(), vae.encode(tensor * 2 - 1).latent_dist.sample()], 1) * vae.config.scaling_factor
        hidden = []
        for start in range(0, len(samples), 480000):
            features = extractor(samples[start:start + 480000], sampling_rate=16000, return_tensors="pt").input_features.to(device, dtype)
            hidden.append(torch.stack(whisper.encoder(features, output_hidden_states=True).hidden_states, dim=2))
        hidden = torch.cat(hidden, dim=1)[:, :math.floor(len(samples) / 16000 * 50)]
        hidden = torch.cat([torch.zeros_like(hidden[:, :4]), hidden, torch.zeros_like(hidden[:, :12])], dim=1)
        positions = torch.arange(50, device=device, dtype=torch.float32).unsqueeze(1)
        frequencies = torch.exp(torch.arange(0, 384, 2, device=device).float() * (-math.log(10000.0) / 384))
        encoding = torch.empty((1, 50, 384), device=device)
        encoding[0, :, 0::2] = torch.sin(positions * frequencies)
        encoding[0, :, 1::2] = torch.cos(positions * frequencies)
        encoding = encoding.to(dtype)
        # A soft lower-face ellipse protects eyes, hair and the original clothing.
        yy, xx = np.mgrid[:256, :256]
        distance = ((xx - 128) / 111) ** 2 + ((yy - 190) / 87) ** 2
        alpha = np.clip((1 - distance) * 5, 0, 1) * np.clip((yy - 124) / 24, 0, 1)
        alpha = cv2.resize(alpha.astype(np.float32), (x2 - x1, y2 - y1))[..., None]
        height, width = image.shape[:2]
        command = ["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
                   "-s", f"{width}x{height}", "-r", "25", "-i", "pipe:0", "-protocol_whitelist", "file,pipe", "-i", str(audio),
                   "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-crf", "18", "-preset", "fast",
                   "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
                   "-shortest", "-movflags", "+faststart", str(output)]
        first, variation = None, 0.0
        with tempfile.TemporaryFile() as errors:
            encoder = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errors)
            try:
                for start in range(0, frames, 4):
                    indices = range(start, min(start + 4, frames))
                    batch = torch.cat([hidden[:, i * 2:i * 2 + 10] for i in indices], dim=0).reshape(-1, 50, 384)
                    predicted = unet(latent.repeat(len(indices), 1, 1, 1), torch.tensor([0], device=device), encoder_hidden_states=batch + encoding).sample
                    decoded = vae.decode(predicted / vae.config.scaling_factor).sample
                    generated = ((decoded / 2 + .5).clamp(0, 1).permute(0, 2, 3, 1).float().cpu().numpy() * 255).round().astype(np.uint8)
                    for mouth in generated:
                        mouth = mouth[..., ::-1]
                        if first is None:
                            first = mouth.copy()
                        else:
                            variation = max(variation, float(np.abs(mouth[140:240, 40:216].astype(np.float32) - first[140:240, 40:216]).mean()))
                        patch = cv2.resize(mouth, (x2 - x1, y2 - y1), interpolation=cv2.INTER_LANCZOS4)
                        frame = image.copy()
                        frame[y1:y2, x1:x2] = np.clip(patch * alpha + frame[y1:y2, x1:x2] * (1 - alpha), 0, 255).astype(np.uint8)
                        encoder.stdin.write(frame.tobytes())
                encoder.stdin.close()
                if encoder.wait(timeout=60):
                    raise PresenterError("invalid_output")
            finally:
                if encoder.poll() is None:
                    encoder.kill()
                    encoder.wait(timeout=5)
    return {"frames": frames, "mouth_variation": variation}


def _main():
    try:
        request = json.load(sys.stdin)
        with contextlib.redirect_stdout(sys.stderr):
            result = _worker(request)
        print(json.dumps(result))
        return 0
    except Exception as error:
        print(json.dumps({"error": getattr(error, "code", "generation_failed")}))
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
