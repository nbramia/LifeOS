"""
Job queue API endpoints for LifeOS.

Provides:
- List jobs with filtering
- Get job status
- Cancel pending jobs
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
import logging

from api.services.job_queue import get_job_queue, is_stale_running_job

router = APIRouter(prefix="/api/jobs", tags=["jobs"])
logger = logging.getLogger(__name__)


class JobResponse(BaseModel):
    id: str
    type: str
    status: str
    params: dict
    result: Optional[dict] = None
    created_at: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    attempts: int
    max_attempts: int
    priority: int
    error: Optional[str] = None
    # Additive (#768): true when this job is reported "running" but its
    # started_at predates this process's own start — it was stranded by a
    # previous process (e.g. an unrelated auto-deploy restart mid-job) and
    # is not actually progressing. Surfaced here so a caller polling a
    # specific job isn't misled before JobQueue.start_worker()'s startup
    # reconciliation has caught up (or if it never ran against this queue).
    stale: bool = False


class JobListResponse(BaseModel):
    jobs: list[JobResponse]
    count: int


def _to_response(job, process_start_time: str) -> JobResponse:
    data = job.to_dict()
    data["stale"] = is_stale_running_job(job, process_start_time)
    return JobResponse(**data)


@router.get("", response_model=JobListResponse)
async def list_jobs(
    status: Optional[str] = None,
    type: Optional[str] = None,
    limit: int = 50,
) -> JobListResponse:
    """List jobs with optional filtering by status or type."""
    queue = get_job_queue()
    jobs = queue.list_jobs(status=status, job_type=type, limit=limit)
    return JobListResponse(
        jobs=[_to_response(j, queue.process_start_time) for j in jobs],
        count=len(jobs),
    )


@router.get("/{job_id}", response_model=JobResponse)
async def get_job(job_id: str) -> JobResponse:
    """Get a specific job's status and result."""
    queue = get_job_queue()
    job = queue.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return _to_response(job, queue.process_start_time)


@router.post("/{job_id}/cancel")
async def cancel_job(job_id: str):
    """Cancel a pending job."""
    queue = get_job_queue()
    job = queue.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    if queue.cancel_job(job_id):
        return {"status": "cancelled", "job_id": job_id}
    return {"status": "not_cancellable", "message": f"Job is {job.status}, only pending jobs can be cancelled"}
