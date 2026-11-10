"""FastAPI endpoints for upgrade planning, patch generation, integrity checks, and human approval gates."""
from __future__ import annotations

import logging
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from src.models.plan import (
    PatchIntegrityResult,
    PlannedUpgradeItem,
    UpgradePlanRequest,
    UpgradeProposalResponse,
    WorkspaceApplyRequest,
    WorkspaceApplyResponse,
)
from src.services.patch_validator import PatchValidator
from src.services.proposal_registry import RegistryCapacityExceededError
from src.services.upgrade_planner import HumanApprovalRequiredError, UpgradePlannerService

logger = logging.getLogger("dua.api.plan")

router = APIRouter(tags=["Upgrade Planning & Patch Generation"])

# Dependency injection for UpgradePlannerService
_planner_instance: Optional[UpgradePlannerService] = None


def get_upgrade_planner() -> UpgradePlannerService:
    global _planner_instance
    if _planner_instance is None:
        _planner_instance = UpgradePlannerService()
    return _planner_instance


class ValidatePatchPayload(BaseModel):
    """Payload for standalone patch integrity validation."""
    manifest_type: str = Field(..., description="Manifest type ('requirements.txt' or 'package.json')")
    original_content: str = Field(..., description="Original manifest content")
    proposed_content: str = Field(..., description="Proposed manifest content")
    unified_diff: str = Field(..., description="Unified diff text")
    file_path: Optional[str] = Field(default=None, description="Optional file path")
    plan_items: Optional[list[PlannedUpgradeItem]] = Field(default=None, description="Optional upgrade plan items")


@router.post(
    "/plan/generate",
    response_model=UpgradeProposalResponse,
    summary="Generate Upgrade Plan and Patch",
    description=(
        "Constructs a deterministic upgrade plan from validated recommendations, mutates the manifest "
        "without touching the original file, generates a unified diff, and runs patch integrity checks."
    ),
)
async def generate_upgrade_plan(
    request: UpgradePlanRequest,
    planner: Annotated[UpgradePlannerService, Depends(get_upgrade_planner)],
) -> UpgradeProposalResponse:
    """Construct an upgrade proposal from validated M3 recommendations and user selections."""
    try:
        proposal = await run_in_threadpool(planner.create_upgrade_proposal, request)
        return proposal
    except RegistryCapacityExceededError as err:
        logger.warning(f"Proposal generation rejected: registry capacity exhausted: {err}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(err),
        )
    except Exception as err:
        logger.exception(f"Error generating upgrade plan: {err}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate upgrade plan: {str(err)}",
        )


@router.post(
    "/plan/validate-patch",
    response_model=PatchIntegrityResult,
    summary="Validate Unified Diff Integrity",
    description=(
        "Validates syntax, applicability, reversibility, path safety, and preservation of unmodified lines."
    ),
)
async def validate_patch(
    payload: ValidatePatchPayload,
) -> PatchIntegrityResult:
    """Run standalone patch integrity checks on original, proposed, and diff content."""
    try:
        result = await run_in_threadpool(
            PatchValidator.validate_patch,
            manifest_type=payload.manifest_type,
            original_content=payload.original_content,
            proposed_content=payload.proposed_content,
            unified_diff=payload.unified_diff,
            plan_items=payload.plan_items or [],
            file_path=payload.file_path,
        )
        return result
    except Exception as err:
        logger.exception(f"Error validating patch integrity: {err}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to validate patch: {str(err)}",
        )


@router.post(
    "/plan/apply",
    response_model=WorkspaceApplyResponse,
    summary="Apply Approved Upgrade Proposal to Workspace",
    description=(
        "Enforces explicit human approval gate prior to workspace mutation. Rejects any mutation "
        "request where approved is false."
    ),
)
async def apply_upgrade_proposal(
    request: WorkspaceApplyRequest,
    planner: Annotated[UpgradePlannerService, Depends(get_upgrade_planner)],
) -> WorkspaceApplyResponse:
    """Apply an approved upgrade proposal to the target workspace under human confirmation."""
    try:
        response = await run_in_threadpool(planner.apply_workspace_mutation, request)
        return response
    except HumanApprovalRequiredError as err:
        logger.warning(f"Human approval gate rejection: {err}")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(err),
        )
    except Exception as err:
        logger.exception(f"Error applying workspace mutation: {err}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to apply workspace mutation: {str(err)}",
        )
