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
) -> ValidationReportResponse:
    """Initiate and await sandboxed validation for a proposal or snapshot."""
    try:
        report = await run_in_threadpool(service.execute_validation, request)
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
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Validation execution failed: {str(err)}",
        )


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
