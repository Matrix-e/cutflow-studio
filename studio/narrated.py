"""Multi-source video driven by complete, measured narration audio.

Ordered visual assembly is explicit. No semantic scene recognition, invented
word timing, or silent visual looping. All files remain local to the job.
"""
from __future__ import annotations

import math
import hashlib
from pathlib import Path
import tempfile

from . import creative, media, narration


class NarratedError(media.MediaError):
    pass


def _options(options):
    options = options or {}
    result = {"aspect": options.get("aspect", "original"), "opening_title": options.get("opening_title", ""),
              "music": options.get("music", False), "visual_fill": options.get("visual_fill", "reject"),
              "subtitles": options.get("subtitles", True), "voice_style": options.get("voice_style", "professional"),
              "source_audio": options.get("source_audio", "mute")}
    if result["visual_fill"] not in ("reject", "loop") or result["source_audio"] not in ("mute", "low"):
        raise NarratedError("画面不足策略或原素材声音选项无效。")
    if type(result["music"]) is not bool or type(result["subtitles"]) is not bool:
        raise NarratedError("音乐和字幕选项须为布尔值。")
    if result["voice_style"] not in narration.STYLES:
        raise NarratedError("旁白语气选项无效。")
    creative.interpret_brief("", {k: result[k] for k in ("aspect", "opening_title", "music")})
    return result


def _visual_timeline(base, frames):
    clips, clock, cycle = [], 0, 0
    fps = base["settings"]["fps"]
    while clock < frames:
        for clip in base["clips"]:
            take = min(clip["frames"], frames - clock)
            if take <= 0:
                break
            clips.append({**clip, "source_out": min(clip["source_out"], clip["source_in"] + take / fps),
                          "frames": take, "duration": take / fps, "timeline_start": clock / fps,
                          "timeline_end": (clock + take) / fps, "visual_cycle": cycle})
            clock += take
            if len(clips) > 500:
                raise NarratedError("画面过短，循环次数过多，请增加素材。")
        cycle += 1
    return clips


def build_narrated_plan(paths: list[str], spoken_script: str, voice_id: str, options: dict | None = None,
                        settings: dict | None = None, *, work_dir: str | Path, stop_event=None,
                        prepared_narration: dict | None = None, on_stage=None) -> dict:
    """Synthesize first; the resulting actual speech duration determines video."""
    opts = _options(options)
    narration._validate(spoken_script, voice_id, opts["voice_style"])
    work = Path(work_dir).resolve()
    work.mkdir(parents=True, exist_ok=True)
    cfg = {**(settings or {}), "normalize_audio": False}
    base_options = {"aspect": opts["aspect"], "opening_title": "", "music": False,
                    "keep_original_audio": opts["source_audio"] == "low", "pace": "preserve"}
    # Validate all video inputs before spending GPU time on synthesis.
    base = creative.build_creation_plan(paths, "", base_options, cfg)
    if prepared_narration is None:
        if on_stage is not None:
            on_stage("preparing_voice")
        audio = narration.synthesize(spoken_script, voice_id, work / "narration.wav", style=opts["voice_style"], stop_event=stop_event)
    else:
        try:
            audio = dict(prepared_narration)
            expected_script = hashlib.sha256(spoken_script.strip().encode("utf-8")).hexdigest()
            audio_file, subtitle_file = Path(audio["output_path"]), Path(audio["srt_path"])
            valid = (audio.get("voice_id") == voice_id and audio.get("style") == opts["voice_style"]
                     and audio.get("script_sha256") == expected_script and audio.get("qc", {}).get("passed") is True
                     and narration._sha(audio_file) == audio["sha256"] and narration._sha(subtitle_file) == audio["srt_sha256"])
            measured = narration.inspect_audio(audio_file)
            valid = valid and measured["qc"]["passed"] and math.isfinite(audio["duration"]) and abs(measured["duration"] - audio["duration"]) <= 1 / measured["sample_rate"]
            if not valid:
                raise ValueError("mismatch")
        except (KeyError, TypeError, ValueError, OSError, narration.NarrationError) as exc:
            raise NarratedError("已有旁白与讲稿、声音或文件记录不一致，请重新生成。") from exc
    if not audio.get("qc", {}).get("passed"):
        raise NarratedError("旁白音频未通过检查。")
    if on_stage is not None:
        on_stage("planning_visuals")
    creative._unchanged(base["sources"])
    fps = base["settings"]["fps"]
    frames = math.ceil(audio["duration"] * fps - 1e-8)
    duration = frames / fps
    loop = base["duration"] + 1e-6 < duration
    if loop and opts["visual_fill"] != "loop":
        raise NarratedError(f"画面总时长 {base['duration']:.1f} 秒，短于完整独白 {audio['duration']:.1f} 秒。请增加素材，或明确选择循环画面；未截断独白。")
    warnings = ["成片时长跟随完整独白音频；目标时长仅用于策划估算，不截断讲稿或加速语音。",
                "当前画面按选择顺序组织，不声称已理解素材或完成语义选镜头。", *audio.get("warnings", [])]
    if loop:
        warnings.append("已按明确选择循环现有画面，重复画面会出现在成片中，请审片确认。")
    else:
        # Keep every selected source's contribution when enough footage exists.
        base = creative.build_creation_plan(paths, "", {**base_options, "target_duration": duration}, cfg)
        warnings.append("画面按各素材时长比例取开头区间以覆盖独白；不是自动匹配讲稿语义。")
    if opts["source_audio"] == "low":
        warnings.append("原素材声音以较低音量混入，并在旁白时压低；请试听是否干扰独白。")
    return {"version": 1, "kind": "narrated_composition", "spoken_script": spoken_script,
            "voice_id": voice_id, "options": opts, "narration": audio, "visual_plan": base,
            "duration": duration, "narration_duration": audio["duration"], "settings": base["settings"],
            "loop_visuals": loop, "clips": _visual_timeline(base, frames), "warnings": warnings}


def _subtitle_filter(path: Path, width: int, height: int, ass_path: Path) -> str:
    import html
    import re
    from PIL import ImageFont
    font = creative._font()
    family = "Microsoft YaHei" if font.name.lower().startswith("msyh") else ("SimHei" if "simhei" in font.name.lower() else "Noto Sans CJK SC")
    # Explicit output coordinates and hard line breaks avoid libass builds that
    # cannot wrap Chinese SRT text. Keep at most two lines inside safe margins.
    size = max(12, round(min(width * .048, height * .052)))
    margin, bottom = max(8, round(width * .07)), max(8, round(height * .09))
    measure = ImageFont.truetype(str(font), size)
    usable = width - 2 * margin - size * .5
    def stamp(value):
        cs = round(value * 100)
        return f"{cs//360000}:{cs//6000%60:02}:{cs//100%60:02}.{cs%100:02}"
    header = (f"[Script Info]\nScriptType: v4.00+\nPlayResX: {width}\nPlayResY: {height}\nWrapStyle: 2\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{family},{size},&H00FFFFFF,&H00FFFFFF,&H00101010,&H80000000,0,0,0,0,100,100,0,0,1,{max(1,round(size*.06))},0,2,{margin},{margin},{bottom},1\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    events = []
    for block in re.split(r"\n\s*\n", path.read_text(encoding="utf-8-sig").strip()):
        rows = block.splitlines()
        if len(rows) < 3 or " --> " not in rows[1]:
            raise NarratedError("字幕格式无效，未输出缺字成片。")
        start, end = (media._subtitle_time(v.strip()) for v in rows[1].split(" --> "))
        text = html.unescape(" ".join(rows[2:])).replace("{", "｛").replace("}", "｝").replace("\\", "＼")
        lines, current = [], ""
        for char in text:
            if current and measure.getlength(current + char) > usable:
                lines.append(current); current = ""
            current += char
        if current: lines.append(current)
        total, consumed = max(1, sum(map(len, lines))), 0
        for index in range(0, len(lines), 2):
            group = lines[index:index+2]
            if len(group) == 2 and measure.getlength(group[1]) < usable * .35:
                combined = "".join(group)
                splits = [(abs(measure.getlength(combined[:k])-measure.getlength(combined[k:])), k)
                          for k in range(1, len(combined))
                          if combined[k] not in "，。！？、；：,.!?;:" and
                          max(measure.getlength(combined[:k]), measure.getlength(combined[k:])) <= usable]
                if splits:
                    split = min(splits)[1]
                    group = [combined[:split], combined[split:]]
            begin = start + (end-start) * consumed / total
            consumed += sum(map(len, group))
            finish = start + (end-start) * consumed / total
            # Long sentences use proportional cue timing, not word alignment.
            events.append(f"Dialogue: 0,{stamp(begin)},{stamp(finish)},Default,,0,0,0,," + r"\N".join(group))
    ass_path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return f"ass=filename='{creative._filter_path(ass_path)}':fontsdir='{creative._filter_path(font.parent)}'"


def render_narrated_plan(plan: dict, output_path: str | Path, settings: dict | None = None, *, on_stage=None) -> dict:
    if plan.get("kind") != "narrated_composition":
        raise NarratedError("独白视频方案无效。")
    destination = Path(output_path).resolve()
    voice = Path(plan["narration"]["output_path"]).resolve()
    srt = Path(plan["narration"]["srt_path"]).resolve()
    if (not voice.is_file() or narration._sha(voice) != plan["narration"]["sha256"]
            or not srt.is_file() or narration._sha(srt) != plan["narration"]["srt_sha256"]):
        raise NarratedError("旁白或字幕在生成方案后已改变，请重新创建任务。")
    if destination in {voice, srt, *(Path(s["path"]).resolve() for s in plan["visual_plan"]["sources"])}:
        raise NarratedError("成片不能覆盖源素材或旁白。")
    opts = _options(plan["options"])
    cfg = creative._encoding({**plan["settings"], **(settings or {})}, plan["visual_plan"]["resolved"])
    for key in ("fps", "width", "height"):
        if cfg.get(key) != plan["settings"].get(key):
            raise NarratedError("画幅或帧率改变后须重新生成方案。")
    width, height, duration = cfg["width"], cfg["height"], plan["duration"]
    if duration + 1e-6 < plan["narration_duration"]:
        raise NarratedError("成片时长不能短于完整旁白。")
    warnings = list(plan["warnings"])
    if on_stage is not None:
        on_stage("compositing")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".cutflow-narrated-", dir=destination.parent) as scratch:
        folder = Path(scratch)
        subtitle_filter = _subtitle_filter(srt, width, height, folder / "captions.ass") if opts["subtitles"] else None
        visual = folder / "visual.mp4"
        visual_report = creative.render_creation_plan(plan["visual_plan"], visual, settings)
        if not visual_report["qc"]["passed"]:
            raise NarratedError("画面未通过技术检查。")
        args = [media._tool("ffmpeg"), "-hide_banner", "-nostdin", "-xerror", "-y"]
        if plan["loop_visuals"]:
            args += ["-stream_loop", "-1"]
        args += [*media.INPUT_OPTIONS, "-i", str(visual), "-protocol_whitelist", "file", "-i", str(voice)]
        graph = [f"[1:a:0]aresample=48000,aformat=channel_layouts=stereo,loudnorm=I=-16:TP=-1.5:LRA=7,aresample=48000,apad=whole_dur={duration:.6f},atrim=duration={duration:.6f}[voice]"]
        beds = []
        if opts["source_audio"] == "low":
            graph.append(f"[0:a:0]volume=0.12,apad=whole_dur={duration:.6f},atrim=duration={duration:.6f}[sourcebed]")
            beds.append("[sourcebed]")
        if opts["music"]:
            graph.append(creative._music_filter(duration) + "[musicbed]")
            beds.append("[musicbed]")
        if beds:
            graph += ["[voice]asplit=2[spoken][detector]",
                      "".join(beds) + (f"amix=inputs={len(beds)}:normalize=0:duration=longest[bed]" if len(beds) > 1 else "anull[bed]"),
                      "[bed][detector]sidechaincompress=threshold=0.02:ratio=6:attack=30:release=400[ducked]",
                      f"[spoken][ducked]amix=inputs=2:normalize=0:duration=first,apad=pad_dur=0.01,alimiter=limit=0.95:level=0:attack=5,atrim=start=0.005:duration={duration:.6f},asetpts=PTS-STARTPTS[audio]"]
        else:
            graph.append("[voice]anull[audio]")
        # Reset timestamps after looping and enforce the planned frame count.
        # Stream-copy can retain a negative initial PTS / lose a tail frame.
        frames = round(duration * cfg["fps"])
        visual_filter = f"setpts=PTS-STARTPTS,fps={cfg['fps']},tpad=stop_mode=clone:stop_duration=1,trim=end_frame={frames},setpts=N/({cfg['fps']}*TB)"
        if opts["opening_title"]:
            title_font = creative._font()
            font_size = max(16, round(min(width * 0.052, height * 0.07)))
            while True:
                title_text = creative._wrapped_title(opts["opening_title"], width, font_size)
                if (title_text.count("\n") + 1) * font_size * 1.35 <= height * 0.7:
                    break
                font_size -= 1
                if font_size < 12:
                    raise NarratedError("开头标题过长，请缩短标题或提高输出尺寸。")
            title_file = folder / "title.txt"
            title_file.write_text(title_text, encoding="utf-8")
            visual_filter += (f",drawtext=fontfile='{creative._filter_path(title_font)}':textfile='{creative._filter_path(title_file)}':"
                              f"expansion=none:fontsize={font_size}:fontcolor=white:box=1:boxcolor=black@0.66:boxborderw={max(6, font_size//3)}:"
                              f"x=(w-text_w)/2:y=h*0.10:line_spacing={max(4, font_size//4)}:enable='lt(t,{min(4, duration):.6f})'")
        graph.append("[0:v:0]" + visual_filter + ("," + subtitle_filter if subtitle_filter else "") + "[video]")
        graph_path = folder / "narrated-filter.txt"
        graph_path.write_text(";\n".join(graph), encoding="utf-8")
        args += ["-filter_complex_threads", "2", "-filter_complex_script", str(graph_path),
                 "-map", "[video]", "-map", "[audio]"]
        encoder = "h264_nvenc" if cfg["encoder"] == "nvenc" and min(width, height) >= 256 and media._nvenc_available() else "libx264"
        args += ["-c:v", encoder]
        args += ["-preset", "p6", "-cq", str(int(cfg["crf"])), "-b:v", "0"] if encoder == "h264_nvenc" else ["-preset", "medium", "-crf", str(int(cfg["crf"])), "-threads", "4"]
        args += ["-pix_fmt", "yuv420p"]
        staged = folder / "finished.mp4"
        args += ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", "-t", f"{duration:.6f}",
                 "-map_metadata", "-1", "-movflags", "+faststart", str(staged)]
        media._run(args, max(300, duration * 10))
        if on_stage is not None:
            on_stage("checking")
        qc = media._check_output(staged, {"duration": duration, "settings": cfg, "source_info": {"has_audio": True}}, width, height)
        metadata = media.inspect_media(staged)
        qc["narration_complete_duration"] = min(metadata["duration"], metadata.get("audio_duration") or 0) + 1e-6 >= plan["narration_duration"]
        qc["checks"].append({"name": "complete_narration_duration", "passed": qc["narration_complete_duration"],
                             "expected": plan["narration_duration"], "actual_audio": metadata.get("audio_duration"),
                             "actual_video": metadata["duration"]})
        qc["passed"] = qc["passed"] and qc["narration_complete_duration"]
        if not qc["passed"]:
            raise NarratedError("独白视频未通过时长、音画或完整解码检查。")
        if narration._sha(voice) != plan["narration"]["sha256"] or narration._sha(srt) != plan["narration"]["srt_sha256"]:
            raise NarratedError("旁白或字幕在渲染期间发生变化。")
        creative._unchanged(plan["visual_plan"]["sources"])
        staged.replace(destination)
    output_srt = destination.with_suffix(".srt")
    output_srt.write_text(srt.read_text(encoding="utf-8"), encoding="utf-8")
    metadata = media.inspect_media(destination)
    qc["speech_content_verified"] = False
    return {"output_path": str(destination), "duration": metadata["duration"], "metadata": metadata,
            "qc": qc, "encoder": encoder if encoder != "copy" else visual_report["encoder"],
            "rendered_clips": plan["clips"], "narration_duration": plan["narration_duration"],
            "narration_path": str(voice), "srt_path": str(output_srt), "subtitles_burned": opts["subtitles"],
            "title_applied": opts["opening_title"] or None,
            "warnings": list(dict.fromkeys(warnings + visual_report.get("warnings", []))),
            "music_source": "程序原创合成轻音乐，无外部采样" if opts["music"] else None}
