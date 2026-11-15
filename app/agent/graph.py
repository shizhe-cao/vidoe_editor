"""LangGraph 编排：probe → plan → render。

- probe: ffprobe 探测所有输入媒体
- plan:  结构化规则直接解析；自然语言规则调 Qwen（百炼 DashScope）生成合成计划
- render: FFmpeg 渲染管线产出成片

任一节点抛异常时由 run_composition 捕获并写入 state.error。
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from langgraph.graph import END, START, StateGraph

from app.agent import planner
from app.agent.state import AgentState
from app.media.probe import is_image_path, probe_audio_duration, probe_image, probe_video
from app.media.renderer import ProgressCallback, render

logger = logging.getLogger(__name__)


def make_graph(on_progress: Optional[Callable[[str, int], None]] = None):
    """构建合成图。on_progress(detail, percent) 用于向 UI 上报进度。"""

    def probe_node(state: AgentState) -> AgentState:
        if on_progress:
            on_progress("探测媒体元数据", 2)
        # 按扩展名分流：静态图片用 probe_image（无固定时长），其余按视频探测
        infos = [
            probe_image(p, index=i) if is_image_path(p) else probe_video(p, index=i)
            for i, p in enumerate(state["video_paths"])
        ]
        music_duration = 0.0
        if state.get("music_path"):
            music_duration = probe_audio_duration(state["music_path"])
        return {"media_infos": infos, "music_duration": music_duration}

    def plan_node(state: AgentState) -> AgentState:
        if on_progress:
            on_progress("AI 解析合成规则", 5)
        if state["rule_is_json"]:
            plan = planner.parse_structured_rule(
                state["rule_text"], state["media_infos"], state["music_duration"]
            )
        else:
            plan = planner.plan_with_llm(
                state["rule_text"], state["media_infos"], state["music_duration"]
            )
        if on_progress:
            on_progress(f"合成计划就绪: {plan.reasoning[:60]}", 8)
        return {"plan": plan}

    def render_node(state: AgentState) -> AgentState:
        def render_progress(detail: str, pct: int) -> None:
            if on_progress:
                # 渲染阶段映射到 10~100
                on_progress(detail, 10 + int(pct * 0.9))

        result_path = render(
            plan=state["plan"],
            media_infos=state["media_infos"],
            music_path=state.get("music_path"),
            output_dir=state["output_dir"],
            on_progress=render_progress,
        )
        return {"result_path": result_path}

    graph = StateGraph(AgentState)
    graph.add_node("probe", probe_node)
    graph.add_node("plan", plan_node)
    graph.add_node("render", render_node)
    graph.add_edge(START, "probe")
    graph.add_edge("probe", "plan")
    graph.add_edge("plan", "render")
    graph.add_edge("render", END)
    return graph.compile()


def run_composition(state: AgentState,
                    on_progress: Optional[Callable[[str, int], None]] = None) -> AgentState:
    """执行完整合成流程，异常统一捕获为 state.error。"""
    app = make_graph(on_progress)
    try:
        return app.invoke(state)
    except Exception as e:  # noqa: BLE001 - 顶层兜底，错误要回传给 UI
        logger.exception("合成任务失败 job=%s", state.get("job_id"))
        return {**state, "error": f"{type(e).__name__}: {e}"}
