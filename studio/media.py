"""Conservative speech editing and measurable delivery checks.

This engine shortens long acoustic pauses, never invents semantic edits. Keep a
human review before commercial release: silence detection cannot judge meaning,
speaker intent, factual claims, or whether an intentional pause should remain.
Original media is immutable. All subprocesses use argument lists, never a shell.
"""
from __future__ import annotations

import functools
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any


class MediaError(RuntimeError):
    pass


DEFAULTS = {
    "silence_min": 0.8,
    "silence_db": -38.0,
    "padding": 0.2,
    "min_clip": 0.5,
    "target_aspect": "16:9",
    "fps": 30,
    "encoder": "cpu",
    "crf": 18,
    "normalize_audio": True,
    "remove_silence": True,
}

# A file disguised as MP4 must not make FFmpeg follow network playlists or read
# a concat script. These formats cover the local-container inputs in the API.
INPUT_OPTIONS = ["-protocol_whitelist", "file", "-format_whitelist", "mov,matroska,webm,avi,mpegts"]


def _run(args: list[str], timeout: float = 600) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(args, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=timeout,
                                creationflags=0x08000000 if os.name == "nt" else 0)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MediaError(f"Media tool could not run: {type(exc).__name__}: {exc}") from exc
    if result.returncode:
        raise MediaError(f"{Path(args[0]).name} failed: {result.stderr[-4000:]}")
    return result


def _tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise MediaError(f"{name} is not installed or is not on PATH")
    return path


def _float(value: Any, fallback: float = 0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else fallback
    except (ValueError, TypeError):
        return fallback


def _settings(settings: dict | None) -> dict:
    result = {**DEFAULTS, **(settings or {})}
    bounds = {"silence_min": (0.3, 20), "silence_db": (-80, -15),
              "padding": (0.05, 2), "min_clip": (0.1, 10),
              "fps": (10, 60), "crf": (14, 28)}
    for name, (low, high) in bounds.items():
        value = _float(result[name], float("nan"))
        if not math.isfinite(value) or not low <= value <= high:
            raise MediaError(f"{name} must be between {low} and {high}")
        result[name] = value
    if result["target_aspect"] not in ("original", "16:9", "9:16", "1:1"):
        raise MediaError("target_aspect must be original, 16:9, 9:16, or 1:1")
    if result["encoder"] not in ("cpu", "nvenc"):
        raise MediaError("encoder must be cpu or nvenc")
    return result


def inspect_media(path: str | Path) -> dict:
    """Probe a local video; return a JSON-serializable technical inventory."""
    source = Path(path).resolve()
    if not source.is_file():
        raise MediaError("Source video does not exist")
    result = _run([_tool("ffprobe"), "-v", "error", *INPUT_OPTIONS, "-show_format", "-show_streams",
                   "-of", "json", str(source)])
    raw = json.loads(result.stdout)
    videos = [s for s in raw.get("streams", []) if s.get("codec_type") == "video"
              and not s.get("disposition", {}).get("attached_pic")]
    audios = [s for s in raw.get("streams", []) if s.get("codec_type") == "audio"]
    if not videos:
        raise MediaError("Source has no video stream")
    v = videos[0]
    a = audios[0] if audios else None
    duration = _float(v.get("duration"), _float(raw.get("format", {}).get("duration")))
    if duration <= 0:
        raise MediaError("Source has no valid finite duration")
    rotation = next((_float(d.get("rotation")) for d in v.get("side_data_list", [])
                     if "rotation" in d), _float(v.get("tags", {}).get("rotate")))
    sar_text = v.get("sample_aspect_ratio", "1:1")
    try:
        numerator, denominator = map(float, sar_text.split(":"))
        sar = numerator / denominator if denominator else 1
    except (ValueError, AttributeError):
        sar = 1
    width, height = int(v["width"]), int(v["height"])
    display_width, display_height = round(width * (sar or 1)), height
    if round(rotation) % 180:
        display_width, display_height = display_height, display_width
    fps_text = v.get("avg_frame_rate", "0/1")
    try:
        n, d = map(float, fps_text.split("/"))
        fps = n / d if d else 0
    except (ValueError, AttributeError):
        fps = 0
    return {
        "path": str(source), "size_bytes": source.stat().st_size,
        "modified_ns": source.stat().st_mtime_ns, "duration": duration,
        "width": width, "height": height, "display_width": display_width,
        "display_height": display_height, "rotation": rotation, "fps": fps,
        "frame_rate": fps_text, "has_audio": bool(a),
        "video_codec": v.get("codec_name"), "pixel_format": v.get("pix_fmt"),
        "video_start": _float(v.get("start_time")),
        "audio_start": _float(a.get("start_time")) if a else None,
        "audio_duration": _float(a.get("duration")) if a else None,
        "audio_codec": a.get("codec_name") if a else None,
        "audio_sample_rate": int(a.get("sample_rate", 0)) if a else None,
        "audio_channels": a.get("channels") if a else None,
        "color_transfer": v.get("color_transfer"), "color_primaries": v.get("color_primaries"),
        "color_space": v.get("color_space"), "field_order": v.get("field_order"),
        "format": raw.get("format", {}).get("format_name"),
    }


def _analyze_audio(source: str, info: dict, settings: dict) -> dict:
    if not info["has_audio"]:
        return {"silences": [], "peak_db": None}
    # Detector time begins with audio. Convert it into the video-relative clock.
    result = _run([_tool("ffmpeg"), "-hide_banner", "-nostdin", "-xerror", *INPUT_OPTIONS, "-i", source,
                   "-map", "0:a:0", "-vn", "-af",
                   f"asetpts=PTS-STARTPTS,silencedetect=n={settings['silence_db']}dB:d={settings['silence_min']},volumedetect",
                   "-f", "null", "-"], max(180, info["duration"] * 3))
    offset = info["audio_start"] - info["video_start"]
    duration = info["duration"]
    found, start = [], None
    for kind, value in re.findall(r"silence_(start|end):\s*(-?[\d.]+)", result.stderr):
        moment = min(duration, max(0, float(value) + offset))
        if kind == "start":
            start = moment
        elif start is not None:
            found.append((start, moment))
            start = None
    if start is not None:
        found.append((start, duration))
    peak = re.search(r"max_volume:\s*(-?[\d.]+) dB", result.stderr)
    return {"silences": found, "peak_db": float(peak.group(1)) if peak else None}


def _retained_ranges(duration: float, silences: list, settings: dict) -> list:
    padding = settings["padding"]
    cursor, ranges = 0.0, []
    for begin, end in silences:
        left = max(cursor, 0 if begin <= 0.001 else begin + padding)
        right = min(duration, duration if end >= duration - 0.001 else end - padding)
        if right - left < 0.15:
            continue
        if left > cursor:
            ranges.append([cursor, left])
        cursor = right
    if cursor < duration:
        ranges.append([cursor, duration])
    # Tiny retained speech fragments are joined by reinstating a removed pause;
    # speech is never discarded to meet the minimum shot duration.
    index = 0
    while len(ranges) > 1 and index < len(ranges):
        if ranges[index][1] - ranges[index][0] < settings["min_clip"]:
            if index + 1 < len(ranges):
                ranges[index:index+2] = [[ranges[index][0], ranges[index+1][1]]]
            else:
                ranges[index-1:index+1] = [[ranges[index-1][0], ranges[index][1]]]
                index = max(0, index - 1)
        else:
            index += 1
    return ranges or [[0, duration]]


def _frame_clips(ranges: list, source_duration: float, fps: float) -> list[dict]:
    """Expand cuts to frame boundaries, then account for any final partial frame.

    Concat pads each segment to its longest stream. Explicit frame lengths avoid
    adding an unrecorded frame at every join and drifting subtitles in long work.
    """
    if not isinstance(ranges, list) or not ranges or len(ranges) > 500:
        raise MediaError("A plan must have 1–500 ranges; split longer material into chapters")
    aligned, last_end = [], 0.0
    for pair in ranges:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise MediaError("Each source range must contain a start and end")
        try:
            begin, end = map(float, pair)
        except (ValueError, TypeError) as exc:
            raise MediaError("Source range times must be numeric") from exc
        if not all(math.isfinite(x) for x in (begin, end)) or begin < last_end - 0.001 or end > source_duration + 0.001 or end <= begin:
            raise MediaError("Source ranges must be ordered, non-overlapping, and inside source duration")
        last_end = end
        begin = max(0, math.floor(begin * fps + 0.0001) / fps)
        end = min(source_duration, math.ceil(end * fps - 0.0001) / fps)
        if aligned and begin <= aligned[-1][1] + 0.000001:
            aligned[-1][1] = end
        else:
            aligned.append([begin, end])
    clips, clock = [], 0.0
    for index, (begin, end) in enumerate(aligned):
        frames = max(1, math.ceil((end - begin) * fps - 0.0001))
        duration = frames / fps
        clips.append({"id": f"clip-{index+1:04d}", "source_in": round(begin, 6),
                      "source_out": round(end, 6), "source_duration": round(end-begin, 6),
                      "timeline_start": round(clock, 6), "timeline_end": round(clock+duration, 6),
                      "duration": round(duration, 6), "frames": frames,
                      "tail_padding": round(max(0, duration - (end-begin)), 6)})
        clock += duration
    return clips


def build_plan(path: str | Path, settings: dict | None = None) -> dict:
    """Create an explicit conservative edit decision list before rendering."""
    cfg = _settings(settings)
    info = inspect_media(path)
    warnings = ["自动剪辑只处理较长静音；内容逻辑、自然停顿和商业表述仍需人工审片。"]
    if not info["has_audio"]:
        warnings.append("素材没有音轨：保留全部画面，不执行语音停顿剪辑。")
    if info["color_transfer"] in ("smpte2084", "arib-std-b67"):
        warnings.append("检测到 HDR 素材；当前版本需要先做受控 SDR 转换，禁止直接改色彩标签。")
    if info["field_order"] not in (None, "unknown", "progressive"):
        warnings.append("检测到隔行素材；请先转为逐行扫描以保证运动画面质量。")
    analysis = _analyze_audio(info["path"], info, cfg) if cfg["remove_silence"] or cfg["normalize_audio"] else {"silences": [], "peak_db": None}
    silence = analysis["silences"] if cfg["remove_silence"] else []
    if sum(end - start for start, end in silence) > info["duration"] * 0.95:
        silence = []
        warnings.append("几乎整条音轨低于静音阈值，已保留原片；请检查收音或调低检测阈值。")
    ranges = _retained_ranges(info["duration"], silence, cfg)
    if cfg.get("source_ranges") is not None:
        ranges = cfg["source_ranges"]
        warnings.append("已使用人工指定的源片区间，请复核每个切点是否截断词句。")
    clips = _frame_clips(ranges, info["duration"], cfg["fps"])
    clock = clips[-1]["timeline_end"]
    warnings.append("切点向外对齐至输出帧以保留词尾；末尾不足一帧时补齐画面和静音，时间线已记录。")
    return {"version": 1, "source": info["path"], "source_info": info,
            "settings": cfg, "clips": clips, "duration": round(clock, 6),
            "removed_seconds": round(info["duration"] - clock, 6),
            "audio_peak_db": analysis["peak_db"],
            "silences": [{"start": a, "end": b} for a, b in silence], "warnings": warnings}


def _dimensions(info: dict, cfg: dict) -> tuple[int, int]:
    defaults = {"16:9": (1920, 1080), "9:16": (1080, 1920), "1:1": (1080, 1080),
                "original": (info["display_width"], info["display_height"])}
    width, height = defaults[cfg["target_aspect"]]
    width, height = int(cfg.get("width", width)), int(cfg.get("height", height))
    if width < 32 or height < 32 or width > 4096 or height > 4096:
        raise MediaError("Output dimensions must be between 32 and 4096")
    return width - width % 2, height - height % 2


@functools.lru_cache(maxsize=1)
def _nvenc_available() -> bool:
    try:
        _run([_tool("ffmpeg"), "-hide_banner", "-v", "error", "-f", "lavfi",
              "-i", "color=size=640x360:rate=30", "-frames:v", "1", "-pix_fmt", "yuv420p", "-c:v", "h264_nvenc",
              "-f", "null", "-"], 20)
        return True
    except MediaError:
        return False


def _subtitle_time(text: str) -> float:
    h, m, rest = text.replace(",", ".").split(":")
    return int(h) * 3600 + int(m) * 60 + float(rest)


def _format_srt(seconds: float) -> str:
    ms = max(0, round(seconds * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def remap_srt(text: str, clips: list[dict]) -> str:
    """Map supplied source-timed subtitles through cuts; never generate words."""
    cues = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip()):
        lines = block.splitlines()
        time_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if time_index is None:
            continue
        match = re.match(r"\s*(\d+:\d+:\d+[,.]\d+)\s*-->\s*(\d+:\d+:\d+[,.]\d+)", lines[time_index])
        if not match:
            raise MediaError("Invalid SRT timestamp")
        begin, end = map(_subtitle_time, match.groups())
        words = "\n".join(lines[time_index+1:]).strip()
        if end <= begin or not words:
            continue
        intervals = []
        for clip in clips:
            a, b = max(begin, clip["source_in"]), min(end, clip["source_out"])
            if b > a:
                start = clip["timeline_start"] + a - clip["source_in"]
                finish = clip["timeline_start"] + b - clip["source_in"]
                if intervals and abs(start - intervals[-1][1]) < 0.002:
                    intervals[-1][1] = finish
                else:
                    intervals.append([start, finish])
        cues.extend((start, end, words) for start, end in intervals)
    cues.sort(key=lambda cue: cue[0])
    return "\n\n".join(f"{i+1}\n{_format_srt(a)} --> {_format_srt(b)}\n{words}"
                       for i, (a, b, words) in enumerate(cues)) + ("\n" if cues else "")


def _check_output(path: Path, plan: dict, width: int, height: int) -> dict:
    info = inspect_media(path)
    tolerance = max(0.08, 2 / plan["settings"]["fps"])
    checks = [
        {"name": "duration", "passed": abs(info["duration"] - plan["duration"]) <= tolerance,
         "expected": plan["duration"], "actual": info["duration"], "tolerance": tolerance},
        {"name": "dimensions", "passed": info["width"] == width and info["height"] == height,
         "actual": [info["width"], info["height"]]},
        {"name": "delivery_format", "passed": info["video_codec"] == "h264" and info["pixel_format"] == "yuv420p"},
        {"name": "audio_presence", "passed": info["has_audio"] == plan["source_info"]["has_audio"]},
    ]
    if info["has_audio"]:
        checks.append({"name": "audio_format", "passed": info["audio_codec"] == "aac" and info["audio_sample_rate"] == 48000})
        checks.append({"name": "audio_video_duration", "passed": abs(info["audio_duration"] - info["duration"]) <= tolerance + 0.1,
                       "audio": info["audio_duration"], "video": info["duration"]})
    try:
        _run([_tool("ffmpeg"), "-hide_banner", "-nostdin", "-v", "error", "-xerror",
              *INPUT_OPTIONS, "-i", str(path), "-f", "null", "-"], max(180, info["duration"] * 3))
        checks.append({"name": "full_decode", "passed": True})
    except MediaError as exc:
        checks.append({"name": "full_decode", "passed": False, "detail": str(exc)})
    return {"passed": all(item["passed"] for item in checks), "checks": checks,
            "warnings": ["技术检查不能替代观看成片：语义、节奏、口型、画面构图和字幕准确性须人工复核。"]}


def render_plan(plan: dict, output_path: str | Path, settings: dict | None = None) -> dict:
    """Render the explicit edit list to H.264/AAC and fully decode for validation.

    NVENC is opt-in and tested before use; otherwise libx264 CRF 18 is the default.
    SDR inputs are fit inside the frame without cropping people or product detail.
    Supplied subtitles are retimed into an SRT sidecar, never invented or burnt in.
    """
    cfg = _settings({**plan.get("settings", {}), **(settings or {})})
    source = Path(plan["source"]).resolve()
    destination = Path(output_path).resolve()
    if source == destination:
        raise MediaError("Output must not overwrite source media")
    # Reprobe and validate ranges, so an edited plan cannot escape media bounds.
    fresh = inspect_media(source)
    prior_info = plan.get("source_info", {})
    if any(prior_info.get(key) is not None and prior_info[key] != fresh[key]
           for key in ("size_bytes", "modified_ns")):
        raise MediaError("Source changed after planning; rebuild the edit plan")
    if fresh["color_transfer"] in ("smpte2084", "arib-std-b67"):
        raise MediaError("HDR-to-SDR conversion requires an explicit validated color workflow; this source is not rendered")
    if fresh["field_order"] not in (None, "unknown", "progressive"):
        raise MediaError("Interlaced video needs a validated deinterlacing pass before editing")
    if not plan.get("clips") or len(plan["clips"]) > 500:
        raise MediaError("A render must have 1–500 clips")
    clips = _frame_clips([[clip["source_in"], clip["source_out"]] for clip in plan["clips"]], fresh["duration"], cfg["fps"])
    clock = clips[-1]["timeline_end"]
    active_plan = {**plan, "clips": clips, "duration": clock, "source_info": fresh, "settings": cfg}
    subtitle_text = None
    if cfg.get("srt_path"):
        subtitle_source = Path(cfg["srt_path"]).resolve()
        if (not subtitle_source.is_file() or subtitle_source.suffix.lower() != ".srt"
                or subtitle_source.stat().st_size > 10 * 1024 * 1024):
            raise MediaError("Subtitle source must be an existing SRT file smaller than 10 MiB")
        if subtitle_source == destination.with_suffix(".srt"):
            raise MediaError("Retimed subtitles must not overwrite source subtitles")
        try:
            subtitle_text = remap_srt(subtitle_source.read_text(encoding="utf-8-sig"), clips)
        except (OSError, UnicodeError) as exc:
            raise MediaError("Subtitles must be readable UTF-8 SRT") from exc
    width, height = _dimensions(fresh, cfg)
    warnings = list(plan.get("warnings", []))
    if width > fresh["display_width"] or height > fresh["display_height"]:
        warnings.append("输出画布大于部分源片尺寸；增加分辨率不会增加原始细节。")
    encoder = "libx264"
    if cfg["encoder"] == "nvenc":
        # Conservative compatibility guard, not a claim about a universal NVENC
        # hardware minimum. Small preview geometries vary by driver/SDK version.
        if min(width, height) < 256:
            warnings.append("输出尺寸较小，为兼容不同 NVENC 驱动，已使用 CPU 高质量编码；标准高清尺寸可使用 GPU。")
        elif _nvenc_available():
            encoder = "h264_nvenc"
        else:
            warnings.append("NVENC 不可用，已回退至 CPU 高质量编码。")
    n, has_audio = len(clips), fresh["has_audio"]
    graph = [f"[0:v:0]setpts=PTS-STARTPTS,split={n}" + "".join(f"[v{i}]" for i in range(n))]
    if has_audio:
        offset = fresh["audio_start"] - fresh["video_start"]
        alignment = f",adelay={round(offset * 1000)}:all=1" if offset > 0 else (f",atrim=start={-offset:.6f},asetpts=PTS-STARTPTS" if offset < 0 else "")
        graph.append(f"[0:a:0]asetpts=PTS-STARTPTS{alignment},aresample=48000,aformat=channel_layouts=stereo,apad=whole_dur={fresh['duration']:.6f},asplit={n}" + "".join(f"[a{i}]" for i in range(n)))
    for i, clip in enumerate(clips):
        start, end = clip["source_in"], clip["source_out"]
        graph.append(f"[v{i}]trim=start={start:.6f}:end={end:.6f},setpts=PTS-STARTPTS,scale=trunc(iw*sar/2)*2:ih,setsar=1,scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps={cfg['fps']},tpad=stop_mode=clone:stop_duration=1,trim=end_frame={clip['frames']},format=yuv420p[vc{i}]")
        if has_audio:
            graph.append(f"[a{i}]atrim=start={start:.6f}:end={end:.6f},asetpts=PTS-STARTPTS,apad=whole_dur={clip['duration']:.6f},atrim=duration={clip['duration']:.6f}[ac{i}]")
    labels = "".join(f"[vc{i}]" + (f"[ac{i}]" if has_audio else "") for i in range(n))
    graph.append(labels + f"concat=n={n}:v=1:a={int(has_audio)}[video]" + ("[joined_audio]" if has_audio else ""))
    if has_audio:
        normalize = cfg["normalize_audio"]
        peak_db = plan.get("audio_peak_db")
        if normalize and peak_db is not None and peak_db <= -80:
            normalize = False
            warnings.append("音轨近乎无声，已跳过响度增益；请检查源片收音。")
        audio_filter = "loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000" if normalize else "anull"
        graph.append(f"[joined_audio]{audio_filter}[audio]")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".cutflow-render-", dir=destination.parent) as scratch:
        scratch_path = Path(scratch)
        graph_path, staged = scratch_path / "filter.txt", scratch_path / "render.mp4"
        graph_path.write_text(";\n".join(graph), encoding="utf-8")
        args = [_tool("ffmpeg"), "-hide_banner", "-nostdin", "-xerror", "-y", *INPUT_OPTIONS, "-i", str(source),
                "-filter_complex_threads", "2", "-filter_complex_script", str(graph_path), "-map", "[video]"]
        if has_audio:
            args += ["-map", "[audio]", "-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
        args += ["-c:v", encoder]
        args += ["-preset", "p6", "-cq", str(int(cfg["crf"])), "-b:v", "0"] if encoder == "h264_nvenc" else ["-preset", "medium", "-crf", str(int(cfg["crf"])), "-threads", "4"]
        args += ["-pix_fmt", "yuv420p", "-movflags", "+faststart", "-map_metadata", "-1", str(staged)]
        _run(args, max(300, fresh["duration"] * 10))
        qc = _check_output(staged, active_plan, width, height)
        staged.replace(destination)
    metadata = inspect_media(destination)
    report = {"output_path": str(destination), "duration": metadata["duration"],
              "rendered_clips": clips,
              "encoder": encoder, "metadata": metadata, "qc": qc, "warnings": warnings}
    if subtitle_text is not None:
        sidecar = destination.with_suffix(".srt")
        sidecar.write_text(subtitle_text, encoding="utf-8")
        report["srt_path"] = str(sidecar)
        report["warnings"].append("字幕已按时间线重排，未烧录进视频；需复核文字和切点。")
    else:
        report["warnings"].append("尚未提供原片字幕；当前版本不声称已完成语音识别或字幕校对。")
    return report
