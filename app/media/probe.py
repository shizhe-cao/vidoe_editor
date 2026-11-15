"""ffprobe 封装：探测视频/音频元数据。"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from app.models import MediaInfo

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}


def is_image_path(path: str) -> bool:
    """按扩展名判断是否为静态图片素材。"""
    return Path(path).suffix.lower() in IMAGE_EXT


def probe_image(path: str, index: int = 0) -> MediaInfo:
    """探测静态图片（分辨率/尺寸），返回 MediaInfo。

    图片无固定时长：duration 固定为 0，展示时长由合成计划的 Clip.duration 指定。
    失败时抛出 RuntimeError。
    """
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-print_format", "json",
            "-show_streams",
            str(path),
        ],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe 解析图片失败 {path}: {result.stderr[:500]}")

    streams = json.loads(result.stdout).get("streams", [])
    img_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    if img_stream is None:
        raise RuntimeError(f"不是有效的图片文件: {path}")

    return MediaInfo(
        index=index,
        filename=Path(path).name,
        path=str(path),
        duration=0.0,
        width=int(img_stream.get("width") or 0),
        height=int(img_stream.get("height") or 0),
        fps=0.0,
        has_audio=False,
        is_image=True,
    )


def probe_video(path: str, index: int = 0) -> MediaInfo:
    """探测视频文件，返回 MediaInfo。失败时抛出 RuntimeError。"""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-print_format", "json",
            "-show_format", "-show_streams",
            str(path),
        ],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe 解析失败 {path}: {result.stderr[:500]}")

    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video_stream is None:
        raise RuntimeError(f"文件中没有视频轨: {path}")

    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    duration = float(data.get("format", {}).get("duration") or video_stream.get("duration") or 0)

    fps = 0.0
    rate = video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate") or "0/1"
    try:
        num, den = rate.split("/")
        if float(den) != 0:
            fps = float(num) / float(den)
    except (ValueError, ZeroDivisionError):
        pass

    return MediaInfo(
        index=index,
        filename=Path(path).name,
        path=str(path),
        duration=round(duration, 3),
        width=int(video_stream.get("width") or 0),
        height=int(video_stream.get("height") or 0),
        fps=round(fps, 3),
        has_audio=has_audio,
    )


def probe_audio_duration(path: str) -> float:
    """探测音频文件时长（秒）。"""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-print_format", "json", "-show_format",
            str(path),
        ],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe 解析音频失败 {path}: {result.stderr[:500]}")
    duration = float(json.loads(result.stdout).get("format", {}).get("duration") or 0)
    return round(duration, 3)
