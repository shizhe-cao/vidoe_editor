"""FFmpeg 渲染管线。

流程：
1. 每个片段预渲染：截取 + 倍速 + 统一分辨率/fps/pix_fmt（保证 xfade 可用）
2. 拼接：xfade 转场链（或 concat / 单片段直用）
3. 配乐：背景音乐循环/截断对齐成片时长，淡入淡出；可选混入原声
产出 outputs/{job_id}/result.mp4
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, List, Optional

from app.models import CompositionPlan, MediaInfo, TransitionType

ProgressCallback = Callable[[str, int], None]  # (阶段描述, 0~100 百分比)


class RenderError(RuntimeError):
    pass


def _run_ffmpeg(args: List[str], on_progress: Optional[ProgressCallback] = None,
                total_duration: Optional[float] = None, stage: str = "") -> None:
    """运行 ffmpeg，通过 -progress pipe:1 解析进度。失败抛 RenderError。"""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-progress", "pipe:1", "-nostats"] + args
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    last_pct = -1
    assert proc.stdout is not None
    for line in proc.stdout:
        if line.startswith("out_time_ms=") and on_progress and total_duration:
            try:
                out_sec = int(line.split("=")[1]) / 1_000_000
                pct = min(99, int(out_sec / total_duration * 100))
                if pct > last_pct:
                    last_pct = pct
                    on_progress(stage, pct)
            except (ValueError, ZeroDivisionError):
                pass
    _, stderr = proc.communicate()
    if proc.returncode != 0:
        raise RenderError(f"ffmpeg 失败: {stderr[-1000:]}")


def _video_encode_args(lossless: bool, crf: int) -> List[str]:
    """视频编码参数。lossless=True 时 qp 0 无损编码（文件显著增大），否则有损压缩。"""
    if lossless:
        return ["-c:v", "libx264", "-preset", "fast", "-qp", "0",
                "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
            "-pix_fmt", "yuv420p"]


def _timeline_durations(plan: CompositionPlan) -> List[float]:
    """每个片段在成片时间轴上的实际时长（考虑倍速/曲线变速）。"""
    return [c.timeline_duration() for c in plan.clips]


def total_duration(plan: CompositionPlan) -> float:
    """成片总时长（考虑转场重叠与倍速）。"""
    d = sum(_timeline_durations(plan))
    n = len(plan.clips)
    if n > 1 and plan.transition.type != TransitionType.NONE and plan.transition.duration > 0:
        d -= (n - 1) * plan.transition.duration
    return round(d, 3)


def _pre_render_clips(plan: CompositionPlan, media_infos: List[MediaInfo],
                      job_dir: Path, on_progress: Optional[ProgressCallback] = None) -> List[Path]:
    """逐片段截取并统一规格，返回中间文件路径列表。

    恒速片段单命令直出；曲线变速片段切分为多个恒速子段后 concat
    （相邻子段来自同一连续源，硬切无缝）。
    """
    by_index = {m.index: m for m in media_infos}
    w, h = plan.output.resolution
    fps = plan.output.fps
    tmp_dir = job_dir / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    norm = (
        f"fps={fps},"
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"setsar=1"
    )
    # 无损模式下中间片段同样无损，避免二次压缩损失画质；-an 保证中间文件无音轨
    encode = ["-an"] + _video_encode_args(plan.output.lossless, crf=23)

    out_paths = []
    for i, clip in enumerate(plan.clips):
        media = by_index[clip.video_index]
        out_path = tmp_dir / f"clip_{i:03d}.mp4"
        if media.is_image:
            # 静态图片：循环帧展示 duration 秒（无截取、无变速）
            if on_progress:
                on_progress(f"预渲染片段 {i + 1}/{len(plan.clips)}", 10 + int(40 * i / len(plan.clips)))
            args = [
                "-loop", "1", "-t", f"{clip.duration:.3f}", "-i", media.path,
                "-vf", norm,
            ] + encode + [str(out_path)]
            _run_ffmpeg(args)
            out_paths.append(out_path)
            continue
        segments = clip.speed_segments()
        if len(segments) == 1:
            src_start, src_len, v = segments[0]
            args = [
                "-ss", str(src_start), "-t", str(src_len), "-i", media.path,
                "-vf", f"{norm},setpts=PTS/{v:.4f}",
            ] + encode + [str(out_path)]
        else:
            inputs: List[str] = []
            chains: List[str] = []
            for si, (src_start, src_len, v) in enumerate(segments):
                inputs += ["-ss", str(src_start), "-t", str(src_len), "-i", media.path]
                chains.append(f"[{si}:v:0]{norm},setpts=PTS/{v:.4f}[sv{si}]")
            joined = "".join(f"[sv{si}]" for si in range(len(segments)))
            chains.append(f"{joined}concat=n={len(segments)}:v=1:a=0[vout]")
            args = inputs + [
                "-filter_complex", ";".join(chains), "-map", "[vout]",
            ] + encode + [str(out_path)]
        if on_progress:
            on_progress(f"预渲染片段 {i + 1}/{len(plan.clips)}", 10 + int(40 * i / len(plan.clips)))
        _run_ffmpeg(args)
        out_paths.append(out_path)
    if on_progress:
        on_progress("片段预渲染完成", 50)
    return out_paths


def _build_video_chain(plan: CompositionPlan, clip_paths: List[Path]):
    """构建视频拼接参数。返回 (输入参数, filter_complex 片段或 None, 映射目标)。"""
    n = len(clip_paths)
    inputs: List[str] = []
    for p in clip_paths:
        inputs += ["-i", str(p)]

    t = plan.transition.duration
    use_xfade = (
        n > 1 and plan.transition.type != TransitionType.NONE and t > 0
    )

    if n == 1:
        return inputs, None, "0:v:0"

    if not use_xfade:
        fc = "".join(f"[{i}:v:0]" for i in range(n))
        fc += f"concat=n={n}:v=1:a=0[vout]"
        return inputs, fc, "[vout]"

    # xfade 链：offset 相对第一条输入的时间轴
    durs = _timeline_durations(plan)
    xfade_name = plan.transition.type.value
    acc = durs[0]
    fc_parts = []
    prev = "0:v:0"
    for i in range(1, n):
        offset = max(0.0, acc - t)
        label = f"vx{i}" if i < n - 1 else "vout"
        fc_parts.append(
            f"[{prev}][{i}:v:0]xfade=transition={xfade_name}:duration={t}:offset={offset:.3f}[{label}]"
        )
        prev = label
        acc = acc - t + durs[i]
    return inputs, ";".join(fc_parts), "[vout]"


def _build_audio_chain(plan: CompositionPlan, media_infos: List[MediaInfo],
                       music_path: Optional[Path], total: float,
                       clip_paths_len: int):
    """构建音频参数。返回 (输入参数, filter 片段列表, 映射目标或 None, 输出编码参数)。"""
    args: List[str] = []
    has_music = plan.audio and total > 0 and music_path is not None
    want_original = plan.audio.keep_original and any(m.has_audio for m in media_infos)

    if not has_music and not want_original:
        return [], [], None, ["-an"]

    filters: List[str] = []
    input_idx = clip_paths_len  # 视频中间文件之后是音乐输入

    if has_music:
        if plan.audio.loop:
            args += ["-stream_loop", "-1"]
        args += ["-i", str(music_path)]
        fi, fo = plan.audio.fade_in, plan.audio.fade_out
        fade_out_start = max(0.0, total - fo)
        music_chain = (
            f"[{input_idx}:a:0]atrim=0:{total:.3f},asetpts=N/SR/TB,"
            f"volume={plan.audio.volume}"
        )
        if fi > 0:
            music_chain += f",afade=t=in:st=0:d={fi}"
        if fo > 0:
            music_chain += f",afade=t=out:st={fade_out_start:.3f}:d={fo}"
        filters.append(music_chain + "[music]")
        input_idx += 1

    if want_original:
        # 重新引入源视频抽取音频段（仅限有音轨的源）：
        # 曲线变速片段先按恒速子段 atempo 再片段内 concat（同源连续，硬接无缝），
        # 片段之间才做 acrossfade，最后与音乐 amix
        by_index = {m.index: m for m in media_infos}
        orig_inputs: List[str] = []
        labels = []
        for j, clip in enumerate(plan.clips):
            media = by_index[clip.video_index]
            if not media.has_audio:
                continue
            segments = clip.speed_segments()
            seg_labels = []
            for si, (src_start, src_len, v) in enumerate(segments):
                orig_inputs += ["-ss", str(src_start), "-t", str(src_len), "-i", media.path]
                atempo = _atempo_filter(v)
                seg_label = f"[ao{j}_{si}]"
                filters.append(
                    f"[{input_idx}:a:0]aformat=sample_rates=44100:channel_layouts=stereo{atempo}{seg_label}"
                )
                seg_labels.append(seg_label)
                input_idx += 1
            if len(seg_labels) == 1:
                labels.append(seg_labels[0])
            else:
                joined = "".join(seg_labels)
                filters.append(f"{joined}concat=n={len(seg_labels)}:v=0:a=1[oc{j}]")
                labels.append(f"[oc{j}]")
        args += orig_inputs
        # 转场时长不得超过最短片段时间轴时长的一半，否则 acrossfade 失败
        min_dur = min(_timeline_durations(plan))
        t_a = min(plan.transition.duration, min_dur / 2)
        if not labels:
            want_original = False  # 实际无任何音轨可用
        elif len(labels) > 1 and t_a > 0.05:
            prev = labels[0]
            for j in range(1, len(labels)):
                out_label = f"[ax{j}]"
                filters.append(f"{prev}{labels[j]}acrossfade=d={t_a:.3f}{out_label}")
                prev = out_label
            filters.append(f"{prev}atrim=0:{total:.3f}[orig]")
        else:
            joined = "".join(labels)
            filters.append(f"{joined}concat=n={len(labels)}:v=0:a=1[aconcat]")
            filters.append(f"[aconcat]atrim=0:{total:.3f}[orig]")

    if has_music and want_original:
        filters.append("[orig][music]amix=inputs=2:duration=first:dropout_transition=0[aout]")
        audio_map = "[aout]"
    elif has_music:
        audio_map = "[music]"
    else:
        audio_map = "[orig]"

    # 固定采样率，避免 AAC 编码问题
    return args, filters, audio_map, ["-ar", "44100", "-c:a", "aac", "-b:a", "192k"]


def _atempo_filter(speed: float) -> str:
    """atempo 单次只支持 0.5~2，超出范围需链式。返回带前导逗号的过滤串。"""
    if abs(speed - 1.0) < 1e-6:
        return ""
    parts = []
    remaining = speed
    while remaining > 2.0:
        parts.append("atempo=2.0")
        remaining /= 2.0
    while remaining < 0.5:
        parts.append("atempo=0.5")
        remaining *= 2.0
    parts.append(f"atempo={remaining:.4f}")
    return "," + ",".join(parts)


def render(plan: CompositionPlan, media_infos: List[MediaInfo],
           music_path: Optional[str], output_dir: str,
           on_progress: Optional[ProgressCallback] = None) -> str:
    """执行渲染，返回结果文件路径。"""
    job_dir = Path(output_dir)
    job_dir.mkdir(parents=True, exist_ok=True)
    music = Path(music_path) if music_path else None

    total = total_duration(plan)
    if total <= 0:
        raise RenderError("合成计划总时长非法")

    clip_paths = _pre_render_clips(plan, media_infos, job_dir, on_progress)

    video_inputs, video_fc, video_map = _build_video_chain(plan, clip_paths)
    audio_inputs, audio_filters, audio_map, audio_encode = _build_audio_chain(
        plan, media_infos, music, total, len(clip_paths)
    )

    all_filters = ([video_fc] if video_fc else []) + audio_filters
    result_path = job_dir / "result.mp4"
    args = list(video_inputs) + list(audio_inputs)
    if all_filters:
        args += ["-filter_complex", ";".join(all_filters)]
    args += ["-map", video_map]
    if audio_map:
        args += ["-map", audio_map]
    args += audio_encode + _video_encode_args(plan.output.lossless, crf=21) + [
        "-movflags", "+faststart",
        "-t", f"{total:.3f}",
        str(result_path),
    ]
    _run_ffmpeg(args, on_progress=on_progress, total_duration=total, stage="拼接与配乐渲染")
    if on_progress:
        on_progress("渲染完成", 100)
    return str(result_path)
