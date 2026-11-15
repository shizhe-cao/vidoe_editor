"""LangGraph Agent 状态定义。"""
from __future__ import annotations

from typing import List, Optional, TypedDict

from app.models import CompositionPlan, MediaInfo


class AgentState(TypedDict, total=False):
    """合成 Agent 的图状态。"""

    job_id: str
    # 输入
    video_paths: List[str]
    music_path: Optional[str]
    music_duration: float
    rule_text: str
    rule_is_json: bool
    # 中间产物
    media_infos: List[MediaInfo]
    plan: Optional[CompositionPlan]
    # 渲染
    output_dir: str
    result_path: Optional[str]
    error: Optional[str]
