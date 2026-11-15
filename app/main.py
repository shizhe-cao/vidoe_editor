"""FastAPI 入口：上传/任务/进度 SSE/结果下载 + 静态页面。"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app import config, jobs
from app.models import Job, JobStatus

app = FastAPI(title="视频合成 Agent", version="0.1.0")

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi"}
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".flac"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
MEDIA_EXT = VIDEO_EXT | IMAGE_EXT


@app.get("/api/assets")
def list_assets():
    """列出服务器素材目录中的可用文件（供无法弹出文件对话框的环境使用）。"""
    from app.media.probe import IMAGE_EXT, probe_audio_duration, probe_image, probe_video

    assets_dir = config.BASE_DIR / "test_assets"
    videos, musics, images = [], [], []
    if assets_dir.is_dir():
        for p in sorted(assets_dir.iterdir()):
            if not p.is_file():
                continue
            ext = p.suffix.lower()
            try:
                if ext in VIDEO_EXT:
                    info = probe_video(str(p))
                    videos.append({"name": p.name, "size_mb": round(p.stat().st_size / 1048576, 1),
                                   "duration": info.duration, "resolution": f"{info.width}x{info.height}"})
                elif ext in IMAGE_EXT:
                    info = probe_image(str(p))
                    images.append({"name": p.name, "size_mb": round(p.stat().st_size / 1048576, 1),
                                   "resolution": f"{info.width}x{info.height}"})
                elif ext in AUDIO_EXT:
                    musics.append({"name": p.name, "size_mb": round(p.stat().st_size / 1048576, 1),
                                   "duration": probe_audio_duration(str(p))})
            except Exception:  # noqa: BLE001 - 无效文件跳过而非报错
                continue
    return {"videos": videos, "musics": musics, "images": images}


def _resolve_asset(name: str) -> Path:
    """将素材名解析为受限目录内的安全路径。"""
    assets_dir = (config.BASE_DIR / "test_assets").resolve()
    path = (assets_dir / name).resolve()
    if not str(path).startswith(str(assets_dir)) or not path.is_file():
        raise HTTPException(400, f"素材不存在: {name}")
    return path


@app.get("/api/assets/file/{name}")
def get_asset_file(name: str):
    """流式返回服务器素材文件（用于页面缩略图/预览播放）。"""
    path = _resolve_asset(name)
    return FileResponse(path)


@app.post("/api/jobs")
async def create_job(
    videos: list[UploadFile] = File(None, description="源素材文件（视频或图片，可多个，按提交顺序排列）"),
    music: UploadFile | None = File(None, description="背景音乐"),
    rule: str = Form("", description="合成规则（自然语言或 JSON）"),
    rule_format: str = Form("text", description="规则格式: text | json"),
    video_assets: list[str] = Form(None, description="服务器素材名（视频或图片，可多个）"),
    music_asset: str = Form(None, description="服务器素材音乐名"),
):
    if not videos and not video_assets:
        raise HTTPException(400, "至少上传或选择一个素材（视频或图片）")

    job_dir = config.UPLOAD_DIR / _new_id()
    job_dir.mkdir(parents=True, exist_ok=True)

    video_paths = []
    for i, f in enumerate(videos or []):
        ext = Path(f.filename or f"v{i}.mp4").suffix.lower()
        if ext not in MEDIA_EXT:
            raise HTTPException(400, f"不支持的素材格式 {f.filename}（支持 {sorted(MEDIA_EXT)}）")
        path = job_dir / f"video_{i}{ext}"
        await _save_upload(f, path)
        video_paths.append(str(path))
    for name in video_assets or []:
        video_paths.append(str(_resolve_asset(name)))

    music_path = None
    if music and music.filename:
        ext = Path(music.filename).suffix.lower()
        if ext not in AUDIO_EXT:
            raise HTTPException(400, f"不支持的音乐格式 {music.filename}（支持 {sorted(AUDIO_EXT)}）")
        path = job_dir / f"music{ext}"
        await _save_upload(music, path)
        music_path = str(path)
    elif music_asset:
        music_path = str(_resolve_asset(music_asset))

    if not rule.strip():
        raise HTTPException(400, "请填写合成规则")
    rule_is_json = rule_format == "json" or _looks_like_json(rule)
    job = jobs.create_job(video_paths, music_path, rule, rule_is_json)
    jobs.submit_job(job, str(config.OUTPUT_DIR / job.id))
    return {"job_id": job.id}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    return _job_payload(job)


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str):
    """SSE 进度流：持续推送任务状态直到 done/failed。"""
    if jobs.get_job(job_id) is None:
        raise HTTPException(404, "任务不存在")

    async def event_stream():
        last_snapshot = None
        while True:
            job = jobs.get_job(job_id)
            if job is None:
                yield "event: error\ndata: {}\n\n"
                break
            snapshot = _job_payload(job)
            if snapshot != last_snapshot:
                last_snapshot = snapshot
                yield f"data: {json.dumps(snapshot, ensure_ascii=False)}\n\n"
            if job.status in (JobStatus.DONE, JobStatus.FAILED):
                yield "event: close\ndata: {}\n\n"
                break
            await asyncio.sleep(0.8)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/api/jobs/{job_id}/result")
def get_result(job_id: str):
    job = jobs.get_job(job_id)
    if job is None or not job.result_path or not Path(job.result_path).exists():
        raise HTTPException(404, "结果不存在")
    return FileResponse(job.result_path, media_type="video/mp4", filename=f"{job_id}.mp4")


@app.get("/", response_class=HTMLResponse)
def index():
    # no-cache：确保前端页面改造后浏览器立即拿到新版本，不命中旧缓存
    return FileResponse(config.WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})


def _job_payload(job: Job) -> dict:
    return {
        "id": job.id,
        "status": job.status.value,
        "stage_detail": job.stage_detail,
        "progress": job.progress,
        "error": job.error,
        "media_infos": [m.model_dump() for m in job.media_infos],
        "plan": job.plan.model_dump() if job.plan else None,
        "music_duration": job.music_duration,
        "has_result": bool(job.result_path and Path(job.result_path).exists()),
    }


async def _save_upload(f: UploadFile, path: Path) -> None:
    size = 0
    with path.open("wb") as out:
        while chunk := await f.read(1024 * 1024):
            size += len(chunk)
            out.write(chunk)
    if size == 0:
        path.unlink(missing_ok=True)
        raise HTTPException(400, f"上传文件为空: {f.filename}")


def _new_id() -> str:
    import uuid
    return uuid.uuid4().hex[:12]


def _looks_like_json(text: str) -> bool:
    t = text.strip()
    return (t.startswith("{") and t.endswith("}")) or bool(re.match(r"^\s*[\[{]", t))
