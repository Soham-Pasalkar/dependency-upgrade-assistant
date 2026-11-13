"""FastAPI endpoints for containerized sandbox validation, execution tracking, and logs."""
from __future__ import annotations

import logging
from typing import Annotated, Any, Dict

from fastapi import APIRouter, Depends, HTTPException, status
from starlette.concurrency import run_in_threadpool

from src.models.sandbox import (
    SandboxExecutionLogResponse,
    SandboxExecutionRequest,
    ValidationReportResponse,
)
from src.services.sandbox_service import SandboxService, get_sandbox_service
from src.services.approval_service import ApprovalError, require_approved_proposal
from src.services.proposal_registry import ProposalRegistry, get_proposal_registry
from src.services.run_store import RunStore, get_run_store
from pathlib import Path

from src.api.config import settings
from src.api.observability import internal_error

logger = logging.getLogger("dua.api.validation")

router = APIRouter(tags=["Sandboxed Validation"])


@router.get(
    "/validation/status",
    summary="Check Docker Sandbox Availability",
    description="Probes the Docker runtime daemon and reports availability status.",
)
async def get_sandbox_status(
    service: Annotated[SandboxService, Depends(get_sandbox_service)],
) -> Dict[str, Any]:
    """Return whether the Docker sandbox runtime is operational."""
    availability = await run_in_threadpool(service.get_availability)
    return {
        "is_available": availability.is_available,
        "version": availability.version,
        "error_message": availability.error_message,
    }


@router.post(
    "/validation/execute",
    response_model=ValidationReportResponse,
    summary="Execute Sandboxed Validation",
    description=(
        "Executes dependency verification, syntax compilation, and test execution inside "
        "an isolated Docker container with strict network isolation (--network none) and resource limits. "
        "Truthfully reports NOT_RUNNABLE if Docker is unavailable without executing untrusted code on the host."
    ),
)
async def execute_validation(
    request: SandboxExecutionRequest,
    service: Annotated[SandboxService, Depends(get_sandbox_service)],
    store: Annotated[RunStore, Depends(get_run_store)],
    registry: Annotated[ProposalRegistry, Depends(get_proposal_registry)],
) -> ValidationReportResponse:
    """Initiate and await sandboxed validation for an approved proposal."""
    try:
        approved = await run_in_threadpool(
            require_approved_proposal, request.proposal_id, store, registry, "run validation"
        )
    except ApprovalError as err:
        raise HTTPException(status_code=err.status_code, detail=err.message)

    # The approved, server-controlled policy decides limits; explicit client values must agree with it (F13).
    policy = approved.policy
    expected = {
        "timeout_seconds": policy.timeout_seconds,
        "memory_limit_mb": policy.memory_limit_mb,
        "cpu_limit": policy.cpu_limit,
        "pids_limit": policy.pids_limit,
        "manifest_type": approved.stored.proposal.manifest_type,
        "ecosystem": policy.ecosystem,
    }
    for field, value in expected.items():
        if field in request.model_fields_set and getattr(request, field) is not None:
            given = getattr(request, field)
            same = float(given) == float(value) if field == "cpu_limit" else str(given) == str(value)
            if not same:
                raise HTTPException(
                    status_code=422,
                    detail=f"{field} is set by the server for approved plans and cannot be changed by the client.",
                )

    # Repository runs validate the real workspace with the approved change applied (FR-18).
    source_workspace = None
    workspace_dir = await run_in_threadpool(store.workspace_dir, approved.binding.run_id)
    if workspace_dir:
        if not Path(workspace_dir).is_dir():
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail="The repository copy has expired. Analyze the repository again to validate.",
            )
        # The sandbox boundary check takes a path relative to the server workspace root.
        try:
            source_workspace = str(Path(workspace_dir).resolve().relative_to(settings.resolved_workspace_root))
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="The repository copy is outside the workspace area and cannot be validated.",
            )

    try:
        report = await run_in_threadpool(service.execute_validation, request, source_workspace, policy)
        return report
    except ValueError as err:
        err_msg = str(err)
        if "not found" in err_msg.lower():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=err_msg,
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=err_msg,
        )
    except Exception as err:
        logger.exception(f"Unexpected error executing sandboxed validation: {err}")
        raise internal_error(logger, "Validation", err) from err


@router.get(
    "/validation/{execution_id}",
    response_model=ValidationReportResponse,
    summary="Get Validation Execution Report",
    description="Retrieves the detailed outcome, command results, and status of a validation execution.",
)
async def get_validation_report(
    execution_id: str,
    service: Annotated[SandboxService, Depends(get_sandbox_service)],
) -> ValidationReportResponse:
    """Retrieve validation report by execution ID."""
    report = service.get_execution(execution_id)
    if not report:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Validation execution '{execution_id}' not found.",
        )
    return report


@router.get(
    "/validation/{execution_id}/logs",
    response_model=SandboxExecutionLogResponse,
    summary="Get Validation Execution Logs",
    description="Retrieves bounded stdout and stderr logs for a validation execution.",
)
async def get_validation_logs(
    execution_id: str,
    service: Annotated[SandboxService, Depends(get_sandbox_service)],
) -> SandboxExecutionLogResponse:
    """Retrieve captured logs for an execution."""
    logs_res = service.get_execution_logs(execution_id)
    if not logs_res:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Validation execution '{execution_id}' not found.",
        )
    return logs_res


@router.post(
    "/validation/{execution_id}/cancel",
    summary="Cancel Validation Execution",
    description="Terminates an active container and sets execution status to CANCELLED.",
)
async def cancel_validation_execution(
    execution_id: str,
    service: Annotated[SandboxService, Depends(get_sandbox_service)],
) -> Dict[str, Any]:
    """Cancel an in-flight validation execution."""
    success = service.cancel_execution(execution_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Validation execution '{execution_id}' not found.",
        )
    return {
        "success": True,
        "execution_id": execution_id,
        "message": "Validation execution cancelled successfully.",
    }
