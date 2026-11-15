"""规则解析器：自然语言规则 → CompositionPlan。

调用阿里云百炼 DashScope 的 Qwen 模型，以 JSON 模式输出结构化合成计划，
并内置「校验失败带错误重试」回环。
"""
from __future__ import annotations

import json
import re
from typing import List, Optional

import dashscope
from pydantic import ValidationError

from app import config
from app.models import CompositionPlan, MediaInfo, TransitionType

SYSTEM_PROMPT = """你是一个视频合成规划器。用户会提供：
1. 若干源视频的元数据（序号、时长、分辨率、是否有音轨）
2. 背景音乐时长
3. 合成规则（自然语言描述）

你的任务是输出一个 JSON 格式的合成计划（CompositionPlan），字段如下：
{
  "clips": [                                // 按顺序排列的片段列表
    {"video_index": 0,                      // 源视频序号
     "start": 0.0,                          // 截取起点（秒），start+duration 不得超过该视频时长
     "duration": 3.0,                       // 从源视频截取的时长（秒），必须 > 0
     "speed": 1.0,                          // 恒速倍速 0.25~4：>1 快放、<1 慢放，默认 1；与 speed_curve 二选一
     "speed_curve": null}                   // 可选曲线变速：{"type": "...", "low": 0.5, "high": 2.0} 或 custom 折线 {"type":"custom","points":[{"u":0,"s":0.5},{"u":1,"s":2}]}，设置后忽略 speed；type: ramp_in(慢→快)/ramp_out(快→慢)/ease_in_out(慢→快→慢)/custom(控制点折线)，low=最低 high=峰值，s/u 为倍速与进度(0~1)
  ],
  "transition": {"type": "fade", "duration": 0.5},   // type: none/fade/dissolve/wipeleft/wiperight/slideup
  "audio": {                                // 背景音乐配置（无音乐时忽略）
    "loop": true,                           // 音乐短于成片时是否循环
    "volume": 0.8,                          // 音量 0~1
    "fade_in": 1.0, "fade_out": 2.0,        // 淡入/淡出秒数
    "keep_original": false                  // 是否保留视频原声
  },
  "output": {
    "resolution": [1920, 1080],             // 宽高
    "fps": 30,
    "duration_policy": "follow_clips",      // follow_clips=成片跟随片段总时长; follow_music=跟随音乐时长
    "lossless": false                        // 不压缩/无损画质输出，默认 false；规则说"不压缩/无损/最高画质/不损失画质"时设 true（文件显著增大）
  },
  "reasoning": "对规则的理解和取舍说明（一句话）"
}

规则：
- 支持逐段变速（快放/慢放）：规则里说某段"快放/加速/2倍速"就设 speed>1（如 2.0），说"慢放/慢动作/0.5倍速"就设 speed<1（如 0.5）；每个片段可以各自不同
- 支持曲线变速（speed ramp）：规则里说"先慢后快"用 speed_curve={"type":"ramp_in"}，"由快到慢"用 {"type":"ramp_out"}，"慢-快-慢的节奏感"/"先加速再减速"用 {"type":"ease_in_out"}；low/high 取规则提到的倍速范围（如"从0.5倍慢慢加速到2倍"则 low=0.5, high=2），未提及时用 low=0.5, high=2
- 变速时注意：恒速片段成片时长 = duration/speed，曲线变速片段成片时长介于 duration/high 与 duration/low 之间；若规则给定片段时间轴目标时长 T，恒速取 duration = T*speed（且不超过源视频剩余时长）
- 若规则说"均分/卡点/按音乐时长"，将音乐时长按片段数均分（考虑转场重叠），用 duration_policy=follow_music
- 若规则要求"不压缩/无损/最高画质"，设 output.lossless=true；未提及时保持 false 或省略该字段
- 若规则指定"每个视频取前N秒/中间N秒/最精彩片段"，无法判断精彩程度时取视频中间段落
- 片段的 start + duration 严禁超过对应源视频的 duration；但静态图片素材无时长限制，对应片段 start=0、speed=1（不支持变速与截取），duration 即展示时长，用户未指定时取 3 秒
- 未提及的字段使用默认值；transition/audio/output 不允许为 null，无需求时直接省略该字段，也不要输出 "speed_curve": null 之类的空字段
- 只输出 JSON，不要输出任何其他文字或 markdown 代码块标记"""

MAX_RETRIES = 3


def _extract_json(text: str) -> str:
    """从模型输出中提取 JSON 字符串（容忍 ```json 代码块包裹）。"""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        return fence.group(1).strip()
    brace = re.search(r"\{.*\}", text, re.DOTALL)
    return brace.group(0) if brace else text


def _user_prompt(rule_text: str, media_infos: List[MediaInfo], music_duration: float) -> str:
    lines = ["## 源素材列表"]
    for m in media_infos:
        if m.is_image:
            lines.append(f"- 序号{m.index}: {m.filename} 静态图片 分辨率{m.width}x{m.height}")
        else:
            lines.append(
                f"- 序号{m.index}: {m.filename} 时长{m.duration}s "
                f"分辨率{m.width}x{m.height} fps={m.fps} {'含音轨' if m.has_audio else '无音轨'}"
            )
    if music_duration > 0:
        lines.append(f"\n## 背景音乐\n时长 {music_duration}s")
    else:
        lines.append("\n## 背景音乐\n无")
    lines.append(f"\n## 合成规则\n{rule_text}")
    return "\n".join(lines)


def _call_llm(rule_text: str, media_infos: List[MediaInfo], music_duration: float,
              fix_errors: Optional[str] = None) -> str:
    content = _user_prompt(rule_text, media_infos, music_duration)
    if fix_errors:
        content += (
            f"\n\n## 上一次输出的校验错误（必须修正后重新输出完整 JSON）\n{fix_errors}"
        )
    response = dashscope.Generation.call(
        model=config.PLANNER_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
        result_format="message",
        temperature=0.2,
    )
    if response.status_code != 200:
        raise RuntimeError(f"DashScope 调用失败: code={response.status_code} {response.message}")
    return response.output.choices[0].message.content


def _validate_plan(plan: CompositionPlan, media_infos: List[MediaInfo]) -> Optional[str]:
    """业务校验（Pydantic 之外的），返回错误描述或 None。"""
    errors = []
    by_index = {m.index: m for m in media_infos}
    for i, clip in enumerate(plan.clips):
        if clip.video_index not in by_index:
            errors.append(f"片段{i}: video_index={clip.video_index} 不存在")
            continue
        media = by_index[clip.video_index]
        if not media.is_image and clip.start + clip.duration > media.duration + 0.01:
            errors.append(
                f"片段{i}: start({clip.start})+duration({clip.duration}) 超过视频{clip.video_index}"
                f"时长({media.duration})"
            )
    if plan.transition.duration > 0:
        for i, clip in enumerate(plan.clips):
            timeline_dur = clip.timeline_duration()
            if timeline_dur <= plan.transition.duration:
                errors.append(
                    f"片段{i}: 倍速后成片时长({timeline_dur:.2f}s)必须大于转场时长"
                    f"({plan.transition.duration}s)，请缩短转场或加长片段"
                )
                break
    # 变速后时间轴总时长不能为负或过短（与 renderer.total_duration 口径一致）
    n = len(plan.clips)
    total = sum(c.timeline_duration() for c in plan.clips)
    if n > 1 and plan.transition.type != TransitionType.NONE and plan.transition.duration > 0:
        total -= (n - 1) * plan.transition.duration
    if total <= 0.5:
        errors.append(f"变速后成片总时长过短({total:.2f}s)，请调整片段或转场")
    return "\n".join(errors) if errors else None


def parse_structured_rule(rule_text: str, media_infos: List[MediaInfo],
                          music_duration: float) -> CompositionPlan:
    """结构化 JSON 规则：直接解析 + 业务校验。"""
    data = json.loads(rule_text)
    plan = CompositionPlan.model_validate(data)
    errors = _validate_plan(plan, media_infos)
    if errors:
        raise ValueError(f"结构化规则校验失败: {errors}")
    return plan


def plan_with_llm(rule_text: str, media_infos: List[MediaInfo],
                  music_duration: float) -> CompositionPlan:
    """自然语言规则：调 Qwen 生成计划，校验失败带错误重试。"""
    errors = None
    last_raw = ""
    for _ in range(MAX_RETRIES):
        raw = _call_llm(rule_text, media_infos, music_duration, fix_errors=errors)
        last_raw = raw
        try:
            plan = CompositionPlan.model_validate_json(_extract_json(raw))
        except ValidationError as e:
            errors = f"JSON 结构不符合 Schema:\n{str(e)[:800]}"
            continue
        errors = _validate_plan(plan, media_infos)
        if errors is None:
            return plan
    raise ValueError(
        f"LLM 经过 {MAX_RETRIES} 次尝试仍无法生成合法计划，最后一次输出:\n{last_raw[:500]}\n错误:\n{errors}"
    )
