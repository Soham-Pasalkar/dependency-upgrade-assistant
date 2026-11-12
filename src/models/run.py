"""Server-authoritative analysis run models (FR-15, FR-16)."""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from src.models.manifest import ManifestAnalysisResponse
from src.models.recommendation import RecommendationBatchResponse


class RepositoryInfo(BaseModel):
    full_name: str
    url: str
    commit_sha: Optional[str] = None
    default_branch: Optional[str] = None
    description: Optional[str] = None
    files_scanned: int = 0
    bytes_on_disk: int = 0


class RunManifest(BaseModel):
    index: int
    path: str = Field(..., description="Path relative to the repository root, or the manifest name")
    manifest_type: str = Field(..., description="Analyzer format: 'requirements.txt' or 'package.json'")
    kind: Literal["requirements", "pyproject", "package_json"]
    ecosystem: Optional[str] = None
    plannable: bool = True
    note: Optional[str] = None
    error: Optional[str] = None
    analysis: Optional[ManifestAnalysisResponse] = None
    recommendations: Optional[RecommendationBatchResponse] = None
    proposal_id: Optional[str] = None


class RunResponse(BaseModel):
    run_id: str
    source: Literal["github", "manifest"]
    created_at: float
    repository: Optional[RepositoryInfo] = None
    manifests: List[RunManifest] = Field(default_factory=list)
    skipped: List[str] = Field(default_factory=list)
    has_workspace: bool = False


class RepositoryAnalyzeRequest(BaseModel):
    url: str = Field(..., max_length=300)


class ManifestRunRequest(BaseModel):
    manifest_type: str = Field(..., max_length=64)
    content: str
    file_path: Optional[str] = Field(default=None, max_length=255)


class RunRecommendationRequest(BaseModel):
    preferred_model: Optional[str] = Field(default=None, max_length=200)
    force_deterministic: bool = False


class RunPlanRequest(BaseModel):
    selected_packages: Optional[List[str]] = None


class ApprovalRequest(BaseModel):
    diff_sha256: str = Field(..., min_length=64, max_length=64)
    note: Optional[str] = Field(default=None, max_length=500)


class ApprovalResponse(BaseModel):
    proposal_id: str
    run_id: str
    diff_sha256: str
    approved: bool
    approved_at: Optional[float] = None
    note: Optional[str] = None
    validation_fingerprint: Optional[str] = Field(
        default=None, description="SHA-256 of the server-controlled validation policy covered by this approval"
    )
    validation_policy: Optional[dict] = Field(
        default=None, description="Validation policy (image, limits, discovery rules) the approval covers"
    )
    resolution_fingerprint: Optional[str] = Field(
        default=None, description="Supervisor fingerprint of the verified COMPATIBLE proposed resolution approved"
    )
    resolution_status: Optional[str] = Field(default=None, description="Resolution status covered by this approval")
