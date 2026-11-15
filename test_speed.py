"""端到端测试：逐段快放/慢放。

时间轴预期：
  clip0: 5s/1.0 = 5.0s   clip1: 6s/2.0 = 3.0s   clip2: 2s/0.5 = 4.0s
  两个 fade 0.5s 转场 → 总时长 = 5 + 3 + 4 - 1 = 11.0s
  音频含 0.25x 慢放 → atempo 链 (0.5 x 2)
"""
import json
import sys

from app.agent.graph import run_composition

RULE = {
    "clips": [
        {"video_index": 0, "start": 1, "duration": 5, "speed": 1.0},
        {"video_index": 1, "start": 2, "duration": 6, "speed": 2.0},
        {"video_index": 2, "start": 0, "duration": 2, "speed": 0.5},
    ],
    "transition": {"type": "fade", "duration": 0.5},
    "audio": {"loop": True, "volume": 0.8, "fade_in": 1, "fade_out": 2, "keep_original": True},
    "output": {"resolution": [1280, 720], "fps": 30, "duration_policy": "follow_clips"},
}
EXPECTED = 11.0


def main():
    state = run_composition({
        "job_id": "test-speed",
        "video_paths": [
            "test_assets/v0.mp4",
            "test_assets/v1.mp4",
            "test_assets/v2.mp4",
        ],
        "music_path": "test_assets/music.mp3",
        "rule_text": json.dumps(RULE),
        "rule_is_json": True,
        "output_dir": "outputs/test-speed",
    }, on_progress=lambda d, p: print(f"[{p:3d}%] {d}"))

    if state.get("error"):
        print("FAILED:", state["error"])
        sys.exit(1)
    print("OK result:", state["result_path"], "| 预期时长:", EXPECTED, "s")


if __name__ == "__main__":
    main()
