"""Compatibility resolution API (FR-28 to FR-30, FR-32; ADR-011, ADR-012).

All state is server-held: the plan, its workspace, the jobs and the supervisor-written evidence.
Nothing here accepts a status, fingerprint, report, date or comparison from a client.
"""
from __future__ import annotations

from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from src.resolution.models import Comparison, ResolutionReport, ResolutionStatus
from src.resolution.policy import COMPATIBILITY_DISCLOSURE
from src.services.job_service import (
    JobServiceError,
    ResolutionJobService,
    get_job_service,
    resolution_state,
)
from src.services.proposal_registry import ProposalRegistry, get_proposal_registry
from src.services.run_store import JobRecord, RunStore, get_run_store

router = APIRouter(tags=["Compatibility Resolution"])
_ID = r"^[A-Za-z0-9_\-]{1,128}$"


class JobView(BaseModel):
    id: str
    status: str
    current_role: Optional[str] = None
    cancel_requested: bool = False
    created_at: float
    started_at: Optional[float] = None
    deadline_at: Optional[float] = None
    finished_at: Optional[float] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None

    @classmethod
    def of(cls, job: Optional[JobRecord]) -> Optional["JobView"]:
        if job is None:
            return None
        return cls(id=job.id, status=job.status.value, current_role=job.current_role,
                   cancel_requested=job.cancel_requested, created_at=job.created_at, started_at=job.started_at,
                   deadline_at=job.deadline_at, finished_at=job.finished_at, error_code=job.error_code,
                   error_message=job.error_message)


class ResolutionView(BaseModel):
    proposal_id: str
    status: ResolutionStatus
    reason: Optional[str] = None
    approval_eligible: bool = Field(description="True only for current, verified COMPATIBLE proposed evidence")
    job: Optional[JobView] = None
    proposed: Optional[ResolutionReport] = Field(default=None, description="Authoritative evidence (FR-28)")
    original: Optional[ResolutionReport] = Field(default=None, description="Informational baseline (FR-32b)")
    comparison: Optional[Comparison] = None
    disclosure: str = COMPATIBILITY_DISCLOSURE


class ResolveResponse(BaseModel):
    job: Optional[JobView] = None
    resolution: ResolutionView


def _error(err: JobServiceError) -> HTTPException:
    return HTTPException(status_code=err.status_code, detail={"code": err.code, "message": err.message})


def build_view(proposal_id: str, store: RunStore, registry: ProposalRegistry) -> ResolutionView:
    state = resolution_state(proposal_id, store, registry)
    proposed = state.proposed.report if state.proposed else None
    return ResolutionView(
        proposal_id=proposal_id,
        status=state.status,
        reason=state.reason,
        approval_eligible=state.status == ResolutionStatus.COMPATIBLE and proposed is not None,
        job=JobView.of(state.job),
        proposed=proposed,
        original=state.original.report if state.original else None,
        comparison=proposed.comparison if proposed else None,
    )


@router.post("/proposals/{proposal_id}/resolve", response_model=ResolveResponse,
             status_code=status.HTTP_202_ACCEPTED, summary="Start the compatibility check for a plan")
async def start_resolution(
    proposal_id: str,
    response: Response,
    service: Annotated[ResolutionJobService, Depends(get_job_service)],
    store: Annotated[RunStore, Depends(get_run_store)],
    registry: Annotated[ProposalRegistry, Depends(get_proposal_registry)],
) -> ResolveResponse:
    _check_id(proposal_id)
    try:
        result = await run_in_threadpool(service.submit, proposal_id)
        view = await run_in_threadpool(build_view, proposal_id, store, registry)
    except JobServiceError as err:
        raise _error(err)
    if result.job is None:
        response.status_code = status.HTTP_200_OK  # answered immediately: input is UNSUPPORTED
    return ResolveResponse(job=JobView.of(result.job), resolution=view)


@router.get("/proposals/{proposal_id}/resolution", response_model=ResolutionView,
            summary="Compatibility status, evidence, comparison and resolved packages for a plan")
async def get_resolution(
    proposal_id: str,
    store: Annotated[RunStore, Depends(get_run_store)],
    registry: Annotated[ProposalRegistry, Depends(get_proposal_registry)],
) -> ResolutionView:
    _check_id(proposal_id)
    try:
        return await run_in_threadpool(build_view, proposal_id, store, registry)
    except JobServiceError as err:
        raise _error(err)


@router.get("/jobs/{job_id}", response_model=JobView, summary="Resolution job status")
async def get_job(job_id: str, service: Annotated[ResolutionJobService, Depends(get_job_service)]) -> JobView:
    _check_id(job_id)
    try:
        return JobView.of(await run_in_threadpool(service.get, job_id))
    except JobServiceError as err:
        raise _error(err)


@router.post("/jobs/{job_id}/cancel", response_model=JobView, summary="Cancel a resolution job")
async def cancel_job(job_id: str, service: Annotated[ResolutionJobService, Depends(get_job_service)]) -> JobView:
    _check_id(job_id)
    try:
        return JobView.of(await run_in_threadpool(service.cancel, job_id))
    except JobServiceError as err:
        raise _error(err)


def _check_id(value: str) -> None:
    import re
    if not re.match(_ID, value or ""):
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "Not found."})
