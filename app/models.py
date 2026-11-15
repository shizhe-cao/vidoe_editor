"""合成计划与媒体元数据的 Pydantic 模型。"""
from __future__ import annotations

import math
from enum import Enum
from typing import List, Optional, Tuple
from pydantic import BaseModel, Field, field_validator, model_validator


class MediaInfo(BaseModel):
    """ffprobe 探测出的媒体元数据。"""

    index: int = Field(description="视频在任务中的序号，从 0 开始")
    filename: str
    path: str = Field(default="", description="服务器上的源文件路径")
    duration: float = Field(ge=0, description="时长（秒）")
    width: int = 0
    height: int = 0
    fps: float = 0.0
    has_audio: bool = False
    is_image: bool = Field(default=False, description="是否为静态图片素材（无固定时长，展示时长由计划指定）")


class TransitionType(str, Enum):
    NONE = "none"
    FADE = "fade"
    DISSOLVE = "dissolve"
    WIPE_LEFT = "wipeleft"
    WIPE_RIGHT = "wiperight"
    SLIDE_UP = "slideup"


class SpeedCurveType(str, Enum):
    """曲线变速类型。"""

    RAMP_IN = "ramp_in"        # 慢 → 快
    RAMP_OUT = "ramp_out"      # 快 → 慢
    EASE_IN_OUT = "ease_in_out"  # 慢 → 快 → 慢
    CUSTOM = "custom"          # 任意控制点折线（UI 曲线编辑器产出）


class CurvePoint(BaseModel):
    """曲线控制点：u 为片段内归一化进度 0~1，s 为该处倍速。"""

    u: float = Field(ge=0, le=1)
    s: float = Field(gt=0.1, le=4, description="该点倍速")


class SpeedCurve(BaseModel):
    """曲线变速（speed ramp）：速度随片段进度平滑变化。

    预设类型用 low/high；type=custom 时用 points 折线（首尾 u 自动补齐为 0/1）。
    设置后覆盖 Clip.speed。
    """

    type: SpeedCurveType = SpeedCurveType.EASE_IN_OUT
    low: float = Field(default=0.5, gt=0.1, le=4.0, description="最低倍速")
    high: float = Field(default=2.0, gt=0.1, le=4.0, description="峰值倍速")
    points: Optional[List[CurvePoint]] = Field(
        default=None, description="custom 类型的控制点折线"
    )

    @model_validator(mode="after")
    def _normalize_points(self) -> "SpeedCurve":
        if self.type == SpeedCurveType.CUSTOM:
            if not self.points:
                raise ValueError("custom 曲线必须提供 points 控制点")
            pts = sorted(self.points, key=lambda p: p.u)
            if pts[0].u > 0:
                pts.insert(0, CurvePoint(u=0.0, s=pts[0].s))
            if pts[-1].u < 1:
                pts.append(CurvePoint(u=1.0, s=pts[-1].s))
            object.__setattr__(self, "points", pts)
        return self


def _smoothstep(u: float) -> float:
    return u * u * (3 - 2 * u)


# 曲线变速的分段数与每段的数值积分子步数
CURVE_SEGMENTS = 10
CURVE_SUBSTEPS = 10


class Clip(BaseModel):
    """从某个源视频截取的片段。"""

    video_index: int = Field(ge=0, description="源视频序号")
    start: float = Field(default=0.0, ge=0, description="截取起点（秒）")
    duration: float = Field(gt=0, description="片段时长（秒）")
    speed: float = Field(default=1.0, gt=0, le=4.0, description="播放倍速")
    speed_curve: Optional[SpeedCurve] = Field(
        default=None, description="曲线变速；设置后忽略 speed"
    )

    def _instant_speed(self, u: float) -> float:
        """归一化进度 u∈[0,1] 处的瞬时倍速。"""
        curve = self.speed_curve
        assert curve is not None
        if curve.type == SpeedCurveType.CUSTOM:
            # 控制点折线线性插值
            pts = curve.points or []
            if not pts:
                return 1.0
            if u <= pts[0].u:
                return pts[0].s
            if u >= pts[-1].u:
                return pts[-1].s
            for i in range(len(pts) - 1):
                a, b = pts[i], pts[i + 1]
                if a.u <= u <= b.u:
                    if b.u <= a.u:
                        return b.s
                    t = (u - a.u) / (b.u - a.u)
                    return a.s + (b.s - a.s) * t
        s = _smoothstep(min(1.0, max(0.0, u)))
        if curve.type == SpeedCurveType.RAMP_IN:
            return curve.low + (curve.high - curve.low) * s
        if curve.type == SpeedCurveType.RAMP_OUT:
            return curve.high + (curve.low - curve.high) * s
        # EASE_IN_OUT：两端 low、中间 high
        return curve.low + (curve.high - curve.low) * math.sin(math.pi * u)

    def speed_segments(self) -> List[Tuple[float, float, float]]:
        """将片段切分为恒速子段，返回 [(源起点, 源时长, 恒速)] 列表。

        无曲线时返回单个子段；有曲线时按源时长等分 CURVE_SEGMENTS 段，
        每段倍速取该段瞬时速度的调和平均（保证源/时间轴时长换算准确）。
        """
        if self.speed_curve is None:
            return [(self.start, self.duration, self.speed)]
        seg_len = self.duration / CURVE_SEGMENTS
        segments = []
        for i in range(CURVE_SEGMENTS):
            u0 = i / CURVE_SEGMENTS
            inv_sum = 0.0
            for k in range(CURVE_SUBSTEPS):
                u = u0 + (k + 0.5) / (CURVE_SEGMENTS * CURVE_SUBSTEPS)
                inv_sum += 1.0 / self._instant_speed(u)
            avg_speed = 1.0 / (inv_sum / CURVE_SUBSTEPS)
            segments.append((self.start + i * seg_len, seg_len, avg_speed))
        return segments

    def timeline_duration(self) -> float:
        """片段在成片时间轴上的实际时长（考虑倍速/曲线变速）。"""
        return sum(length / v for _, length, v in self.speed_segments())


class Transition(BaseModel):
    type: TransitionType = TransitionType.FADE
    duration: float = Field(default=0.5, ge=0, le=3.0, description="转场时长（秒）")


class AudioConfig(BaseModel):
    """背景音乐配置。"""

    loop: bool = Field(default=True, description="音乐短于成片时是否循环")
    volume: float = Field(default=0.8, ge=0, le=1, description="背景音乐音量 0~1")
    fade_in: float = Field(default=1.0, ge=0, le=10, description="淡入时长（秒）")
    fade_out: float = Field(default=2.0, ge=0, le=10, description="淡出时长（秒）")
    keep_original: bool = Field(default=False, description="是否保留视频原声混音")


class DurationPolicy(str, Enum):
    """成片总时长策略。"""

    FOLLOW_CLIPS = "follow_clips"  # 跟随片段总时长，音乐截断/循环对齐
    FOLLOW_MUSIC = "follow_music"  # 跟随音乐时长，片段自动拉伸/补齐


class OutputConfig(BaseModel):
    resolution: List[int] = Field(default_factory=lambda: [1920, 1080])
    fps: int = Field(default=30, ge=10, le=60)
    duration_policy: DurationPolicy = DurationPolicy.FOLLOW_CLIPS
    lossless: bool = Field(default=False, description="不压缩：无损画质编码（文件显著增大）")


class CompositionPlan(BaseModel):
    """完整合成计划：LLM 解析规则的产物，也是渲染管线的输入。"""

    clips: List[Clip] = Field(min_length=1)
    transition: Transition = Field(default_factory=Transition)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    reasoning: str = Field(default="", description="LLM 对规则的理解说明（供 UI 展示）")

    @field_validator("transition", "audio", "output", mode="before")
    @classmethod
    def _none_to_default(cls, v, info):
        """容忍 LLM 输出 null（如 "audio": null），自动回退默认值。"""
        if v is None:
            defaults = {"transition": Transition, "audio": AudioConfig, "output": OutputConfig}
            return defaults[info.field_name]()
        return v


class JobStatus(str, Enum):
    QUEUED = "queued"
    PROBING = "probing"
    PLANNING = "planning"
    RENDERING = "rendering"
    DONE = "done"
    FAILED = "failed"


class Job(BaseModel):
    """一次合成任务。"""

    id: str
    status: JobStatus = JobStatus.QUEUED
    stage_detail: str = ""
    progress: int = 0  # 0~100
    video_paths: List[str] = []
    music_path: Optional[str] = None
    music_duration: float = 0.0
    rule_text: str = ""
    rule_is_json: bool = False
    media_infos: List[MediaInfo] = []
    plan: Optional[CompositionPlan] = None
    result_path: Optional[str] = None
    error: Optional[str] = None
