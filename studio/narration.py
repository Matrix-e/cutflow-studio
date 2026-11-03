"""Offline Qwen3-TTS narration with real per-sentence audio timing.

Parent process uses the standard library. GPU dependencies live only in the
configured disposable worker. No hosted API, downloads, or fallback fake audio.
"""
from __future__ import annotations

import array
import contextlib
import hashlib
import html
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import wave

MODEL_ID = "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"
HARD_TIMEOUT = 600.0
MAX_TEXT = 1500
MAX_SEGMENTS = 32
_lock = threading.Lock()
STYLES = {"professional": "用专业、自然、清晰的普通话朗读，语速适中，语气平实可信，不夸张。",
          "warm": "用温暖、亲切、自然的普通话朗读，停顿清楚，不夸张。",
          "energetic": "用积极、有活力但不过度夸张的普通话朗读，吐字清楚，保持自然节奏。"}
VOICES = [("serena", "Serena · 温和女声", "female"), ("uncle_fu", "Uncle Fu · 沉稳男声", "male"),
          ("vivian", "Vivian · 明亮女声", "female"), ("dylan", "Dylan · 北京青年男声", "male"),
          ("eric", "Eric · 四川风格男声", "male")]


class NarrationError(RuntimeError):
    def __init__(self, message: str, code="generation_failed"):
        super().__init__(message)
        self.code = code


ERRORS = {"not_configured": "中文旁白模型尚未部署完成。", "busy": "旁白正在生成，请稍后重试。",
          "cancelled": "旁白生成已取消。", "timeout": "旁白生成超时，未交付不完整音频。",
          "invalid_request": "请提供有效的中文讲稿、声音和语气选项。",
          "gpu_unavailable": "中文旁白需要可用的 GPU。", "resource_limit": "旁白生成资源不足，请缩短讲稿或稍后重试。",
          "invalid_audio": "生成音频未通过完整性检查，未作为成功交付。",
          "generation_failed": "中文旁白生成失败，请检查模型环境后重试。"}


def _error(code):
    safe = code if isinstance(code, str) and code in ERRORS else "generation_failed"
    return NarrationError(ERRORS[safe], safe)


def list_voices() -> list[dict]:
    return [{"id": ident, "name": name, "language": "zh-CN", "gender": gender,
             "supports_style": True, "kind": "preset"} for ident, name, gender in VOICES]


def _model_exists(folder: Path) -> bool:
    try:
        config = json.loads((folder / "config.json").read_text(encoding="utf-8"))
        return (config.get("tts_model_type") == "custom_voice"
                and config.get("tts_model_size") in ("1b7", "1.7b", "1.7B")
                and any(folder.glob("*.safetensors"))
                and (folder / "speech_tokenizer" / "config.json").is_file()
                and any((folder / "speech_tokenizer").glob("*.safetensors"))
                and (folder / "tokenizer_config.json").is_file())
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def configuration_status() -> dict:
    folder, executable = os.environ.get("CUTFLOW_TTS_MODEL_DIR", ""), os.environ.get("CUTFLOW_TTS_PYTHON", "")
    exists, python_exists = bool(folder and _model_exists(Path(folder))), bool(executable and Path(executable).is_file())
    ready = exists and python_exists
    return {"configured": ready, "model_exists": exists, "python_exists": python_exists,
            "provider": "local_qwen_tts", "model": MODEL_ID, "voices": list_voices(),
            "reason": None if ready else ERRORS["not_configured"], "runtime_verified": False}


def split_sentences(text: str) -> list[str]:
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= MAX_TEXT or any(ord(c) < 32 and c not in "\n\r\t" for c in text):
        raise _error("invalid_request")
    pieces = re.findall(r"[^。！？!?\n]+[。！？!?]+[”’\"」』]?|[^。！？!?\n]+", text.strip())
    output = []
    for piece in pieces:
        piece = piece.strip()
        if not piece:
            continue
        if len(piece) <= 160:
            output.append(piece)
            continue
        clauses = re.findall(r"[^，,；;]+[，,；;]?", piece)
        current = ""
        for clause in clauses:
            if len(clause) > 160:
                raise NarrationError("单句过长，请添加自然的标点停顿后生成。", "invalid_request")
            if current and len(current) + len(clause) > 160:
                output.append(current)
                current = ""
            current += clause
        if current:
            output.append(current)
    if not output or len(output) > MAX_SEGMENTS:
        raise NarrationError("讲稿段落过多，请分成多个视频制作。", "invalid_request")
    if re.sub(r"\s+", "", "".join(output)) != re.sub(r"\s+", "", text):
        raise _error("invalid_request")
    return output


def _validate(text, voice_id, style):
    segments = split_sentences(text)
    if voice_id not in {v[0] for v in VOICES} or style not in STYLES:
        raise _error("invalid_request")
    return {"text": text.strip(), "voice_id": voice_id, "style": style, "sentences": segments}


def _stamp(seconds):
    milliseconds = max(0, round(seconds * 1000))
    return f"{milliseconds//3600000:02}:{milliseconds//60000%60:02}:{milliseconds//1000%60:02},{milliseconds%1000:03}"


def make_srt(segments: list[dict]) -> str:
    def safe(text):
        return html.escape(text, quote=False).replace("{", "｛").replace("}", "｝").replace("\\", "＼")
    return "\n\n".join(f"{i+1}\n{_stamp(s['start'])} --> {_stamp(s['end'])}\n{safe(s['text'])}"
                       for i, s in enumerate(segments)) + "\n"


def inspect_audio(path: str | Path) -> dict:
    try:
        with wave.open(str(path), "rb") as handle:
            channels, width, rate, frames = handle.getnchannels(), handle.getsampwidth(), handle.getframerate(), handle.getnframes()
            if channels != 1 or width != 2 or not 16000 <= rate <= 48000 or frames <= 0 or frames / rate > 600:
                raise _error("invalid_audio")
            count, square_sum, peak, clipped = 0, 0, 0, 0
            while block := handle.readframes(65536):
                samples = array.array("h", block)
                if sys.byteorder != "little":
                    samples.byteswap()
                count += len(samples)
                square_sum += sum(value * value for value in samples)
                peak = max(peak, max(map(abs, samples), default=0))
                clipped += sum(abs(value) >= 32760 for value in samples)
        if count != frames:
            raise _error("invalid_audio")
        rms = math.sqrt(square_sum / count) / 32768
        checks = [{"name": "complete_pcm", "passed": True},
                  {"name": "non_silent", "passed": rms > 0.0001},
                  {"name": "clipping", "passed": clipped / count < 0.005}]
        return {"duration": frames / rate, "sample_rate": rate, "channels": channels, "frames": frames,
                "peak_dbfs": 20 * math.log10(max(peak / 32768, 1e-12)),
                "rms_dbfs": 20 * math.log10(max(rms, 1e-12)),
                "qc": {"passed": all(x["passed"] for x in checks), "checks": checks,
                       "speech_content_verified": False}}
    except (OSError, EOFError, wave.Error) as exc:
        raise _error("invalid_audio") from exc


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def assemble_audio(sentences: list[str], audio_paths: list[Path], destination: Path) -> dict:
    if len(sentences) != len(audio_paths) or not sentences:
        raise _error("invalid_audio")
    inspections = [inspect_audio(path) for path in audio_paths]
    if any(not entry["qc"]["passed"] for entry in inspections) or len({entry["sample_rate"] for entry in inspections}) != 1:
        raise _error("invalid_audio")
    rate = inspections[0]["sample_rate"]
    clock, segments = 0, []
    with wave.open(str(destination), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        for index, (text, path, info) in enumerate(zip(sentences, audio_paths, inspections)):
            start = clock
            with wave.open(str(path), "rb") as source:
                while block := source.readframes(65536):
                    output.writeframesraw(block)
            clock += info["frames"]
            segments.append({"text": text, "start": start / rate, "end": clock / rate})
            if index < len(sentences) - 1:
                gap = round(rate * 0.10)
                output.writeframesraw(b"\x00\x00" * gap)
                clock += gap
    result = inspect_audio(destination)
    if not result["qc"]["passed"]:
        raise _error("invalid_audio")
    return {**result, "segments": segments, "sha256": _sha(destination), "timing_method": "measured_sentence_audio"}


def _kill(process):
    if process.poll() is None:
        process.kill()
    process.communicate(timeout=5)


def synthesize(text, voice_id, output_path, *, style="professional", stop_event=None) -> dict:
    request = _validate(text, voice_id, style)
    destination = Path(output_path).resolve()
    if destination.suffix.lower() != ".wav":
        raise _error("invalid_request")
    if stop_event is not None and stop_event.is_set():
        raise _error("cancelled")
    if not configuration_status()["configured"]:
        raise _error("not_configured")
    if not _lock.acquire(False):
        raise _error("busy")
    process = None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".cutflow-tts-", dir=destination.parent) as scratch:
            folder = Path(scratch)
            env = {**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
                   "HF_HUB_DISABLE_TELEMETRY": "1", "TOKENIZERS_PARALLELISM": "false",
                   "OMP_NUM_THREADS": "2", "MKL_NUM_THREADS": "2", "PYTHONIOENCODING": "utf-8"}
            process = subprocess.Popen([env["CUTFLOW_TTS_PYTHON"], "-m", "studio.narration", "--worker"],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, encoding="utf-8", errors="replace", env=env,
                                       cwd=Path(__file__).resolve().parent.parent,
                                       creationflags=0x08000000 if os.name == "nt" else 0)
            payload = json.dumps({**request, "work_dir": str(folder)}, ensure_ascii=False)
            deadline = time.monotonic() + HARD_TIMEOUT
            while True:
                if stop_event is not None and stop_event.is_set():
                    _kill(process)
                    raise _error("cancelled")
                if time.monotonic() >= deadline:
                    _kill(process)
                    raise _error("timeout")
                try:
                    stdout, _ = process.communicate(input=payload, timeout=0.25)
                    break
                except subprocess.TimeoutExpired:
                    payload = None
            if process.returncode:
                try:
                    error = json.loads(stdout).get("error", "generation_failed")
                except (ValueError, AttributeError):
                    error = "generation_failed"
                raise _error(error)
            staged = folder / "narration.wav"
            paths = [folder / f"sentence-{index:03}.wav" for index in range(len(request["sentences"]))]
            result = assemble_audio(request["sentences"], paths, staged)
            srt_text = make_srt(result["segments"])
            staged.replace(destination)
            srt = destination.with_suffix(".srt")
            srt.write_text(srt_text, encoding="utf-8")
            result.update(output_path=str(destination), srt_path=str(srt), srt_sha256=_sha(srt),
                          text=request["text"], script_sha256=hashlib.sha256(request["text"].encode("utf-8")).hexdigest(),
                          voice_id=voice_id, style=style, provider="local_qwen_tts", model=MODEL_ID,
                          warnings=["字幕时间来自每句实际合成音频，属于分句字幕，不是逐字强制对齐。",
                                    "完整讲稿已逐句提交合成；尚未用语音识别校验漏读或误读，需试听确认。"])
            destination.with_suffix(".tts.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            return result
    except OSError as exc:
        raise _error("generation_failed") from exc
    finally:
        try:
            if process is not None and process.poll() is None:
                _kill(process)
        finally:
            _lock.release()


def _worker(request):
    import numpy as np
    import torch
    from qwen_tts import Qwen3TTSModel

    checked = _validate(request["text"], request["voice_id"], request["style"])
    if not torch.cuda.is_available():
        raise _error("gpu_unavailable")
    if torch.cuda.mem_get_info(0)[0] < 6 * 1024 ** 3:
        raise _error("resource_limit")
    torch.set_num_threads(2)
    torch.cuda.set_per_process_memory_fraction(0.60, 0)
    model_dir = os.environ.get("CUTFLOW_TTS_MODEL_DIR", "")
    if not model_dir or not _model_exists(Path(model_dir)):
        raise _error("not_configured")
    model = Qwen3TTSModel.from_pretrained(model_dir, device_map="cuda:0", dtype=torch.bfloat16,
                                         attn_implementation="sdpa", local_files_only=True, use_safetensors=True,
                                         trust_remote_code=False)
    folder = Path(request["work_dir"]).resolve()
    if not folder.is_dir():
        raise _error("invalid_request")
    for index, sentence in enumerate(checked["sentences"]):
        wavs, sample_rate = model.generate_custom_voice(text=sentence, language="Chinese", speaker=checked["voice_id"],
                                                       instruct=STYLES[checked["style"]], max_new_tokens=1536,
                                                       do_sample=True, temperature=0.6, top_p=0.9)
        samples = np.asarray(wavs[0], dtype=np.float64).reshape(-1)
        if not samples.size or not np.isfinite(samples).all() or samples.size / sample_rate > 125:
            raise _error("invalid_audio")
        # Avoid integer wraparound while retaining clipping evidence for QC.
        pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2")
        target = folder / f"sentence-{index:03}.wav"
        with wave.open(str(target), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(int(sample_rate))
            handle.writeframes(pcm.tobytes())
        if not inspect_audio(target)["qc"]["passed"]:
            raise _error("invalid_audio")
    return {"sentences": len(checked["sentences"])}


def _main():
    if sys.argv[1:] == ["--status"]:
        print(json.dumps(configuration_status(), ensure_ascii=False))
        return 0
    try:
        raw = sys.stdin.buffer.read(32000)
        request = json.loads(raw.decode("utf-8"))
        if sys.argv[1:] == ["--worker"]:
            with contextlib.redirect_stdout(sys.stderr):
                result = _worker(request)
        else:
            result = synthesize(request["text"], request["voice_id"], request["output_path"], style=request.get("style", "professional"))
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except NarrationError as exc:
        print(json.dumps({"error": exc.code, "detail": str(exc)}, ensure_ascii=False))
        return 2
    except Exception as exc:
        code = "resource_limit" if type(exc).__name__ == "OutOfMemoryError" else "generation_failed"
        print(json.dumps({"error": code, "detail": ERRORS[code]}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(_main())
