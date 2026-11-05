"""Truthful phase snapshots. Stage counts are not time-completion percentages.

queue_position is one-based and includes this queued job. Durations describe the
latest attempt, including its queue wait, and stop at completion/failure/withdrawal.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math

STAGE_LABELS = {
    "planning_story":"策划故事", "captioning":"制作字幕",
    "planning_supplement": "起草衔接内容", "generating_visuals": "生成补充画面",
    "queued": "排队中", "validating": "检查素材", "preparing_voice": "生成旁白",
    "transcribing": "识别原声", "planning_visuals": "编排画面",
    "animating_presenter": "制作人物口型",
    "compositing": "合成视频", "checking": "检查成片", "ready": "制作完成", "failed": "需要处理",
    "withdrawn": "已撤回",
}
STAGE_MESSAGES = {
    "planning_story":"正在结合素材与品牌知识编排故事", "captioning":"正在识别成片语音并制作字幕",
    "planning_supplement": "正在起草片尾衔接内容", "generating_visuals": "正在生成补充画面",
    "queued": "任务已加入队列", "validating": "正在分析原始素材",
    "transcribing": "正在识别素材中的原声内容",
    "preparing_voice": "正在按已确认讲稿生成完整旁白", "animating_presenter": "正在同步人物嘴型",
    "planning_visuals": "正在按完整旁白时长编排画面", "compositing": "正在合成画面、字幕和声音",
    "checking": "正在检查成片音画与完整解码", "ready": "成片检查通过，等待人工试看确认",
    "failed": "剪辑失败", "withdrawn": "用户撤回了尚未开始的排队任务，素材、讲稿和设置已保留",
}
_MESSAGE_STAGES = {message: stage for stage, message in STAGE_MESSAGES.items()}
_MESSAGE_STAGES.update({"用户请求重新剪辑，已排队": "queued", "服务重新启动，已重新排队恢复剪辑": "queued",
                        "分析完成，开始渲染和成片检查": "compositing", "成片检查未通过，禁止发布": "failed"})
_ACTIVE_STAGES = {"transcribing", "planning_story", "captioning","planning_supplement", "generating_visuals","validating", "preparing_voice", "animating_presenter", "planning_visuals", "compositing", "checking"}
_DONE_STATUSES = {"review", "approved", "publishing", "published"}


def _time(value):
    try:
        if isinstance(value, datetime):
            return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            return datetime.fromtimestamp(value, timezone.utc)
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return _time(parsed)
    except (ValueError, TypeError, OverflowError, OSError):
        pass
    return None


def _seconds(begin, end):
    return max(0, int((end - begin).total_seconds())) if begin and end else None


def format_elapsed(seconds):
    """Readable elapsed time, never an estimated remaining time."""
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or not math.isfinite(seconds):
        return "暂无计时"
    value = max(0, int(seconds))
    hours, remainder = divmod(value, 3600)
    minutes, seconds = divmod(remainder, 60)
    return (f"{hours}小时{minutes}分{seconds}秒" if hours else f"{minutes}分{seconds}秒" if minutes else f"{seconds}秒")


def _event_stage(event):
    explicit = event.get("stage") or (event.get("production") or {}).get("stage")
    if explicit in STAGE_LABELS:
        return explicit
    if event.get("message") in _MESSAGE_STAGES:
        return _MESSAGE_STAGES[event["message"]]
    status = event.get("status")
    return ("withdrawn" if status == "withdrawn" else
            "ready" if status in _DONE_STATUSES else "queued" if status == "queued" else
            "failed" if status == "failed" else "validating" if status == "analyzing" else
            "compositing" if status == "rendering" else None)


def progress_snapshot(job, events, queue_position=None, current_time=None):
    """Normalize existing jobs/events without guessing progress from wall time.

    Future workers may put a recognized ``stage`` on events, or use
    STAGE_MESSAGES with the existing event schema. production.last_stage and
    production.stage_started_at are optional; older records remain supported.
    """
    job = job or {}
    creation = (job.get("settings") or {}).get("creation") or {}
    production = job.get("production") or {}
    mode = job.get("mode") or creation.get("mode") or production.get("mode") or "edit"
    narrated = mode in {"talking_head", "narrated_promo", "brand_promo"}
    vlog = mode == "vlog"
    if mode=="story":
        ids=(["validating","compositing","checking"] if creation.get('assembly_plan_id') else
             ["validating","transcribing","preparing_voice","planning_visuals","compositing","checking"] if creation.get('spoken_script') else
             ["validating","transcribing","planning_story","preparing_voice","planning_visuals","compositing","checking"])
    elif vlog:
        opts = creation.get("options") or {}
        ids = ["validating", "planning_visuals"]
        if opts.get("allow_supplemental_visuals") or opts.get("allow_generated_dialogue"):ids += ["planning_supplement"]
        if opts.get("allow_supplemental_visuals"):ids += ["generating_visuals"]
        if opts.get("allow_generated_dialogue"):ids += ["preparing_voice"]
        ids += ["compositing", "checking"]
    else:
        ids = (["validating", "preparing_voice"] + (["animating_presenter"] if mode == "talking_head" else [])
               + ["planning_visuals", "compositing", "checking"] if narrated else ["validating", "compositing", "checking"])
    if mode in {"edit","vlog"} and (creation.get("options") or {}).get("subtitles"):
        ids.insert(ids.index("checking"),"captioning")
    labels = dict(STAGE_LABELS)
    if not narrated:
        labels.update(validating="分析素材", compositing="渲染画面")
    now = _time(current_time) or datetime.now(timezone.utc)
    history = []
    for index, event in enumerate(events or []):
        if not isinstance(event, dict):
            continue
        when = _time(event.get("created_at"))
        if when:
            history.append((when, index, _event_stage(event)))
    history.sort(key=lambda entry: (entry[0], entry[1]))
    # A retry or restart begins a fresh attempt, even if the old plan remains.
    queued_indices = [index for index, entry in enumerate(history) if entry[2] == "queued"]
    if queued_indices:
        history = history[queued_indices[-1]:]
    attempt_start = history[0][0] if history else _time(job.get("created_at"))
    status = job.get("status", "queued")
    recorded_stage = production.get("stage")
    active = [(when, stage) for when, _, stage in history if stage in ids]
    last_stage = active[-1][1] if active else None
    explicit_last = production.get("last_stage")
    if explicit_last in ids and status != "queued":
        last_stage = explicit_last
    if status == "withdrawn":
        stage, last_stage = "withdrawn", None
    elif status == "queued":
        stage, last_stage = "queued", None
    elif status in _DONE_STATUSES:
        stage, last_stage = "ready", "ready"
    elif status == "failed":
        stage = "failed"
        if recorded_stage in ids:
            last_stage = recorded_stage
        if any(entry[2] == "ready" for entry in history):
            last_stage = "ready"  # e.g. a later delivery error, not a rendering failure.
    elif recorded_stage in ids:
        stage = last_stage = recorded_stage
    else:
        stage = last_stage or ("compositing" if status == "rendering" else "validating")
        last_stage = stage
    done = stage == "ready" or (stage == "failed" and last_stage == "ready")
    terminal_stage = "ready" if done else stage if stage in {"failed", "withdrawn"} else None
    terminal_times = [when for when, _, marker in history if marker == terminal_stage] if terminal_stage else []
    terminal_fallback = (_time(production.get("withdrawn_at")) if stage == "withdrawn" else None)
    end = (terminal_times[0] if terminal_times else terminal_fallback or _time(job.get("updated_at")) or now) if terminal_stage else now
    effective_stage = last_stage if stage == "failed" else stage
    starts, previous_stage = [], None
    for when, _, marker in history:
        if marker is None:
            continue
        if marker == effective_stage and marker != previous_stage:
            starts.append(when)
        previous_stage = marker
    stage_start = starts[-1] if starts else attempt_start
    explicit_start = _time(production.get("stage_started_at"))
    if stage not in {"queued", "ready", "failed", "withdrawn"} and explicit_start and (not attempt_start or explicit_start >= attempt_start):
        stage_start = explicit_start
    if stage == "withdrawn":
        stage_start = end
    completed = len(ids) if done else ids.index(effective_stage) if effective_stage in ids else 0
    stages = []
    for index, ident in enumerate(ids):
        state = ("completed" if index < completed else "failed" if stage == "failed" and ident == last_stage
                 else "active" if ident == stage else "pending")
        stages.append({"id": ident, "label": labels[ident], "state": state})
    position = queue_position if status == "queued" and type(queue_position) is int and queue_position >= 1 else None
    error = str(job.get("error") or "")[:1000] or None
    if stage == "withdrawn":
        detail = "已从等待队列撤回，尚未开始这次制作。素材、讲稿和设置已保留，可继续编辑后创建新任务；不会自动重新排队。"
    elif stage == "queued":
        detail = f"等待队列中排第 {position} 位；当前正在制作的任务完成后依次开始。" if position else "任务已排队，等待制作服务领取。"
    elif stage == "ready":
        detail = "成片已通过技术检查，请试看画面、口型、字幕和声音。"
    elif stage == "failed":
        detail = ("成片已完成，后续操作需要处理。" if done else f"制作在“{labels[last_stage]}”阶段停止。" if last_stage else "任务未完成。")
        if error:
            detail += " " + error
    else:
        detail = ("正在按所选素材顺序编排画面" if vlog and stage == 'planning_visuals' else STAGE_MESSAGES[stage]) + "；阶段数量不代表预计耗时百分比。"
    elapsed, stage_elapsed = _seconds(attempt_start, end), _seconds(stage_start, end)
    return {"stage": stage, "label": labels[stage], "detail": detail, "stages": stages,
            "completed_count": completed, "total_count": len(ids), "elapsed_seconds": elapsed,
            "stage_elapsed_seconds": stage_elapsed, "elapsed_label": format_elapsed(elapsed),
            "stage_elapsed_label": format_elapsed(stage_elapsed), "queue_position": position,
            "last_stage": last_stage, "error": error if stage == "failed" else None,
            "attempt_started_at": attempt_start.isoformat() if attempt_start else None,
            "updated_at": job.get("updated_at"), "eta_seconds": None}
