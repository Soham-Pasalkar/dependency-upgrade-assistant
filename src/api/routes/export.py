"""FastAPI endpoints for deliverable artifact export, checksum verification, and downloads."""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from src.models.export import (
    ArtifactType,
    ExportMetadataResponse,
    ExportRequest,
)
from src.services.exporter import (
    ArtifactExporter,
    get_artifact_exporter,
)
from src.services.proposal_registry import (
    ProposalRegistry,
    get_proposal_registry,
)
from src.services.sandbox_service import (
    SandboxService,
    get_sandbox_service,
)
from src.services.secret_scanner import SecretLeakageError

logger = logging.getLogger("dua.api.export")

router = APIRouter(tags=["Artifact Export & Deliverables"])


@router.post(
    "/export",
    response_model=ExportMetadataResponse,
    summary="Generate Verified Deliverables Export",
    description=(
        "Bundles verified dependency upgrades into a Git-compatible .patch, a structured JSON execution summary, "
        "sanitized validation logs, a SHA-256 checksums manifest, and a validated ZIP archive. "
        "Strictly verifies zero credential leakage before exporting deliverables."
    ),
)
async def create_export(
    request: ExportRequest,
    exporter: Annotated[ArtifactExporter, Depends(get_artifact_exporter)],
    registry: Annotated[ProposalRegistry, Depends(get_proposal_registry)],
    sandbox: Annotated[SandboxService, Depends(get_sandbox_service)],
) -> ExportMetadataResponse:
    """Construct verified deliverable bundle for a proposal and optional validation run."""
    if not request.proposal_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A valid 'proposal_id' is required to generate deliverables.",
        )

    # 1. Retrieve server-authoritative proposal
    stored_proposal = registry.get(request.proposal_id)
    if not stored_proposal:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Proposal '{request.proposal_id}' was not found or has expired.",
        )

    # 2. Retrieve optional validation report
    validation_report = None
    if request.execution_id:
        validation_report = sandbox.get_execution(request.execution_id)
        if not validation_report:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Validation execution '{request.execution_id}' was not found.",
            )

    # 3. Export deliverables in worker thread pool
    try:
        bundle = await run_in_threadpool(
            exporter.export_proposal,
            proposal=stored_proposal.proposal,
            validation_report=validation_report,
            run_id=request.run_id,
            custom_notes=request.custom_notes,
        )
        return bundle.to_response()
    except SecretLeakageError as err:
        logger.error(f"Deliverable export rejected due to secret leakage: {err}")
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(err),
        )
    except ValueError as err:
        logger.warning(f"Invalid export request parameter: {err}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(err),
        )
    except Exception as err:
        logger.exception(f"Unexpected error constructing export deliverables: {err}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate export deliverables: {str(err)}",
        )


@router.get(
    "/export/{export_id}",
    response_model=ExportMetadataResponse,
    summary="Get Export Metadata and Checksums",
    description="Retrieves metadata, download URLs, and SHA-256 checksums for an existing export bundle.",
)
async def get_export_metadata(
    export_id: str,
    exporter: Annotated[ArtifactExporter, Depends(get_artifact_exporter)],
) -> ExportMetadataResponse:
    """Retrieve metadata and deliverable checksums for an export bundle."""
    bundle = exporter.get_export(export_id)
    if not bundle:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Export bundle '{export_id}' was not found.",
        )
    return bundle.to_response()


@router.get(
    "/export/{export_id}/download/{artifact_type}",
    summary="Download Deliverable Artifact File",
    description="Streams an individual deliverable file (.patch, summary.json, execution.log, checksums, or .zip).",
)
async def download_artifact(
    export_id: str,
    artifact_type: str,
    exporter: Annotated[ArtifactExporter, Depends(get_artifact_exporter)],
) -> FileResponse:
    """Stream an exported deliverable file with safe Content-Disposition and SHA-256 header."""
    try:
        file_path, filename, mime_type, sha256 = exporter.get_artifact_file(
            export_id=export_id,
            artifact_type=artifact_type,
        )
        return FileResponse(
            path=file_path,
            media_type=mime_type,
            filename=filename,
            headers={
                "X-Checksum-SHA256": sha256,
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )
    except FileNotFoundError as err:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(err),
        )
    except ValueError as err:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(err),
        )
    except Exception as err:
        logger.exception(f"Error serving export artifact '{artifact_type}': {err}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error retrieving artifact file.",
        )


@router.get(
    "/export/{export_id}/{artifact_type}",
    summary="Download Deliverable Artifact (Alias)",
    description="Convenience download alias for deliverable artifacts.",
    include_in_schema=False,
)
async def download_artifact_alias(
    export_id: str,
    artifact_type: str,
    exporter: Annotated[ArtifactExporter, Depends(get_artifact_exporter)],
) -> FileResponse:
    """Convenience alias route matching /export/{export_id}/{artifact_type}."""
    return await download_artifact(export_id=export_id, artifact_type=artifact_type, exporter=exporter)
