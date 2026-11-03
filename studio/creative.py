"""Explainable, local multi-source composition; no claim of semantic editing.

The brief compiler supports a deliberately small vocabulary. Ordered ranges,
canvas, original sound, an opening title and original synthesized music are
rendered, not merely recorded as prompt metadata. No models or network are used.
"""
from __future__ import annotations

import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any

from . import media


class CreativeError(media.MediaError):
    pass


DEFAULT_CHOICES = {"target_duration": None, "aspect": "original",
                   "keep_original_audio": True, "pace": "preserve",
                   "opening_title": "", "music": False}
ENCODING_KEYS = {"fps", "crf", "encoder", "width", "height", "normalize_audio"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mts"}


def _number(value: str) -> float:
    if re.fullmatch(r"\d+(?:\.\d+)?", value):
        return float(value)
    digits = {c: i for i, c in enumerate("零一二三四五六七八九")}
    digits["两"] = 2
    if "百" in value:
        a, b = value.split("百", 1)
        return (digits.get(a, 1) * 100) + (_number(b.lstrip("零")) if b else 0)
    if "十" in value:
        a, b = value.split("十", 1)
        return digits.get(a, 1) * 10 + digits.get(b, 0)
    return float(digits.get(value, 0))


def _negated(prefix: str) -> bool:
    clause = re.split(r"[，,。；;！!？?\n]", prefix)[-1]
    return bool(re.search(r"(?:不要|不需要|无需|不用|不能|不加|不添加|不使用|不保留|关闭|去掉|移除|禁止|无).{0,12}$", clause))


def _duration_intent(text: str, warnings: list[str]) -> float | None:
    number = r"(\d+(?:\.\d+)?|[零一二两三四五六七八九十百]+)"
    compounds = list(re.finditer(number + r"\s*分(?:钟)?\s*" + number + r"\s*秒", text))
    candidates = [(m.start(), m.end(), _number(m.group(1)) * 60 + _number(m.group(2))) for m in compounds]
    for match in re.finditer(number + r"\s*(秒钟?|分钟|分)(?!镜)", text):
        if not any(m.start() <= match.start() < m.end() for m in compounds):
            candidates.append((match.start(), match.end(), _number(match.group(1)) * (60 if match.group(2).startswith("分") else 1)))
    global_values, ordinary_values = [], []
    for start, end, value in sorted(candidates):
        prefix = text[max(0, start - 22):start]
        suffix = re.split(r"[，,。；;]", text[end:end + 12])[0]
        global_markers = list(re.finditer(r"整片|全片|总时长|成片(?:时长)?|(?:做|制作|生成)(?:成)?(?:一条|一个|一段)?", prefix))
        local_markers = list(re.finditer(r"开头|片头|标题|转场|结尾|片尾|(?:前|后|每段|每个|每条|每份|每一段)\s*$", prefix))
        global_last = global_markers[-1].end() if global_markers else -1
        local_last = local_markers[-1].end() if local_markers else -1
        if global_last > local_last:
            global_values.append(value)
        elif local_last >= 0 or re.search(r"显示标题|(?:的)?片头|(?:的)?转场|(?:的)?片尾|停留", suffix):
            continue
        else:
            ordinary_values.append(value)
    values = global_values or ordinary_values
    if len(set(values)) > 1:
        warnings.append("描述包含多个整体时长，已采用最后一个明确时长；请核对方案或在选项中指定。")
    if not values and candidates:
        warnings.append("检测到局部片段时长，未把它当作整片时长；开头标题目前最多显示 4 秒。")
    return values[-1] if values else None


def interpret_brief(brief: str, options: dict | None = None) -> dict:
    """Compile supported explicit requests. Options override every inferred value."""
    if not isinstance(brief, str) or len(brief) > 4000 or "\x00" in brief:
        raise CreativeError("创作想法须为不超过 4000 字的文字。")
    marker = "【剪辑执行要求】"
    count = brief.count(marker)
    if count > 1:
        raise CreativeError("剪辑执行要求标记只能出现一次，请保留一个明确的执行区块。")
    execution_only = count == 1
    if execution_only:
        execution = brief.split(marker, 1)[1].strip()
        if not execution or len(execution) > 1000:
            raise CreativeError("剪辑执行要求区块须包含 1–1000 字的明确要求。")
        # The caller retains the full original brief. Only this explicitly
        # adopted execution block may control deterministic editing options.
        brief = execution
    if options is not None and not isinstance(options, dict):
        raise CreativeError("创作选项必须为对象。")
    options = options or {}
    unknown = set(options) - set(DEFAULT_CHOICES)
    if unknown:
        raise CreativeError("存在不支持的创作选项。")
    choices = dict(DEFAULT_CHOICES)
    parse_warnings = (["完整创意、分镜和新旁白作为策划保存；本次仅执行末尾【剪辑执行要求】中的可支持项目。"]
                      if execution_only else [])
    title = re.search(r"(?:开头标题|片头标题|标题)(?:\s*(?:用|为|是|写|写成|设为|[:：]))?\s*[“\"「『](.*?)[”\"」』]", brief)
    # Text inside the explicitly requested title is content, not instructions.
    parsed = brief[:title.start()] + brief[title.end():] if title else brief
    if title:
        choices["opening_title"] = title.group(1).strip()
    choices["target_duration"] = _duration_intent(parsed, parse_warnings)
    aspects, previous_end = [], 0
    for match in re.finditer(r"9\s*[:：]\s*16|竖屏|竖版|16\s*[:：]\s*9|横屏|横版|1\s*[:：]\s*1|方形|正方|原比例|原始比例", parsed):
        if not _negated(parsed[previous_end:match.start()]):
            token = match.group()
            aspects.append("9:16" if re.search(r"9\s*[:：]\s*16|竖", token) else "16:9" if re.search(r"16\s*[:：]\s*9|横", token) else "original" if "原" in token else "1:1")
        previous_end = match.end()
    if aspects:
        choices["aspect"] = aspects[-1]
    if len(set(aspects)) > 1 and "aspect" not in options:
        parse_warnings.append("描述包含多个画幅，已采用最后一个未被否定的画幅；请核对方案。")
    if re.search(r"(?:不(?:要|保留)?|去掉|移除|关闭)(?:原声|原音|原始声音)|(?:原声|音轨|原音|整片|全片|视频)静音", parsed) or parsed.strip(" 。！!") == "静音":
        choices["keep_original_audio"] = False
    elif re.search(r"保留(?:原声|原音|原始声音)", parsed):
        choices["keep_original_audio"] = True
    if re.search(r"完整|不剪|不裁|原有节奏", parsed):
        choices["pace"] = "preserve"
    elif re.search(r"紧凑|快节奏|节奏快|加快节奏|清理停顿", parsed):
        choices["pace"] = "compact"
    elif re.search(r"保守|自然节奏|清理长停顿|(?:清理|去掉|删除|消除)静音", parsed):
        choices["pace"] = "gentle"
    music_values, previous_end = [], 0
    for match in re.finditer(r"音乐|配乐", parsed):
        music_values.append(not _negated(parsed[previous_end:match.start()]))
        previous_end = match.end()
    if music_values:
        choices["music"] = music_values[-1]
    if len(set(music_values)) > 1 and "music" not in options:
        parse_warnings.append("描述中音乐要求有变更，已采用最后一次要求；请核对音乐选项。")
    choices.update(options)
    target = choices["target_duration"]
    if target is not None and (isinstance(target, bool) or not isinstance(target, (int, float))
                               or not math.isfinite(target) or not 1 <= target <= 3600):
        raise CreativeError("目标时长须为 1–3600 秒，或留空保留可用时长。")
    if choices["aspect"] not in {"original", "16:9", "9:16", "1:1"}:
        raise CreativeError("画幅选项无效。")
    if choices["pace"] not in {"preserve", "gentle", "compact"}:
        raise CreativeError("节奏选项无效。")
    if any(type(choices[key]) is not bool for key in ("music", "keep_original_audio")):
        raise CreativeError("原声及音乐选项必须为布尔值。")
    label = choices["opening_title"]
    if not isinstance(label, str) or len(label) > 120 or any(ord(c) < 32 for c in label):
        raise CreativeError("开头标题须不超过 120 字，且不包含控制字符。")
    warnings = ["按素材选择顺序制作；未使用语义模型选亮点、判断口误或重写内容。", *parse_warnings]
    if re.search(r"字幕|逐字|转写|识别语音", parsed):
        warnings.append("本次不会自动生成字幕：尚未接入此流程的语音转写及字幕校对。")
    if re.search(r"配音|独白|旁白|改写|重写|换声", parsed):
        warnings.append("本次不会新增或改写配音；原声开关只控制已有音轨。")
    if re.search(r"转场|特效|调色|抠像|去水印|跟踪|卡点|变速|加速|倍速", parsed):
        warnings.append("本次不执行转场特效、语义卡点、调色、去水印或变速；画面按顺序直接切换。")
    if choices["pace"] != "preserve":
        warnings.append("节奏调整仅清理声学静音，不判断句意；需复核是否保留了必要停顿。")
    aspect = {"original": "沿用首段画幅", "16:9": "16:9 横屏", "9:16": "9:16 竖屏", "1:1": "1:1 方形"}[choices["aspect"]]
    pace = {"preserve": "完整保留可用片段", "gentle": "保守清理长停顿", "compact": "清理停顿以收紧节奏"}[choices["pace"]]
    summary = [aspect + "，完整适配画面，不自动裁人脸", pace,
               "保留原声" if choices["keep_original_audio"] else "关闭原声",
               "添加原创轻音乐" if choices["music"] else "不添加音乐"]
    if target is not None:
        summary.append(f"目标 {target:g} 秒，按各素材可用时长分配片头区间")
    if label:
        summary.append("开头叠加标题：" + label)
    return {"version": 1, "resolved": choices, "choices": dict(choices), "summary": summary, "warnings": warnings}


def _encoding(settings: dict | None, choices: dict) -> dict:
    cfg = {key: value for key, value in (settings or {}).items() if key in ENCODING_KEYS}
    cfg.update(target_aspect=choices["aspect"], remove_silence=choices["pace"] != "preserve")
    if choices["pace"] == "compact":
        cfg.update(silence_min=0.5, padding=0.15, min_clip=0.4)
    else:
        cfg.update(silence_min=0.9, padding=0.25, min_clip=0.5)
    return media._settings(cfg)


def _allocation(available: list[int], target: int) -> list[int]:
    """Proportional integer frame budget, retaining at least one frame per source."""
    if target >= sum(available):
        return available[:]
    if target < len(available):
        raise CreativeError("目标时长不足以包含全部素材。")
    result = [1] * len(available)
    budget = target - len(available)
    capacities = [x - 1 for x in available]
    total = sum(capacities)
    exact = [budget * x / total for x in capacities]
    for i, value in enumerate(exact):
        result[i] += math.floor(value)
    for i in sorted(range(len(result)), key=lambda i: exact[i] % 1, reverse=True)[:target - sum(result)]:
        result[i] += 1
    return result


def build_creation_plan(paths: list[str], brief: str = "", options: dict | None = None,
                        settings: dict | None = None) -> dict:
    """Resolve ordered local sources into an explicit frame-aligned edit list."""
    if not isinstance(paths, list) or not 1 <= len(paths) <= 12:
        raise CreativeError("请选择 1–12 段视频素材。")
    intent = interpret_brief(brief, options)
    cfg = _encoding(settings, intent["resolved"])
    sources, plans = [], []
    for value in paths:
        if not isinstance(value, (str, Path)) or "\x00" in str(value):
            raise CreativeError("素材路径无效。")
        path = Path(value).resolve()
        if not path.is_file() or path.suffix.lower() not in VIDEO_SUFFIXES:
            raise CreativeError("素材必须为已存在的本地视频文件。")
        entry = media.build_plan(path, cfg)
        plans.append(entry)
        sources.append({"path": str(path), "info": entry["source_info"], "ctime_ns": path.stat().st_ctime_ns})
    width, height = media._dimensions(sources[0]["info"], cfg)
    cfg.update(width=width, height=height)
    for entry in plans:
        entry["settings"] = dict(cfg)
    available = [sum(c["frames"] for c in p["clips"]) for p in plans]
    requested = intent["resolved"]["target_duration"]
    target = max(len(paths), round(requested * cfg["fps"])) if requested is not None else sum(available)
    budgets = _allocation(available, target)
    warnings = list(intent["warnings"])
    if target < sum(available):
        warnings.append("为接近目标时长，按各素材时长比例保留开头区间；这不是语义亮点选段，切点可能截断句子，请审片。")
    elif requested is not None and target > sum(available):
        warnings.append("可用素材短于目标时长，已使用全部可用内容；未循环素材或改变语速来凑时长。")
    clips, clock = [], 0.0
    for index, (entry, budget) in enumerate(zip(plans, budgets)):
        ranges = []
        for clip in entry["clips"]:
            if budget <= 0:
                break
            take = min(clip["frames"], budget)
            ranges.append([clip["source_in"], min(clip["source_out"], clip["source_in"] + take / cfg["fps"])])
            budget -= take
        selected = media._frame_clips(ranges, entry["source_info"]["duration"], cfg["fps"])
        entry["clips"] = selected
        entry["duration"] = selected[-1]["timeline_end"]
        for clip in selected:
            clips.append({**clip, "source": entry["source"], "source_index": index,
                          "timeline_start": round(clock + clip["timeline_start"], 6),
                          "timeline_end": round(clock + clip["timeline_end"], 6)})
        clock += entry["duration"]
        if not entry["source_info"]["has_audio"]:
            warnings.append(f"素材 {index + 1} 没有音轨，已安排静音音轨以保持音画时长一致。")
        if width > entry["source_info"]["display_width"] or height > entry["source_info"]["display_height"]:
            warnings.append(f"素材 {index + 1} 会适配较大的画布；放大不会增加原始细节。")
    if len(clips) > 500:
        raise CreativeError("片段过多，请拆分为多个项目。")
    return {"version": 1, "kind": "ordered_composition", "brief": brief,
            "resolved": intent["resolved"], "choices": intent["choices"], "summary": intent["summary"],
            "sources": sources, "source_plans": plans, "clips": clips,
            "duration": round(clock, 6), "settings": cfg, "warnings": warnings}


def _unchanged(sources: list[dict]) -> None:
    for entry in sources:
        path = Path(entry["path"])
        if not path.is_file():
            raise CreativeError("素材已移走，请重新创建任务。")
        stat = path.stat()
        info = entry["info"]
        if (stat.st_size != info["size_bytes"] or stat.st_mtime_ns != info["modified_ns"]
                or stat.st_ctime_ns != entry["ctime_ns"]):
            raise CreativeError("素材自生成方案后发生变化，请重新创建任务。")


def _font() -> Path:
    candidates = [os.environ.get("CUTFLOW_CJK_FONT", ""), "C:/Windows/Fonts/msyh.ttc",
                  "C:/Windows/Fonts/simhei.ttf", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                  "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
                  "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate).resolve()
    raise CreativeError("未找到中文标题字体，请安装 Noto Sans CJK 或设置 CUTFLOW_CJK_FONT；本次未静默丢弃标题。")


def _filter_path(path: Path) -> str:
    return path.as_posix().replace("\\", "\\\\").replace(":", "\\:").replace("'", "'\\''")


def _wrapped_title(title: str, width: int, font_size: int) -> str:
    # Chinese glyphs occupy roughly an em; ASCII is narrower. Conservative width.
    limit = max(4, int(width * 0.82 / font_size))
    lines, line, used = [], "", 0.0
    for character in title:
        units = 0.6 if ord(character) < 128 else 1
        if line and used + units > limit:
            lines.append(line)
            line, used = "", 0
        line += character
        used += units
    if line:
        lines.append(line)
    return "\n".join(lines)


def _music_filter(duration: float) -> str:
    # Original synthesized C / Am / F / G triads; no downloaded or sampled music.
    # A soft pulsed pad, not a claim of beat/semantic alignment with the footage.
    chord = "mod(floor(t/4),4)"
    root = f"if(eq({chord},0),130.8128,if(eq({chord},1),110,if(eq({chord},2),174.6141,195.9977)))"
    third = f"if(eq({chord},1),1.189207,1.259921)"
    envelope = "(0.8+0.2*cos(2*PI*1.5*t))*min(1,mod(t,4)/0.08)*min(1,(4-mod(t,4))/0.08)"
    note = f"0.026*{envelope}*(sin(2*PI*({root})*t)+0.5*sin(2*PI*({root})*({third})*t)+0.35*sin(2*PI*({root})*1.498307*t))"
    # Quoting protects the expression's commas from the filtergraph parser.
    return f"aevalsrc='{note}|{note}':s=48000:d={duration:.6f},afade=t=in:d=0.4,afade=t=out:st={max(0, duration - 0.8):.6f}:d=0.8"


def render_creation_plan(plan: dict, output_path: str | Path, settings: dict | None = None) -> dict:
    """Render each source separately, then assemble. Memory is bounded by one source.

    All stages are kept temporary until full final decode passes. Existing audio
    is normalized per source, absent/muted tracks receive silence. Final music
    is ducked by retained audio; title duration is at most four seconds.
    """
    if plan.get("kind") != "ordered_composition" or not 1 <= len(plan.get("sources", [])) <= 12:
        raise CreativeError("多素材生成方案无效。")
    choices = interpret_brief("", plan["resolved"])["resolved"]
    cfg = _encoding({**plan["settings"], **(settings or {})}, choices)
    # Timebase and canvas are part of the reviewed plan, not late render knobs.
    for key in ("fps", "width", "height"):
        if cfg.get(key) != plan["settings"].get(key):
            raise CreativeError("渲染时不能改变已确认的画幅或帧率，请重新生成方案。")
    width, height = cfg["width"], cfg["height"]
    destination = Path(output_path).resolve()
    if destination in [Path(s["path"]).resolve() for s in plan["sources"]]:
        raise CreativeError("成片不能覆盖原素材。")
    _unchanged(plan["sources"])
    if choices["opening_title"]:
        title_font = _font()
        font_size = max(16, round(min(width * 0.052, height * 0.07)))
        while True:
            title_text = _wrapped_title(choices["opening_title"], width, font_size)
            if (title_text.count("\n") + 1) * font_size * 1.35 <= height * 0.7:
                break
            font_size -= 1
            if font_size < 12:
                raise CreativeError("开头标题在当前小画幅中无法清晰显示，请缩短标题或提高输出尺寸。")
    destination.parent.mkdir(parents=True, exist_ok=True)
    warnings, encoders = list(plan["warnings"]), []
    with tempfile.TemporaryDirectory(prefix=".cutflow-creation-", dir=destination.parent) as scratch:
        folder = Path(scratch)
        entries = []
        for index, source_plan in enumerate(plan["source_plans"]):
            _unchanged(plan["sources"])
            normalized = folder / f"normalized-{index:02}.mp4"
            report = media.render_plan(source_plan, normalized, cfg)
            if not report["qc"]["passed"]:
                raise CreativeError(f"第 {index + 1} 段素材未通过技术检查。")
            encoders.append(report["encoder"])
            warnings.extend(w for w in report["warnings"] if "NVENC" in w or "近乎无声" in w)
            # Every concat entry uses identical H.264/AAC stereo layout. Replace
            # audio only; copy video without a second lossy encode at this stage.
            segment = folder / f"segment-{index:02}.mp4"
            if not choices["keep_original_audio"] or not report["metadata"]["has_audio"]:
                media._run([media._tool("ffmpeg"), "-hide_banner", "-nostdin", "-v", "error", "-y",
                            *media.INPUT_OPTIONS, "-i", str(normalized), "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                            "-map", "0:v:0", "-map", "1:a:0", "-t", str(source_plan["duration"]),
                            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", str(segment)],
                           max(180, source_plan["duration"] * 3))
            else:
                normalized.replace(segment)
            entries.extend([f"file '{segment.name}'", f"duration {source_plan['duration']:.6f}"])
        manifest = folder / "concat.txt"
        manifest.write_text("\n".join(entries) + "\n", encoding="utf-8")
        staged = folder / "finished.mp4"
        graph = []
        if choices["music"]:
            graph += ["[0:a:0]aresample=48000,aformat=channel_layouts=stereo,asplit=2[original][detector]",
                      _music_filter(plan["duration"]) + "[music]",
                      "[music][detector]sidechaincompress=threshold=0.02:ratio=6:attack=30:release=400[ducked]",
                      "[original][ducked]amix=inputs=2:normalize=0:duration=first,alimiter=limit=0.95:level=0[audio]"]
        else:
            graph.append("[0:a:0]anull[audio]")
        args = [media._tool("ffmpeg"), "-hide_banner", "-nostdin", "-xerror", "-y", "-protocol_whitelist", "file",
                "-f", "concat", "-safe", "1", "-i", str(manifest)]
        if choices["opening_title"]:
            title_file = folder / "title.txt"
            title_file.write_text(title_text, encoding="utf-8")
            graph.append(f"[0:v:0]drawtext=fontfile='{_filter_path(title_font)}':textfile='{_filter_path(title_file)}':"
                         f"expansion=none:fontsize={font_size}:fontcolor=white:box=1:boxcolor=black@0.66:boxborderw={max(6, font_size//3)}:"
                         f"x=(w-text_w)/2:y=h*0.10:line_spacing={max(4, font_size//4)}:enable='lt(t,{min(4, plan['duration']):.6f})'[video]")
        graph_path = folder / "finish-filter.txt"
        graph_path.write_text(";\n".join(graph), encoding="utf-8")
        args += ["-filter_complex_threads", "2", "-filter_complex_script", str(graph_path),
                 "-map", "[video]" if choices["opening_title"] else "0:v:0", "-map", "[audio]"]
        final_encoder = "copy"
        if choices["opening_title"]:
            final_encoder = "h264_nvenc" if cfg["encoder"] == "nvenc" and min(width, height) >= 256 and media._nvenc_available() else "libx264"
            args += ["-c:v", final_encoder]
            args += ["-preset", "p6", "-cq", str(int(cfg["crf"])), "-b:v", "0"] if final_encoder == "h264_nvenc" else ["-preset", "medium", "-crf", str(int(cfg["crf"])), "-threads", "4"]
            args += ["-pix_fmt", "yuv420p"]
        else:
            args += ["-c:v", "copy"]
        args += ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", "-t", str(plan["duration"]),
                 "-movflags", "+faststart", "-map_metadata", "-1", str(staged)]
        media._run(args, max(300, plan["duration"] * 10))
        qc = media._check_output(staged, {"duration": plan["duration"], "settings": cfg,
                                         "source_info": {"has_audio": True}}, width, height)
        if not qc["passed"]:
            raise CreativeError("合成成片未通过时长、音画或完整解码检查，未替换交付文件。")
        _unchanged(plan["sources"])
        staged.replace(destination)
    metadata = media.inspect_media(destination)
    warnings.append("技术检查不代表内容验收；请观看成片，确认选段、标题和声音符合用途。")
    return {"output_path": str(destination), "duration": metadata["duration"], "metadata": metadata,
            "qc": qc, "warnings": list(dict.fromkeys(warnings)), "encoder": final_encoder if final_encoder != "copy" else encoders[0],
            "segment_encoders": encoders, "rendered_clips": plan["clips"], "resolved": choices,
            "music_source": "程序原创合成 C–Am–F–G 轻音乐，无外部采样" if choices["music"] else None,
            "title_applied": choices["opening_title"] or None}
