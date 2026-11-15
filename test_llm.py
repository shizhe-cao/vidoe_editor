"""端到端测试脚本：自然语言规则（经 LLM 规划）。"""
import sys

from app.agent.graph import run_composition


def main():
    state = run_composition({
        "job_id": "test-llm",
        "video_paths": [
            "test_assets/v0.mp4",
            "test_assets/v1.mp4",
            "test_assets/v2.mp4",
        ],
        "music_path": "test_assets/music.mp3",
        "rule_text": "每个视频取中间最精彩的4秒，按顺序拼接，用0.5秒淡入淡出转场，"
                     "背景音乐循环对齐成片时长，结尾2秒淡出，输出720p 25fps。",
        "rule_is_json": False,
        "output_dir": "outputs/test-llm",
    }, on_progress=lambda d, p: print(f"[{p:3d}%] {d}"))

    if state.get("error"):
        print("FAILED:", state["error"])
        sys.exit(1)
    plan = state["plan"]
    print("OK result:", state["result_path"])
    print("reasoning:", plan.reasoning)
    for c in plan.clips:
        print(f"  clip: video={c.video_index} start={c.start} dur={c.duration} speed={c.speed}")
    print("transition:", plan.transition.type.value, plan.transition.duration)
    print("output:", plan.output.resolution, plan.output.fps, plan.output.duration_policy.value)


if __name__ == "__main__":
    main()
