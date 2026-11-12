"""Server-authoritative analysis runs: GitHub repositories and manual manifests (FR-1R, FR-2R, FR-15, FR-16)."""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from starlette.concurrency import run_in_threadpool

from src.analysis.manifest_parser import ManifestParseError
from src.analysis.pyproject_parser import parse_pyproject_toml
from src.api.config import settings
from src.api.routes.manifest import MAX_MANIFEST_SIZE_BYTES, manifest_service
from src.api.routes.plan import get_upgrade_planner
from src.api.routes.recommendations import get_recommendation_service
from src.models.plan import UpgradePlanRequest, UpgradeProposalResponse
from src.models.recommendation import RecommendationBatchRequest, RecommendationBatchResponse
from src.models.run import (
    ApprovalRequest,
    ApprovalResponse,
    ManifestRunRequest,
    RepositoryAnalyzeRequest,
    RepositoryInfo,
    RunPlanRequest,
    RunRecommendationRequest,
    RunResponse,
)
from src.services import repository_service as repo_svc
from src.sandbox.validation_policy import ValidationPolicyError, policy_for_proposal
from src.services.approval_service import ensure_registered
from src.services.retention import maybe_run_retention
from src.services.proposal_registry import ProposalRegistry, get_proposal_registry
from src.services.recommendation_service import RecommendationService
from src.services.run_store import RunStore, StoredManifest, get_run_store, sha256_text
from src.services.upgrade_planner import UpgradePlannerService

logger = logging.getLogger("dua.api.runs")
router = APIRouter(tags=["Analysis Runs"])

SUPPORTED_MANUAL_TYPES = {"requirements.txt", "package.json"}


def _analyzer_type(kind: str) -> str:
    return "package.json" if kind == "package_json" else "requirements.txt"


def _pyproject_as_requirements(path: Path) -> tuple[str, List[str]]:
    """Express pyproject.toml dependencies as requirement lines for read-only analysis."""
    parsed = parse_pyproject_toml(path)
    lines = []
    for dep in parsed.dependencies:
        extras = f"[{dep.extras}]" if dep.extras else ""
        marker = f"; {dep.environment_marker}" if dep.environment_marker else ""
        lines.append(f"{dep.package_name}{extras}{dep.specifier or ''}{marker}")
    notes = [f"Not analyzed: {u}" for u in parsed.unsupported_declarations[:20]]
    return "\n".join(lines) + ("\n" if lines else ""), notes


def _analyze_into(store: RunStore, run_id: str, index: int, path: str, kind: str, content: str,
                  plannable: bool, note: Optional[str]) -> None:
    analysis = None
    error = None
    try:
        analysis = manifest_service.analyze_manifest(manifest_type=_analyzer_type(kind), content=content)
    except ManifestParseError as err:
        error = str(err)
    except Exception as err:  # registry/network failures are reported per manifest
        logger.warning("Analysis failed for %s: %s", path, err)
        error = "This manifest could not be analyzed."
    store.add_manifest(
        StoredManifest(
            run_id=run_id, index=index, path=path, manifest_type=_analyzer_type(kind), kind=kind,
            plannable=plannable and analysis is not None, content=content, analysis=analysis,
            recommendations=None, proposal_id=None, note=note, error=error,
        )
    )


def _analyze_repository(url: str, store: RunStore) -> RunResponse:
    workspace_root = settings.resolved_workspace_root
    repo_svc.cleanup_expired_workspaces(workspace_root)
    acquired = repo_svc.acquire_repository(url, workspace_root)
    try:
        discovery = repo_svc.discover_manifests(acquired.workspace)
        info = RepositoryInfo(
            full_name=acquired.metadata.full_name,
            url=acquired.ref.html_url,
            commit_sha=acquired.commit_sha,
            default_branch=acquired.metadata.default_branch,
            description=acquired.metadata.description,
            files_scanned=discovery.files_scanned,
            bytes_on_disk=acquired.bytes_on_disk,
        )
        run_id = store.create_run(
            source="github", repository=info, workspace_dir=str(acquired.workspace), skipped=discovery.skipped
        )
        for index, found in enumerate(discovery.manifests):
            file_path = acquired.workspace / found.relative_path
            note = None
            plannable = True
            try:
                if found.kind == "pyproject":
                    content, notes = _pyproject_as_requirements(file_path)
                    plannable = False
                    note = "Read-only: upgrades for pyproject.toml are shown but not written as a patch."
                    if not content.strip():
                        note = "No dependencies declared in a supported pyproject.toml table."
                else:
                    with open(file_path, "rb") as fh:
                        raw = fh.read(repo_svc.MAX_MANIFEST_BYTES + 1)
                    content = raw.decode("utf-8", "replace")
            except Exception as err:
                logger.warning("Could not read %s: %s", found.relative_path, err)
                store.add_manifest(StoredManifest(
                    run_id=run_id, index=index, path=found.relative_path, manifest_type=_analyzer_type(found.kind),
                    kind=found.kind, plannable=False, content="", analysis=None, recommendations=None,
                    proposal_id=None, note=None, error="This file could not be read.",
                ))
                continue
            _analyze_into(store, run_id, index, found.relative_path, found.kind, content, plannable, note)
        run = store.get_run(run_id)
        assert run is not None
        return run
    except Exception:
        shutil.rmtree(acquired.workspace, ignore_errors=True)
        raise


@router.post("/repositories/analyze", response_model=RunResponse, summary="Analyze a public GitHub repository")
async def analyze_repository(
    request: RepositoryAnalyzeRequest,
    store: Annotated[RunStore, Depends(get_run_store)],
) -> RunResponse:
    await run_in_threadpool(maybe_run_retention)
    try:
        return await run_in_threadpool(_analyze_repository, request.url, store)
    except repo_svc.RepositoryError as err:
        raise HTTPException(status_code=err.status_code, detail={"code": err.code, "message": err.message})


@router.post("/runs/manifest", response_model=RunResponse, summary="Analyze a pasted or uploaded manifest")
async def analyze_manifest_run(
    request: ManifestRunRequest,
    store: Annotated[RunStore, Depends(get_run_store)],
) -> RunResponse:
    manifest_type = request.manifest_type.strip().lower()
    if manifest_type not in SUPPORTED_MANUAL_TYPES:
        raise HTTPException(status_code=422, detail="Supported formats are requirements.txt and package.json.")
    if len(request.content.encode("utf-8")) > MAX_MANIFEST_SIZE_BYTES:
        raise HTTPException(status_code=413, detail="The manifest is larger than 1 MB.")
    if not request.content.strip():
        raise HTTPException(status_code=422, detail="The manifest is empty.")
    kind = "package_json" if manifest_type == "package.json" else "requirements"
    await run_in_threadpool(maybe_run_retention)
    try:
        analysis = await run_in_threadpool(manifest_service.analyze_manifest, manifest_type, request.content)
    except ManifestParseError as err:
        raise HTTPException(status_code=422, detail={"message": str(err), "diagnostics": err.diagnostics})

    def persist() -> RunResponse:
        run_id = store.create_run(source="manifest")
        store.add_manifest(StoredManifest(
            run_id=run_id, index=0, path=manifest_type, manifest_type=manifest_type, kind=kind, plannable=True,
            content=request.content, analysis=analysis, recommendations=None, proposal_id=None,
        ))
        run = store.get_run(run_id)
        assert run is not None
        return run

    return await run_in_threadpool(persist)


@router.get("/runs/{run_id}", response_model=RunResponse, summary="Get an analysis run")
async def get_run(run_id: str, store: Annotated[RunStore, Depends(get_run_store)]) -> RunResponse:
    run = await run_in_threadpool(store.get_run, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Analysis not found.")
    return run


def _require_manifest(store: RunStore, run_id: str, index: int) -> StoredManifest:
    m = store.get_manifest(run_id, index)
    if m is None:
        raise HTTPException(status_code=404, detail="Manifest not found in this analysis.")
    if m.analysis is None:
        raise HTTPException(status_code=409, detail=m.error or "This manifest has no analysis.")
    return m


@router.post(
    "/runs/{run_id}/manifests/{index}/recommendations",
    response_model=RecommendationBatchResponse,
    summary="Generate recommendations from the stored analysis",
)
async def run_recommendations(
    run_id: str,
    index: int,
    request: RunRecommendationRequest,
    store: Annotated[RunStore, Depends(get_run_store)],
    service: Annotated[RecommendationService, Depends(get_recommendation_service)],
) -> RecommendationBatchResponse:
    m = await run_in_threadpool(_require_manifest, store, run_id, index)
    batch = RecommendationBatchRequest(
        manifest_analysis=m.analysis,
        preferred_model=request.preferred_model,
        force_deterministic=request.force_deterministic,
    )
    result = await run_in_threadpool(service.generate_recommendations, batch)
    await run_in_threadpool(store.set_recommendations, run_id, index, result)
    return result


@router.post(
    "/runs/{run_id}/manifests/{index}/plan",
    response_model=UpgradeProposalResponse,
    summary="Build an upgrade plan from server-held recommendations",
)
async def run_plan(
    run_id: str,
    index: int,
    request: RunPlanRequest,
    store: Annotated[RunStore, Depends(get_run_store)],
    planner: Annotated[UpgradePlannerService, Depends(get_upgrade_planner)],
) -> UpgradeProposalResponse:
    m = await run_in_threadpool(_require_manifest, store, run_id, index)
    if not m.plannable:
        raise HTTPException(status_code=409, detail="Upgrades for this file are shown for review only.")
    if m.recommendations is None:
        raise HTTPException(status_code=409, detail="Generate recommendations before building a plan.")
    plan_request = UpgradePlanRequest(
        manifest_type=m.manifest_type,
        original_content=m.content,
        recommendations=m.recommendations.recommendations,
        selected_packages=request.selected_packages,
        file_path=m.path,
    )
    proposal = await run_in_threadpool(planner.create_upgrade_proposal, plan_request)
    if proposal.proposal_id:
        await run_in_threadpool(
            store.bind_proposal, proposal.proposal_id, run_id, index, proposal.unified_diff, proposal
        )
    return proposal


@router.post("/proposals/{proposal_id}/approve", response_model=ApprovalResponse, summary="Record explicit approval")
async def approve_proposal(
    proposal_id: str,
    request: ApprovalRequest,
    store: Annotated[RunStore, Depends(get_run_store)],
    registry: Annotated[ProposalRegistry, Depends(get_proposal_registry)],
) -> ApprovalResponse:
    binding = await run_in_threadpool(store.get_binding, proposal_id)
    if binding is None:
        raise HTTPException(status_code=409, detail="Only plans built from an analysis can be approved.")
    stored = await run_in_threadpool(ensure_registered, proposal_id, store, registry)
    if stored is None:
        raise HTTPException(status_code=410, detail="This plan has expired. Build it again.")
    proposal = stored.proposal
    if sha256_text(proposal.unified_diff) != binding.diff_sha256 or request.diff_sha256 != binding.diff_sha256:
        raise HTTPException(status_code=409, detail="The diff you reviewed does not match this plan. Reload and review it again.")
    if not proposal.integrity.is_valid or proposal.total_planned == 0:
        raise HTTPException(status_code=409, detail="This plan cannot be approved: it has no valid changes.")
    try:
        # The server, not the client, decides how this plan will be validated; the approval covers it.
        policy = policy_for_proposal(proposal)
    except ValidationPolicyError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    record = await run_in_threadpool(
        store.record_approval, proposal_id, binding.diff_sha256, request.note, policy.fingerprint
    )
    return ApprovalResponse(
        proposal_id=proposal_id, run_id=binding.run_id, diff_sha256=record.diff_sha256, approved=True,
        approved_at=record.approved_at, note=record.note,
        validation_fingerprint=policy.fingerprint, validation_policy=policy.to_dict(),
    )


@router.get("/proposals/{proposal_id}/approval", response_model=ApprovalResponse, summary="Get approval state")
async def get_approval(proposal_id: str, store: Annotated[RunStore, Depends(get_run_store)]) -> ApprovalResponse:
    binding = await run_in_threadpool(store.get_binding, proposal_id)
    if binding is None:
        raise HTTPException(status_code=404, detail="Plan not found.")
    record = await run_in_threadpool(store.get_approval, proposal_id)
    return ApprovalResponse(
        proposal_id=proposal_id, run_id=binding.run_id, diff_sha256=binding.diff_sha256,
        approved=record is not None, approved_at=record.approved_at if record else None,
        note=record.note if record else None,
        validation_fingerprint=record.validation_fingerprint if record else None,
    )
