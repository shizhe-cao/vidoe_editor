"""端到端测试：曲线变速（speed ramp）。

clip2 为 ease_in_out 曲线（0.5~2x），源时长 4s：
  时间轴时长 = ∫ dt/s(u)，介于 4/2=2s 与 4/0.5=8s 之间
总时长 = clip0(5/1=5) + clip1(6/2=3) + clip2(曲线) - 2×0.5s 转场
"""
import json
import sys

from app.agent.graph import run_composition
from app.models import Clip

RULE = {
    "clips": [
        {"video_index": 0, "start": 1, "duration": 5, "speed": 1.0},
        {"video_index": 1, "start": 2, "duration": 6, "speed": 2.0},
        {"video_index": 2, "start": 0, "duration": 4,
         "speed_curve": {"type": "ease_in_out", "low": 0.5, "high": 2.0}},
    ],
    "transition": {"type": "fade", "duration": 0.5},
    "audio": {"loop": True, "volume": 0.8, "fade_in": 1, "fade_out": 2, "keep_original": True},
    "output": {"resolution": [1280, 720], "fps": 30, "duration_policy": "follow_clips"},
}


def main():
    # 先做单元级验证：曲线分段
    curve_clip = Clip(video_index=2, start=0, duration=4,
                      speed_curve={"type": "ease_in_out", "low": 0.5, "high": 2.0})
    segs = curve_clip.speed_segments()
    assert len(segs) == 10, f"应有 10 个子段，实际 {len(segs)}"
    tl = curve_clip.timeline_duration()
    assert 2.0 < tl < 8.0, f"曲线时间轴时长应在 (2, 8) 内，实际 {tl}"
    # 子段源时长守恒
    assert abs(sum(l for _, l, _ in segs) - 4.0) < 1e-6
    # 中间子段速度应高于两端子段（ease_in_out 峰值在中部）
    mid_speed = segs[5][2]
    end_speed = segs[0][2]
    assert mid_speed > end_speed, f"中部速度({mid_speed})应高于起始({end_speed})"
    print(f"分段验证通过: 时间轴 {tl:.3f}s, 起始段 {end_speed:.3f}x, 中段 {mid_speed:.3f}x")

    state = run_composition({
        "job_id": "test-curve",
        "video_paths": [
            "test_assets/v0.mp4",
            "test_assets/v1.mp4",
            "test_assets/v2.mp4",
        ],
        "music_path": "test_assets/music.mp3",
        "rule_text": json.dumps(RULE),
        "rule_is_json": True,
        "output_dir": "outputs/test-curve",
    }, on_progress=lambda d, p: print(f"[{p:3d}%] {d}"))

    if state.get("error"):
        print("FAILED:", state["error"])
        sys.exit(1)

    expected = 5.0 + 3.0 + tl - 2 * 0.5
    print(f"OK result: {state['result_path']} | 预期总时长: {expected:.3f}s (曲线段 {tl:.3f}s)")


if __name__ == "__main__":
    main()
