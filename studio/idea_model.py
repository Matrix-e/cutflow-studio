"""Offline Chinese idea planning with a bounded, disposable GPU subprocess.

Deploy the official Qwen3-4B-Instruct-2507 safetensors separately. This module
never downloads models or calls a hosted inference API. Parent imports remain
stdlib-only; torch/transformers are imported exclusively inside the worker.
"""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from typing import Any

MODEL_ID = "Qwen/Qwen3-4B-Instruct-2507"
HARD_TIMEOUT = 180.0
MAX_INPUT_TOKENS = 6144
MAX_NEW_TOKENS = 4096
MAX_WIRE_BYTES = 64000
_generation_lock = threading.Lock()


class IdeaModelError(RuntimeError):
    def __init__(self, message: str, code: str = "generation_failed"):
        super().__init__(message)
        self.code = code


ERRORS = {
    "not_configured": "想法模型尚未完成本机部署，请完成模型和独立运行环境配置。",
    "busy": "想法模型正在处理其他请求，请稍后重试。",
    "cancelled": "想法优化已取消，推理进程已结束。",
    "timeout": "想法优化超时，推理进程已结束；请缩短描述后重试。",
    "invalid_output": "模型未返回完整、可靠的结构化策划，请重试或补充具体目标。",
    "gpu_unavailable": "想法模型需要可用的 CUDA GPU，当前未检测到可用设备。",
    "resource_limit": "可用显存或请求长度不足以完成本次优化，请稍后重试或缩短描述。",
    "generation_failed": "本次想法优化未完成，请检查独立模型环境后重试。",
    "invalid_request": "请输入 1–3000 字的想法，素材名称最多 12 个，风格不超过 200 字。",
}


def _error(code: str) -> IdeaModelError:
    safe = code if isinstance(code, str) and code in ERRORS else "generation_failed"
    return IdeaModelError(ERRORS[safe], safe)


def _model_files(path: Path) -> bool:
    try:
        config = path / "config.json"
        if not config.is_file() or config.stat().st_size > 1024 * 1024:
            return False
        if json.loads(config.read_text(encoding="utf-8")).get("model_type") != "qwen3":
            return False
        if not all((path / filename).is_file() for filename in ("tokenizer.json", "tokenizer_config.json")):
            return False
        index = path / "model.safetensors.index.json"
        if index.is_file():
            if index.stat().st_size > 4 * 1024 * 1024:
                return False
            names = set(json.loads(index.read_text(encoding="utf-8"))["weight_map"].values())
        else:
            names = {"model.safetensors"}
        if not names or len(names) > 16:
            return False
        total = 0
        for name in names:
            if not isinstance(name, str) or Path(name).name != name or not name.endswith(".safetensors"):
                return False
            shard = path / name
            if not shard.is_file() or shard.stat().st_size == 0:
                return False
            total += shard.stat().st_size
        return total <= 12 * 1024 ** 3
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def configuration_status() -> dict:
    """Cheap configuration check; never import CUDA, load weights or use network."""
    model_path = os.environ.get("CUTFLOW_IDEA_MODEL_DIR", "")
    python = os.environ.get("CUTFLOW_IDEA_PYTHON", "")
    exists = bool(model_path and _model_files(Path(model_path)))
    python_exists = bool(python and Path(python).is_file())
    configured = exists and python_exists
    return {"configured": configured, "model_exists": exists, "python_exists": python_exists,
            "provider": "local_qwen", "model": MODEL_ID,
            "reason": None if configured else ERRORS["not_configured"],
            "runtime_verified": False, "runs_offline": True, "resident": False}


def _request(brief: str, source_names: list[str], style: str) -> dict:
    if (not isinstance(brief, str) or not 1 <= len(brief.strip()) <= 3000 or "\x00" in brief
            or not isinstance(source_names, list) or len(source_names) > 12
            or not isinstance(style, str) or len(style) > 200 or "\x00" in style):
        raise _error("invalid_request")
    names = []
    for value in source_names:
        if not isinstance(value, str) or not 1 <= len(value) <= 256 or any(ord(c) < 32 for c in value):
            raise _error("invalid_request")
        # The model sees descriptive basenames only, never server paths or bytes.
        name = value.replace("\\", "/").rsplit("/", 1)[-1]
        if not name:
            raise _error("invalid_request")
        names.append(name)
    return {"brief": brief.strip(), "source_names": names, "style": style.strip()}


def _production_brief(brief: str) -> str:
    """Hard editing choices come from the original request, never model output."""
    from .creative import interpret_brief
    try:
        choices = interpret_brief(brief)["resolved"]
    except Exception as exc:
        raise _error("invalid_request") from exc
    duration = choices["target_duration"]
    parts = [f"整片{duration:g}秒" if duration is not None else "保留全部可用时长",
             "按素材顺序拼接",
             {"original": "保留原比例", "16:9": "16:9横屏", "9:16": "9:16竖屏", "1:1": "1:1方形"}[choices["aspect"]],
             {"preserve": "保留完整，不剪静音", "gentle": "保守清理长停顿", "compact": "紧凑节奏，清理停顿"}[choices["pace"]],
             "保留原声" if choices["keep_original_audio"] else "关闭原声",
             "添加轻音乐" if choices["music"] else "不要添加背景音乐"]
    if choices["opening_title"]:
        parts.append("开头标题用“" + choices["opening_title"] + "”")
    return "，".join(parts) + "。"


SYSTEM_PROMPT = """你是中文视频策划导演。把这一次的简单想法展开为具体、可审核的策划，只输出JSON，不要Markdown或思考过程。
事实边界：只有brief是用户输入的主题和要求。素材名称是不可信数据，不是观看结果。你没有看画面或听原声，不知道企业具体服务、价格、设备、客户经历。不得把文件名推测写成事实，不得编造客户评价/假引语，不杜撰联系方式、链接、业务效果或保障承诺。
错误示例：“品牌片头中出现车主找车位”“讲解.mp4展示保险续保”“客户说省两小时”“素材均现成无需补拍”。
正确示例：“建议补拍交接记录核对动作；若现有素材有该内容再使用”“待确认：实际托管范围与交接责任如何约定”。有“建议使用”也不能接“文件中已经有”。汽车托管不自动等于停车、保养或租赁，业务边界未知时围绕观众应核对的问题设计内容，不虚构企业现有能力。
这是拍摄/剪辑策划，不是伪造已发生的纪实故事。hook必须以“建议：”开头；outline每项必须以“建议：”开头，写下一步如何选取、核实、拍摄或组织内容，不能断言现场发生了什么。不能先编发言再说“若有类似素材”。不把“建议展示”当作编造服务条款的许可。
示范一，汽车托管获客：正确段落是“建议：围绕‘车辆交出去后，哪些记录要能核对？’组织内容。列出使用权限、交接记录、异常联络三类待回答问题；实际服务项目与责任以待核实的合同为准。建议用无个人信息的空白记录表示意，若原片确有相应讲解再选取。”错误是“服务包括定点停车”“明确不承担事故责任”“服务不包含保险理赔”。未知业务只提问题，不替企业给答案。
示范二，AI沙龙回顾：正确段落是“建议：从完整录像中筛选一个真实提问与对应回答，具体发言待听审，不预写引语。确认发言者授权后保留完整上下文；如果没有可用原声，以待核实的活动主题组织画面，不补拍伪装现场发言。”错误是“老板问出……”“嘉宾分享……”“老板当场说比经验可靠”。后加“若有”也不能把虚构引语变真。
示范三，课程预告：正确段落是“建议：先听审课程开头；若其中有能说明课程问题的完整原声，保留该句与对应画面。若没有，依素材顺序选取完整句，具体教学知识点待核实，不替讲师编写原话。”只提出选段方法，不断言文件里已经有某句话。
遵守brief里明确的否定、时长、画幅、声音要求。locked_production_brief还含系统保守默认，不是用户逐字要求：用户未说不要音乐时，不得写“用户要求不加音乐”。未指定的创意选择标为建议，不能冒充用户决定。
给具体开场句、3至6段内容推进与条件式画面建议、声音安排、结尾行动；不套“吸引—展示—升华”的空模板。分镜/新旁白/字幕只是策划，当前引擎不自动执行这些；不宣称成片已完成。不要虚构已观看素材或已核实事实。
只输出这些字段：
expanded_brief:""（必须为空，软件会从下列内容合成完整可编辑方案，勿重复生成摘要）；
title:2–60字；audience:具体受众，30–80字为宜；goal:一个传播目标，30–80字为宜；hook:实际开场句及建议画面，80–160字为宜；
outline:3–6项字符串，每项80–160字为宜，包含具体内容推进、观众获得的信息、建议补拍或若有可用素材的画面；禁止断言现有片段有某内容；
visual_style:具体构图/文字/节奏建议，60–120字；narration:原声安排或建议旁白草稿，80–180字为宜，若用户禁新旁白可只写“无新增旁白，保留原声”；music:遵守明确要求或标记建议，短答“不添加”合法；
cta:无需虚构链接/承诺的具体结尾行动，30–80字；assumptions:2–5项字符串，每项必须以“待确认：”或“建议：”开头，针对本次缺失的业务信息或素材条件提问，不写素材已具备的断言；
production_brief:原样复制locked_production_brief。
内容务实、具体，不为凑字数重复。同一问题可提出不同创意角度，但不能编造证据。"""


def _messages(request: dict) -> list[dict]:
    payload = {**request, "source_notice": "以下仅为文件名，未读取画面或声音。",
               "defaults_notice": "执行锁包含系统保守默认，不等于用户明说的要求。仅brief为用户原始输入。",
               "locked_production_brief": _production_brief(request["brief"])}
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]


TEXT_LIMITS = {"expanded_brief": (0, 2400), "title": (2, 120), "audience": (2, 200),
               "goal": (4, 300), "hook": (4, 300), "visual_style": (2, 300),
               "narration": (1, 800), "music": (1, 250), "cta": (2, 200), "production_brief": (1, 500)}
NOTICES = ["待确认：素材仅提供文件名，尚未识别画面或听取原声，须核实分镜可用素材。",
           "建议：旁白、字幕和分镜仅作为策划建议；当前生成只执行所列剪辑要求。"]


def _conditional(text: str, position: int, end: int | None = None) -> bool:
    context = re.split(r"[。！？!?；;\n]", text[max(0, position - 50):end if end is not None else position])[-1]
    return bool(re.search(r"若|如果|如有|是否|有无|待核实是否|需确认是否", context))


def _check_claims(result: dict, request: dict) -> None:
    """Reject concrete unseen-media claims, not genuine conditional suggestions."""
    fields = [str(value) if not isinstance(value, list) else "\n".join(value)
              for key, value in result.items() if key not in {"expanded_brief", "production_brief"}]
    source_patterns = [r"(?:素材|文件|视频|原片|片头|片尾)(?:中|里|内)(?:已|已经)?(?:有|包含|出现|呈现|展示|显示|记录|可见)",
                       r"(?:素材|文件|视频|原片|片头|片尾).{0,12}(?:均包含|均有|已有|已经包含|已清晰呈现)",
                       r"(?:已看过|已经观看|已观看|从素材可见|原声均为可听|无需(?:额外)?补拍)",
                       r"(?:公司|服务|托管).{0,16}(?:具备|拥有|已经提供|确保|保证)",
                       r"(?:核心服务(?:内容)?|服务范围|服务内容)[：:，,]?(?:包括|包含|为|是)",
                       r"(?:服务)?不包含(?:保险理赔|非计划性维修|驾驶行为监管)",
                       r"不承担(?:事故|任何).{0,5}责任"]
    for text in fields:
        for pattern in source_patterns:
            for match in re.finditer(pattern, text):
                if not _conditional(text, match.start(), match.end()) and match.group() not in request["brief"]:
                    raise _error("invalid_output")
        for name in request["source_names"]:
            for match in re.finditer(re.escape(name) + r"[’'”\"）)]*.{0,35}(?:中(?:的)?画面|中(?:已)?(?:有|出现|包含)|展示|显示|呈现)", text):
                if not _conditional(text, match.start(), match.end()):
                    raise _error("invalid_output")
        # Quotes by invented customers are not made safe by calling them examples.
        quote_patterns = [
            r"(?:车主|客户|用户|学员|老板|嘉宾|讲师|主持人|观众)[^\n。！？；;，,]{0,20}(?:表示|说道?|问出|问道|提出|提问|分享|反馈|评价)(?:了|道)?[：:，,\s]*[“‘\"]([^”’\"]{3,200})[”’\"]",
            r"(?:车主|客户|用户|学员)[^\n。！？；;，,]{0,20}反馈的对话场景[，,\s]*(?:例如|比如)[：:\s]*[“‘\"]([^”’\"]{3,200})[”’\"]",
        ]
        for pattern in quote_patterns:
            for match in re.finditer(pattern, text):
                if match.group(1) not in request["brief"]:
                    raise _error("invalid_output")
    for field, relevant in (("music", r"音乐|配乐|BGM"), ("narration", r"旁白|配音|原声")):
        if re.search(r"用户.{0,4}(?:要求|明确|指定)", result[field]) and not re.search(relevant, request["brief"], re.I):
            raise _error("invalid_output")


def _compose_expanded(result: dict) -> str:
    sections = [("视频主题", result["title"]), ("目标受众", result["audience"]),
                ("传播目标", result["goal"]), ("开场设计", result["hook"])]
    sections += [(f"内容段落 {i + 1}", value) for i, value in enumerate(result["outline"])]
    sections += [("视觉与节奏", result["visual_style"]), ("原声与旁白策划", result["narration"]),
                 ("音乐安排", result["music"]), ("结尾行动", result["cta"])]
    sections += [("制作前核实", value) for value in result["assumptions"]]
    output = "\n\n".join(label + "：" + value.strip() for label, value in sections)
    # Never shorten by dropping a later qualification such as "if verified".
    # The bounded quality revision can rewrite an overlong draft; otherwise the
    # request explicitly fails instead of silently losing meaning or constraints.
    if len(output) > 2400:
        raise _error("invalid_output")
    return output


def _validated_result(value: Any, request: dict) -> dict:
    if not isinstance(value, dict) or set(value) != {*TEXT_LIMITS, "outline", "assumptions"}:
        raise _error("invalid_output")
    result = {}
    for name, (minimum, maximum) in TEXT_LIMITS.items():
        text = value.get(name)
        if not isinstance(text, str) or not minimum <= len(text.strip()) <= maximum or "\x00" in text:
            raise _error("invalid_output")
        result[name] = text.strip()
    for name, minimum, maximum in (("outline", 3, 6), ("assumptions", 0, 10)):
        items = value.get(name)
        if (not isinstance(items, list) or not minimum <= len(items) <= maximum
                or any(not isinstance(x, str) or not 4 <= len(x.strip()) <= 250 or "\x00" in x for x in items)):
            raise _error("invalid_output")
        result[name] = list(dict.fromkeys(x.strip() for x in items))
        if len(result[name]) < minimum:
            raise _error("invalid_output")
    if any(not re.match(r"^(?:待确认|建议)[:：]", item) for item in result["assumptions"]):
        raise _error("invalid_output")
    if len(set("".join(result["outline"]))) < 25:
        raise _error("invalid_output")
    _check_claims(result, request)
    body = " ".join(str(v) for k, v in result.items() if k != "production_brief")
    # Guard common invented commercial claims; this does not constitute factual
    # verification. Remaining assumptions and review are still shown to users.
    claims = re.findall(r"\d+(?:\.\d+)?\s*(?:万元|亿元|元|%|％)|1[3-9]\d{9}|\d{4}年\d{1,2}月\d{1,2}日|保证收益|稳赚|零风险|收益翻倍|行业第一", body)
    if any(claim not in request["brief"] for claim in claims):
        raise _error("invalid_output")
    result["production_brief"] = _production_brief(request["brief"])
    result["assumptions"] = list(dict.fromkeys(result["assumptions"] + NOTICES))
    if len(result["assumptions"]) > 10:
        raise _error("invalid_output")
    result["expanded_brief"] = _compose_expanded(result)
    return result


def _parse_output(text: str, request: dict) -> dict:
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_WIRE_BYTES:
        raise _error("invalid_output")
    stripped = text.strip()
    if stripped.startswith("```json") and stripped.endswith("```"):
        stripped = stripped[7:-3].strip()
    try:
        result = json.loads(stripped)
    except (ValueError, TypeError) as exc:
        raise _error("invalid_output") from exc
    return _validated_result(result, request)


def _kill(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.kill()
    try:
        process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def generate_idea(brief: str, source_names: list[str], style: str = "", *, stop_event=None) -> dict:
    """Blocking provider; caller may run in its queue thread and supply Event.

    A request always starts one disposable child. Cancel/timeout kills it, so
    model weights and KV cache do not remain in the web process or GPU memory.
    """
    request = _request(brief, source_names, style)
    if stop_event is not None and stop_event.is_set():
        raise _error("cancelled")
    if not configuration_status()["configured"]:
        raise _error("not_configured")
    if not _generation_lock.acquire(blocking=False):
        raise _error("busy")
    process = None
    try:
        env = os.environ.copy()
        env.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_DATASETS_OFFLINE="1",
                   HF_HUB_DISABLE_TELEMETRY="1", TOKENIZERS_PARALLELISM="false", OMP_NUM_THREADS="2",
                   MKL_NUM_THREADS="2", PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        command = [env["CUTFLOW_IDEA_PYTHON"], "-m", "studio.idea_model", "--worker"]
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding="utf-8", errors="replace", env=env,
                                   cwd=Path(__file__).resolve().parent.parent,
                                   creationflags=0x08000000 if os.name == "nt" else 0)
        deadline = time.monotonic() + HARD_TIMEOUT
        payload = json.dumps(request, ensure_ascii=False)
        while True:
            if stop_event is not None and stop_event.is_set():
                _kill(process)
                raise _error("cancelled")
            if time.monotonic() >= deadline:
                _kill(process)
                raise _error("timeout")
            try:
                stdout, _stderr = process.communicate(input=payload, timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                payload = None
        if process.returncode:
            try:
                envelope = json.loads(stdout)
                code = envelope.get("error", "generation_failed")
            except (ValueError, TypeError, AttributeError):
                code = "generation_failed"
            raise _error(code)
        return _parse_output(stdout, request)
    except (OSError, UnicodeError) as exc:
        raise _error("generation_failed") from exc
    finally:
        try:
            if process is not None and process.poll() is None:
                _kill(process)
        finally:
            _generation_lock.release()


def _worker_generate(request: dict) -> dict:
    """Only invoked in the isolated environment; never in the web interpreter."""
    deadline = time.monotonic() + 165
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    if not torch.cuda.is_available():
        raise _error("gpu_unavailable")
    torch.set_num_threads(2)
    free, _total = torch.cuda.mem_get_info(0)
    if free < 10 * 1024 ** 3:
        raise _error("resource_limit")
    torch.cuda.set_per_process_memory_fraction(0.65, 0)
    model_dir = os.environ.get("CUTFLOW_IDEA_MODEL_DIR", "")
    if not model_dir or not _model_files(Path(model_dir)):
        raise _error("not_configured")
    tokenizer = AutoTokenizer.from_pretrained(model_dir, local_files_only=True, trust_remote_code=False)
    prompt = tokenizer.apply_chat_template(_messages(request), tokenize=False, add_generation_prompt=True)
    inputs = tokenizer([prompt], return_tensors="pt")
    if inputs["input_ids"].shape[-1] > MAX_INPUT_TOKENS:
        raise _error("resource_limit")
    dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
    model = AutoModelForCausalLM.from_pretrained(model_dir, local_files_only=True, trust_remote_code=False,
                                               use_safetensors=True, torch_dtype=dtype, device_map={"": "cuda:0"},
                                               attn_implementation="sdpa", low_cpu_mem_usage=True)
    model.eval()
    try:
        def generate_text(messages):
            remaining = deadline - time.monotonic()
            if remaining <= 3:
                raise _error("timeout")
            prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
            batch = tokenizer([prompt], return_tensors="pt")
            if batch["input_ids"].shape[-1] > MAX_INPUT_TOKENS:
                raise _error("resource_limit")
            batch = batch.to("cuda:0")
            with torch.inference_mode():
                tokens = model.generate(**batch, max_new_tokens=MAX_NEW_TOKENS, do_sample=True,
                                        temperature=0.6, top_p=0.9, top_k=20, repetition_penalty=1.08, use_cache=True,
                                        pad_token_id=tokenizer.eos_token_id, max_time=min(145, remaining - 2))
            return tokenizer.decode(tokens[0, batch["input_ids"].shape[-1]:], skip_special_tokens=True)
        return _generate_checked(request, generate_text)
    except torch.cuda.OutOfMemoryError as exc:
        raise _error("resource_limit") from exc
    finally:
        del model, inputs
        torch.cuda.empty_cache()


def _generate_checked(request: dict, generate_text) -> dict:
    """At most one in-process quality revision, using the same loaded model.

    Only invalid model output permits revision. Cancellation, resource failures
    and deadlines are never retried. The worker callback enforces the shared
    token/time limits on both generations; the parent still kills at 180 s.
    """
    messages = _messages(request)
    for attempt in range(2):
        raw = generate_text(messages)
        try:
            return _parse_output(raw, request)
        except IdeaModelError as exc:
            if exc.code != "invalid_output" or attempt:
                raise
            messages = [*messages, {"role": "assistant", "content": raw},
                        {"role": "user", "content":
                         "上稿未通过质量检查。请只修订一次并输出字段完整的JSON，expanded_brief保持空字符串。"
                         "上稿是未核实草稿，不是新增事实来源。删除并重写所有原始brief未提供的数字、报价、成效、服务项目或免责条款。"
                         "不可编写老板/嘉宾/车主/讲师的发言或评价；活动真实原声只能写成待听审提取，不能先造引语再加若有素材。"
                         "不得断言已看过文件或现有素材包含具体镜头。hook和各outline以建议开头，具体范围未知时只设计待回答的问题。"
                         "assumptions每项以待确认：或建议：开头。保留用户硬约束，production_brief原样复制执行锁；不要把默认项冒称用户要求。"
                         "音乐不添加、旁白无等短答合法；各字段会组合成完整方案，合计正文尽量不超过2100字，删减重复表达但保留条件和完整句意。"}]
    raise _error("invalid_output")


def _main() -> int:
    worker = sys.argv[1:] == ["--worker"]
    if sys.argv[1:] == ["--status"]:
        print(json.dumps(configuration_status(), ensure_ascii=False))
        return 0
    if not worker and sys.argv[1:]:
        print(json.dumps({"error": "invalid_request"}))
        return 2
    try:
        raw = sys.stdin.buffer.read(MAX_WIRE_BYTES + 1)
        if len(raw) > MAX_WIRE_BYTES:
            raise _error("invalid_request")
        values = json.loads(raw.decode("utf-8"))
        if not isinstance(values, dict):
            raise _error("invalid_request")
        request = _request(values.get("brief"), values.get("source_names", []), values.get("style", ""))
        if worker:
            # Library startup messages cannot contaminate the JSON-only protocol.
            with contextlib.redirect_stdout(sys.stderr):
                result = _worker_generate(request)
        else:
            result = generate_idea(**request)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except IdeaModelError as exc:
        print(json.dumps({"error": exc.code, "detail": str(exc)}, ensure_ascii=False))
        return 2
    except Exception as exc:
        # No stack traces, local paths, input prompts, or provider raw output.
        code = "resource_limit" if type(exc).__name__ == "OutOfMemoryError" else "generation_failed"
        print(json.dumps({"error": code, "detail": ERRORS[code]}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(_main())
