"""端到端测试脚本：结构化规则（不经 LLM）验证渲染管线。"""
import json
import sys

from app.agent.graph import run_composition

RULE = {
    "clips": [
        {"video_index": 0, "start": 1, "duration": 4},
        {"video_index": 1, "start": 2, "duration": 3},
        {"video_index": 2, "start": 0, "duration": 2, "speed": 2.0},
    ],
    "transition": {"type": "fade", "duration": 0.5},
    "audio": {"loop": True, "volume": 0.8, "fade_in": 1, "fade_out": 2, "keep_original": True},
    "output": {"resolution": [1280, 720], "fps": 30, "duration_policy": "follow_clips"},
}


def main():
    state = run_composition({
        "job_id": "test-structured",
        "video_paths": [
            "test_assets/v0.mp4",
            "test_assets/v1.mp4",
            "test_assets/v2.mp4",
        ],
        "music_path": "test_assets/music.mp3",
        "rule_text": json.dumps(RULE),
        "rule_is_json": True,
        "output_dir": "outputs/test-structured",
    }, on_progress=lambda d, p: print(f"[{p:3d}%] {d}"))

    if state.get("error"):
        print("FAILED:", state["error"])
        sys.exit(1)
    print("OK result:", state["result_path"])


if __name__ == "__main__":
    main()
