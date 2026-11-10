"""FastAPI endpoints for manifest ingestion and dependency analysis."""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from fastapi.responses import JSONResponse

from src.analysis.manifest_parser import ManifestParseError
from src.models.manifest import (
    ManifestAnalysisRequest,
    ManifestAnalysisResponse,
)
from src.services.manifest_service import ManifestService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/manifest", tags=["manifest"])
manifest_service = ManifestService()

MAX_UPLOAD_SIZE_BYTES = 1024 * 1024  # 1 MB upload cap


def _detect_manifest_type_from_filename(filename: str) -> Optional[str]:
    fn = filename.lower()
    if fn.endswith(".txt") or "requirements" in fn:
        return "requirements.txt"
    if fn.endswith(".json") or "package" in fn:
        return "package.json"
    return None


@router.post(
    "/analyze",
    response_model=ManifestAnalysisResponse,
    summary="Analyze raw manifest text",
    description="Parses raw requirements.txt or package.json manifest content, queries official package registries, and classifies dependencies.",
)
async def analyze_raw_manifest(
    request: ManifestAnalysisRequest,
) -> ManifestAnalysisResponse:
    if not request.content.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Manifest content cannot be empty.",
        )

    try:
        response = manifest_service.analyze_manifest(
            manifest_type=request.manifest_type,
            content=request.content,
        )
        return response
    except ManifestParseError as err:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": str(err), "diagnostics": err.diagnostics},
        )
    except Exception as err:
        logger.error(f"Unexpected error analyzing manifest: {err}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Internal error processing manifest: {err}",
        )


@router.post(
    "/upload",
    response_model=ManifestAnalysisResponse,
    summary="Upload and analyze manifest file",
    description="Accepts a multipart file upload for requirements.txt or package.json, parses dependencies, and queries registries.",
)
async def upload_manifest_file(
    file: UploadFile = File(...),
    manifest_type: Optional[str] = Form(None),
) -> ManifestAnalysisResponse:
    filename = file.filename or "unknown"
    detected_type = manifest_type or _detect_manifest_type_from_filename(filename)

    if not detected_type:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Unsupported file '{filename}'. Supported manifest formats are "
                "'requirements.txt' and 'package.json'."
            ),
        )

    # Read content with size guard
    content_bytes = await file.read(MAX_UPLOAD_SIZE_BYTES + 1024)
    if len(content_bytes) > MAX_UPLOAD_SIZE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds maximum allowed size of {MAX_UPLOAD_SIZE_BYTES // 1024} KB.",
        )

    try:
        content_text = content_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Uploaded file must be valid UTF-8 text.",
        )

    if not content_text.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Uploaded manifest file is empty.",
        )

    try:
        response = manifest_service.analyze_manifest(
            manifest_type=detected_type,
            content=content_text,
        )
        return response
    except ManifestParseError as err:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": str(err), "diagnostics": err.diagnostics},
        )
    except Exception as err:
        logger.error(f"Unexpected error processing uploaded manifest '{filename}': {err}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Internal error processing uploaded manifest: {err}",
        )
