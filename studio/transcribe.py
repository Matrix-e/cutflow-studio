"""Optional offline ASR, deliberately independent of the normal render queue.

Requires a user-provided local faster-whisper/CTranslate2 model directory.
Nothing imports faster_whisper or downloads a model until explicitly invoked;
the invocation itself requires all local model/tokenizer files and disables hub
downloads. CPU and CUDA inference are deployment-dependent and are not claimed
as verified by mocked tests. Output is source-timed SRT + word-timed JSON, never
burnt-in subtitles or human-verified text.
"""
from __future__ import annotations

import argparse
import importlib
import json
import math
import os
from pathlib import Path
import tempfile
import textwrap

from .media import INPUT_OPTIONS, MediaError, _format_srt, _run, _tool, inspect_media


class TranscriptionError(RuntimeError):
    pass


def segments_to_srt(segments: list[dict], line_width: int = 28) -> str:
    """Format already source-timed segments without inventing or changing text."""
    if not 10 <= line_width <= 80:
        raise TranscriptionError("Subtitle line width must be between 10 and 80")
    cues, previous_end = [], 0.0
    for segment in segments:
        start, end = float(segment["start"]), float(segment["end"])
        if not all(math.isfinite(value) for value in (start, end)) or start < 0 or end <= start:
            raise TranscriptionError("ASR returned invalid subtitle timestamps")
        if start < previous_end - 0.001:
            raise TranscriptionError("ASR returned overlapping segments; review the transcript before using it")
        previous_end = end
        text = " ".join(str(segment.get("text", "")).split())
        if not text:
            continue
        lines = textwrap.wrap(text, width=line_width, break_long_words=True, break_on_hyphens=False)
        cues.append(f"{len(cues)+1}\n{_format_srt(start)} --> {_format_srt(end)}\n" + "\n".join(lines))
    return "\n\n".join(cues) + ("\n" if cues else "")


def _local_model(path: str | Path) -> Path:
    model = Path(path).resolve()
    if not model.is_dir():
        raise TranscriptionError("Provide an existing local model directory; model names and downloads are not accepted")
    missing = [name for name in ("model.bin", "config.json", "tokenizer.json")
               if not (model / name).is_file()]
    if missing:
        raise TranscriptionError("Offline model is incomplete; missing " + ", ".join(missing))
    # tokenizer.json is essential: faster-whisper otherwise calls a hub tokenizer
    # fallback even when the main model's local_files_only option is true.
    return model


def _decode_aligned_audio(source: Path, output: Path, info: dict):
    offset = info["audio_start"] - info["video_start"]
    align = f",adelay={round(offset*1000)}:all=1" if offset > 0 else (
        f",atrim=start={-offset:.6f},asetpts=PTS-STARTPTS" if offset < 0 else "")
    _run([_tool("ffmpeg"), "-hide_banner", "-nostdin", "-xerror", "-y", *INPUT_OPTIONS,
          "-i", str(source), "-map", "0:a:0", "-vn", "-af",
          f"asetpts=PTS-STARTPTS{align},apad=whole_dur={info['duration']:.6f}",
          "-t", str(info["duration"]), "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(output)],
         max(180, info["duration"] * 3))


def transcribe_source(source_path: str | Path, model_path: str | Path,
                      output_dir: str | Path, *, device: str = "cpu",
                      compute_type: str | None = None, language: str | None = "zh",
                      beam_size: int = 5, max_duration_seconds: float = 14400) -> dict:
    """Run explicitly requested offline transcription; preserve video time zero.

    No automatic fallback changes the selected device. Missing CUDA libraries,
    model incompatibility, or an absent dependency produce an actionable error.
    For long courses, split into chapters; the default duration cap is four hours.
    CPU decoding and Whisper feature extraction still consume RAM with CUDA.
    """
    model = _local_model(model_path)
    if device not in {"cpu", "cuda"}:
        raise TranscriptionError("device must be cpu or cuda")
    compute_type = compute_type or ("float16" if device == "cuda" else "int8")
    if compute_type not in {"float16", "float32", "int8", "int8_float16", "int8_float32"}:
        raise TranscriptionError("Unsupported compute_type")
    if not isinstance(beam_size, int) or not 1 <= beam_size <= 10:
        raise TranscriptionError("beam_size must be an integer between 1 and 10")
    if not math.isfinite(max_duration_seconds) or not 0 < max_duration_seconds <= 14400:
        raise TranscriptionError("max_duration_seconds must be positive and at most four hours")
    source = Path(source_path).resolve()
    info = inspect_media(source)
    if not info["has_audio"]:
        raise TranscriptionError("The source has no audio track")
    if info["duration"] > max_duration_seconds:
        raise TranscriptionError("Source exceeds the transcription limit; split this course into chapters")
    destination = Path(output_dir).resolve()
    srt_path = destination / f"{source.stem}.srt"
    json_path = destination / f"{source.stem}.transcript.json"
    if srt_path.exists() or json_path.exists():
        raise TranscriptionError("Transcript outputs already exist; choose another output directory")
    try:
        whisper_class = importlib.import_module("faster_whisper").WhisperModel
    except ImportError as exc:
        raise TranscriptionError("Optional faster-whisper dependency is not installed; install it in a dedicated ASR environment before running this command") from exc
    destination.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix=".cutflow-asr-", dir=destination) as scratch:
            audio_path = Path(scratch) / "source-aligned.wav"
            _decode_aligned_audio(source, audio_path, info)
            model_instance = whisper_class(str(model), device=device, compute_type=compute_type,
                                           local_files_only=True, num_workers=1)
            stream, recognition = model_instance.transcribe(str(audio_path), language=language,
                word_timestamps=True, beam_size=beam_size, vad_filter=False,
                condition_on_previous_text=False, temperature=0.0)
            segments = []
            for item in stream:
                start, end = float(item.start), float(item.end)
                if not all(math.isfinite(x) for x in (start, end)) or start < 0 or end <= start or end > info["duration"] + 0.1:
                    raise TranscriptionError("ASR produced an invalid or out-of-range source timestamp")
                words = []
                for word in getattr(item, "words", None) or []:
                    a, b, confidence = float(word.start), float(word.end), float(word.probability)
                    if not all(math.isfinite(x) for x in (a, b, confidence)) or a < start - 0.1 or b < a or b > end + 0.1:
                        raise TranscriptionError("ASR produced invalid word timing")
                    words.append({"start": round(a, 3), "end": round(b, 3), "text": str(word.word),
                                  "probability": max(0, min(1, confidence))})
                segments.append({"id": len(segments)+1, "start": round(start, 3),
                                 "end": round(min(end, info["duration"]), 3),
                                 "text": str(item.text).strip(), "words": words})
            if not segments:
                raise TranscriptionError("No speech was recognized; no empty subtitle file was created")
            srt = segments_to_srt(segments)
            if not srt.strip():
                raise TranscriptionError("No speech text was recognized; no empty subtitle file was created")
            report = {"version": 1, "source": str(source), "clock": "source_video_seconds",
                      "duration": info["duration"], "language": str(recognition.language),
                      "model_directory_name": model.name, "device": device, "compute_type": compute_type,
                      "segments": segments, "srt_path": str(srt_path), "json_path": str(json_path),
                      "validation": {"inference_completed": True, "human_verified": False,
                                     "burned_subtitles": False},
                      "warnings": ["机器转写未经人工校对，专有名词、数字和断句需复核。",
                                   "输出是原片时间轴 SRT 和逐词 JSON；字幕尚未烧录进视频。"]}
            staged_srt, staged_json = Path(scratch)/"result.srt", Path(scratch)/"result.json"
            staged_srt.write_text(srt, encoding="utf-8")
            staged_json.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
            # Same-volume hard links publish complete files without overwriting
            # existing subtitles, including another process winning the race.
            os.link(staged_srt, srt_path)
            try:
                os.link(staged_json, json_path)
            except OSError:
                if srt_path.exists() and os.path.samefile(staged_srt, srt_path):
                    srt_path.unlink()
                raise
            return report
    except (TranscriptionError, MediaError):
        raise
    except Exception as exc:
        raise TranscriptionError(f"Offline ASR failed on requested {device} device: {exc}") from exc


def main():
    parser = argparse.ArgumentParser(description="Explicit offline source-timed transcription; never downloads models")
    parser.add_argument("source")
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--device", choices=["cpu", "cuda"], default="cpu")
    parser.add_argument("--compute-type")
    parser.add_argument("--language", default="zh")
    args = parser.parse_args()
    try:
        result = transcribe_source(args.source, args.model_dir, args.output_dir,
            device=args.device, compute_type=args.compute_type, language=args.language)
    except (MediaError, TranscriptionError) as exc:
        parser.exit(1, f"{exc}\n")
    print(json.dumps({key: result[key] for key in ("srt_path", "json_path", "duration", "device")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
