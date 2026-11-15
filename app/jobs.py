"""后台合成任务管理：内存任务表 + 线程池执行。"""
from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Optional

from app.agent.graph import run_composition
from app.models import Job, JobStatus

_JOBS: Dict[str, Job] = {}
_LOCK = threading.Lock()
_POOL = ThreadPoolExecutor(max_workers=2)


def create_job(video_paths, music_path: Optional[str], rule_text: str,
               rule_is_json: bool) -> Job:
    job = Job(
        id=uuid.uuid4().hex[:12],
        video_paths=video_paths,
        music_path=music_path,
        rule_text=rule_text,
        rule_is_json=rule_is_json,
    )
    with _LOCK:
        _JOBS[job.id] = job
    return job


def get_job(job_id: str) -> Optional[Job]:
    with _LOCK:
        return _JOBS.get(job_id)


def _update(job_id: str, **fields) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if job:
            for k, v in fields.items():
                setattr(job, k, v)


def submit_job(job: Job, output_dir: str) -> None:
    """把任务提交到线程池执行。"""
    _POOL.submit(_execute, job.id, output_dir)


def _execute(job_id: str, output_dir: str) -> None:
    job = get_job(job_id)
    if job is None:
        return

    def progress_with_stage(detail: str, percent: int) -> None:
        if percent < 5:
            status = JobStatus.PROBING
        elif percent < 10:
            status = JobStatus.PLANNING
        else:
            status = JobStatus.RENDERING
        _update(job_id, status=status, stage_detail=detail,
                progress=min(100, max(0, percent)))

    state = run_composition(
        {
            "job_id": job_id,
            "video_paths": job.video_paths,
            "music_path": job.music_path,
            "rule_text": job.rule_text,
            "rule_is_json": job.rule_is_json,
            "output_dir": output_dir,
        },
        on_progress=progress_with_stage,
    )

    if state.get("error"):
        _update(job_id, status=JobStatus.FAILED, error=state["error"], progress=100)
        return
    _update(
        job_id,
        status=JobStatus.DONE,
        progress=100,
        stage_detail="完成",
        media_infos=state.get("media_infos", []),
        plan=state.get("plan"),
        music_duration=state.get("music_duration", 0.0),
        result_path=state.get("result_path"),
    )
