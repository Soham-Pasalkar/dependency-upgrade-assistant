/**
 * Centralized API Client for Dependency Upgrade Assistant 2.0
 */

import { config } from '../config';

export type Ecosystem = 'python' | 'npm';
export type DependencyType = 'runtime' | 'development';
export type DependencyStatus = 'UP_TO_DATE' | 'UPGRADE_AVAILABLE' | 'PRESERVED';
export type RegistryLookupStatus = 'SUCCESS' | 'NOT_FOUND' | 'RATE_LIMITED' | 'NETWORK_ERROR' | 'SKIPPED';

export type RecommendationCategory =
  | 'RECOMMENDED'
  | 'CAUTION'
  | 'NO_UPGRADE_NEEDED'
  | 'PRESERVED_UNSUPPORTED';

export type RecommendationPriority = 'CRITICAL' | 'HIGH' | 'MEDIUM' | 'LOW';

export type ValidationStatus =
  | 'VALIDATED'
  | 'REJECTED_HALLUCINATION'
  | 'REJECTED_INCOMPATIBLE'
  | 'FALLBACK_DETERMINISTIC'
  | 'SKIPPED';

export interface ModelMetadata {
  name: string;
  model?: string | null;
  size_bytes?: number;
  modified_at?: string | null;
  digest?: string | null;
  family?: string | null;
  parameter_size?: string | null;
  display_label?: string | null;
}

export interface PackageUpgradeRecommendation {
  package_name: string;
  ecosystem: Ecosystem;
  current_version: string | null;
  declared_specifier: string;
  latest_registry_version: string | null;
  candidate_version: string | null;
  category: RecommendationCategory;
  priority: RecommendationPriority;
  explanation: string;
  breaking_changes_summary: string | null;
  confidence_score: number;
  provider_used: string;
  model_used: string | null;
  is_ai_generated: boolean;
  validation_status: ValidationStatus;
  raw_line: string | null;
  line_number: number | null;
}

export interface RecommendationBatchRequest {
  manifest_analysis: ManifestAnalysisResponse;
  preferred_model?: string | null;
  force_deterministic?: boolean;
}

export interface RecommendationBatchResponse {
  manifest_type: string;
  ecosystem: Ecosystem;
  recommendations: PackageUpgradeRecommendation[];
  provider_used: string;
  model_used: string | null;
  is_offline_fallback: boolean;
  total_upgrades_recommended: number;
  total_caution: number;
  summary: string;
}

export interface AIProviderStatusResponse {
  is_available: boolean;
  provider_name: string;
  endpoint: string;
  discovered_models: ModelMetadata[];
  default_model: string | null;
  message: string;
}

export interface DatabaseHealth {
  status: string;
  path: string;
  message: string;
}

export interface HealthResponse {
  status: 'healthy' | 'degraded';
  app_name: string;
  version: string;
  environment: string;
  database: DatabaseHealth;
  timestamp: string;
}

export interface AnalyzedDependency {
  name: string;
  original_name: string;
  ecosystem: Ecosystem;
  dependency_type: DependencyType;
  declared_specifier: string;
  current_version: string | null;
  latest_version: string | null;
  latest_compatible_version: string | null;
  status: DependencyStatus;
  status_reason: string;
  registry_status: RegistryLookupStatus;
  available_versions: string[];
  yanked_or_deprecated: boolean;
  deprecation_reason: string | null;
  line_number: number | null;
  raw_line: string | null;
  diagnostic_message: string | null;
}

export interface ManifestAnalysisRequest {
  manifest_type: string;
  content: string;
}

export interface ManifestAnalysisResponse {
  manifest_type: string;
  ecosystem: Ecosystem;
  total_dependencies: number;
  up_to_date_count: number;
  upgrade_available_count: number;
  preserved_count: number;
  dependencies: AnalyzedDependency[];
  diagnostics: string[];
}

export class ApiError extends Error {
  statusCode?: number;
  details?: unknown;

  constructor(message: string, statusCode?: number, details?: unknown) {
    super(message);
    this.name = 'ApiError';
    this.statusCode = statusCode;
    this.details = details;
  }
}

/**
 * Fetch application and database health status from the backend.
 */
export async function getHealth(): Promise<HealthResponse> {
  const url = `${config.apiBaseUrl}/api/health`;

  try {
    const response = await fetch(url, {
      method: 'GET',
      headers: {
        'Accept': 'application/json',
      },
    });

    if (!response.ok) {
      const errorText = await response.text();
      let errorData;
      try {
        errorData = JSON.parse(errorText);
      } catch {
        errorData = errorText;
      }
      throw new ApiError(
        `Backend returned HTTP ${response.status}: ${response.statusText}`,
        response.status,
        errorData
      );
    }

    const data: HealthResponse = await response.json();
    return data;
  } catch (error) {
    if (error instanceof ApiError) {
      throw error;
    }
    const message = error instanceof Error ? error.message : 'Unknown connection error';
    throw new ApiError(`Failed to connect to backend at ${url}: ${message}`);
  }
}

/**
 * Analyze raw manifest text (requirements.txt or package.json).
 */
export async function analyzeManifest(
  request: ManifestAnalysisRequest
): Promise<ManifestAnalysisResponse> {
  const url = `${config.apiBaseUrl}/api/manifest/analyze`;

  try {
    const response = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
      },
      body: JSON.stringify(request),
    });

    if (!response.ok) {
      const errorText = await response.text();
      let errorData;
      try {
        errorData = JSON.parse(errorText);
      } catch {
        errorData = errorText;
      }
      const message =
        typeof errorData === 'object' && errorData?.detail
          ? String(errorData.detail)
          : `Manifest analysis failed (${response.status})`;
      throw new ApiError(message, response.status, errorData);
    }

    return await response.json();
  } catch (error) {
    if (error instanceof ApiError) {
      throw error;
    }
    const message = error instanceof Error ? error.message : 'Unknown error';
    throw new ApiError(`Failed to analyze manifest: ${message}`);
  }
}

/**
 * Upload and analyze a manifest file via multipart/form-data.
 */
export async function uploadManifest(
  file: File,
  manifestType?: string
): Promise<ManifestAnalysisResponse> {
  const url = `${config.apiBaseUrl}/api/manifest/upload`;
  const formData = new FormData();
  formData.append('file', file);
  if (manifestType) {
    formData.append('manifest_type', manifestType);
  }

  try {
    const response = await fetch(url, {
      method: 'POST',
      headers: {
        'Accept': 'application/json',
      },
      body: formData,
    });

    if (!response.ok) {
      const errorText = await response.text();
      let errorData;
      try {
        errorData = JSON.parse(errorText);
      } catch {
        errorData = errorText;
      }
      const message =
        typeof errorData === 'object' && errorData?.detail
          ? String(errorData.detail)
          : `Manifest upload failed (${response.status})`;
      throw new ApiError(message, response.status, errorData);
    }

    return await response.json();
  } catch (error) {
    if (error instanceof ApiError) {
      throw error;
    }
    const message = error instanceof Error ? error.message : 'Unknown error';
    throw new ApiError(`Failed to upload manifest: ${message}`);
  }
}

/**
 * Check AI provider connectivity and discovered models.
 */
export async function getAiStatus(): Promise<AIProviderStatusResponse> {
  const url = `${config.apiBaseUrl}/api/ai/status`;

  try {
    const response = await fetch(url, {
      method: 'GET',
      headers: {
        'Accept': 'application/json',
      },
    });

    if (!response.ok) {
      throw new ApiError(`AI status check failed (${response.status})`, response.status);
    }

    return await response.json();
  } catch (error) {
    if (error instanceof ApiError) {
      throw error;
    }
    const message = error instanceof Error ? error.message : 'Unknown error';
    return {
      is_available: false,
      provider_name: 'ollama',
      endpoint: 'http://127.0.0.1:11434',
      discovered_models: [],
      default_model: null,
      message: `Offline (${message})`,
    };
  }
}

/**
 * Generate upgrade recommendations for analyzed manifest dependencies.
 */
export async function generateRecommendations(
  request: RecommendationBatchRequest
): Promise<RecommendationBatchResponse> {
  const url = `${config.apiBaseUrl}/api/recommendations/generate`;

  try {
    const response = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
      },
      body: JSON.stringify(request),
    });

    if (!response.ok) {
      const errorText = await response.text();
      let errorData;
      try {
        errorData = JSON.parse(errorText);
      } catch {
        errorData = errorText;
      }
      const message =
        typeof errorData === 'object' && errorData?.detail
          ? String(errorData.detail)
          : `Recommendation generation failed (${response.status})`;
      throw new ApiError(message, response.status, errorData);
    }

    return await response.json();
  } catch (error) {
    if (error instanceof ApiError) {
      throw error;
    }
    const message = error instanceof Error ? error.message : 'Unknown error';
    throw new ApiError(`Failed to generate recommendations: ${message}`);
  }
}

// ----------------------------------------------------------------------
// M4: Upgrade Planning & Patch Generation Types and Functions
// ----------------------------------------------------------------------

export type UpgradeAction =
  | 'UPGRADE'
  | 'RETAIN'
  | 'REJECT_CONFLICT'
  | 'REJECT_UNSUPPORTED'
  | 'REJECT_STALE';

export type ProposalStatus =
  | 'READY_FOR_REVIEW'
  | 'APPROVED'
  | 'REJECTED'
  | 'INVALID_PATCH'
  | 'CONFLICT_DETECTED'
  | 'NO_OP';

export interface PlannedUpgradeItem {
  package_name: string;
  ecosystem: Ecosystem;
  current_version?: string | null;
  target_version?: string | null;
  declared_specifier: string;
  proposed_specifier: string;
  action: UpgradeAction;
  rationale: string;
  recommendation_ref?: string | null;
  line_number?: number | null;
  dependency_type: DependencyType;
  is_conflict: boolean;
  conflict_details?: string | null;
}

export interface PatchIntegrityResult {
  is_valid: boolean;
  can_apply: boolean;
  can_reverse: boolean;
  syntax_valid: boolean;
  target_versions_match: boolean;
  unrelated_dependencies_unchanged: boolean;
  original_unchanged: boolean;
  checks_passed: string[];
  checks_failed: string[];
  errors: string[];
  warnings: string[];
}

export interface UpgradeProposalResponse {
  proposal_id: string;
  manifest_type: string;
  ecosystem: Ecosystem;
  file_path: string;
  plan_items: PlannedUpgradeItem[];
  total_planned: number;
  total_unchanged: number;
  total_conflicts: number;
  original_content: string;
  proposed_content: string;
  unified_diff: string;
  integrity: PatchIntegrityResult;
  status: ProposalStatus;
  requires_human_approval: boolean;
  diagnostics: string[];
  summary: string;
}

export interface UpgradePlanRequest {
  manifest_type: string;
  original_content: string;
  recommendations: PackageUpgradeRecommendation[];
  selected_packages?: string[] | null;
  file_path?: string | null;
}

/**
 * Generate an upgrade plan and unified diff from validated recommendations.
 */
export async function generateUpgradePlan(
  request: UpgradePlanRequest
): Promise<UpgradeProposalResponse> {
  const url = `${config.apiBaseUrl}/api/plan/generate`;

  try {
    const response = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
      },
      body: JSON.stringify(request),
    });

    if (!response.ok) {
      const errorText = await response.text();
      let errorData;
      try {
        errorData = JSON.parse(errorText);
      } catch {
        errorData = errorText;
      }
      const message =
        typeof errorData === 'object' && errorData?.detail
          ? String(errorData.detail)
          : `Upgrade plan generation failed (${response.status})`;
      throw new ApiError(message, response.status, errorData);
    }

    return await response.json();
  } catch (error) {
    if (error instanceof ApiError) {
      throw error;
    }
    const message = error instanceof Error ? error.message : 'Unknown error';
    throw new ApiError(`Failed to generate upgrade plan: ${message}`);
  }
}

// ----------------------------------------------------------------------
// M5: Sandboxed Validation Types and Functions
// ----------------------------------------------------------------------

export type SandboxValidationStatus =
  | 'PASSED'
  | 'FAILED'
  | 'NO_TESTS_FOUND'
  | 'TIMED_OUT'
  | 'CANCELLED'
  | 'INFRASTRUCTURE_ERROR'
  | 'NOT_RUNNABLE';

export interface ValidationCommandResult {
  command_name: string;
  command_args: string[];
  status: SandboxValidationStatus;
  exit_code: number;
  stdout: string;
  stderr: string;
  duration_seconds: number;
  description: string;
  error_message?: string | null;
}

export interface SandboxExecutionRequest {
  proposal_id?: string | null;
  manifest_type?: string | null;
  custom_test_cmd?: string[] | null;
  timeout_seconds?: number;
  memory_limit_mb?: number;
  cpu_limit?: string;
  pids_limit?: number;
  image_override?: string | null;
  ecosystem?: string | null;
}

export interface ValidationReportResponse {
  execution_id: string;
  proposal_id?: string | null;
  status: SandboxValidationStatus;
  is_runnable: boolean;
  summary: string;
  commands: ValidationCommandResult[];
  total_duration_seconds: number;
  image_used: string;
  image_digest?: string | null;
  network_enabled: boolean;
  resource_limits: Record<string, unknown>;
  logs: string;
  is_truncated?: boolean;
  test_discovery_status?: string | null;
  staging_status?: string | null;
  snapshot_files_count?: number;
  snapshot_excluded_count?: number;
  created_at: number;
  completed_at?: number | null;
  diagnostics: string[];
}

export interface SandboxExecutionLogResponse {
  execution_id: string;
  status: SandboxValidationStatus;
  logs: string;
  truncated: boolean;
}

export interface SandboxStatusResponse {
  is_available: boolean;
  version?: string | null;
  error_message?: string | null;
}

/**
 * Check Docker sandbox availability and daemon version.
 */
export async function getSandboxStatus(): Promise<SandboxStatusResponse> {
  const url = `${config.apiBaseUrl}/api/validation/status`;

  try {
    const response = await fetch(url, {
      method: 'GET',
      headers: {
        'Accept': 'application/json',
      },
    });

    if (!response.ok) {
      throw new ApiError(`Sandbox status check failed (${response.status})`, response.status);
    }

    return await response.json();
  } catch (error) {
    if (error instanceof ApiError) {
      throw error;
    }
    const message = error instanceof Error ? error.message : 'Unknown error';
    return {
      is_available: false,
      version: null,
      error_message: `Sandbox probe failed: ${message}`,
    };
  }
}

/**
 * Execute sandboxed validation for a proposal or snapshot.
 */
export async function executeValidation(
  request: SandboxExecutionRequest
): Promise<ValidationReportResponse> {
  const url = `${config.apiBaseUrl}/api/validation/execute`;

  try {
    const response = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
      },
      body: JSON.stringify(request),
    });

    if (!response.ok) {
      const errorText = await response.text();
      let errorData;
      try {
        errorData = JSON.parse(errorText);
      } catch {
        errorData = errorText;
      }
      const message =
        typeof errorData === 'object' && errorData?.detail
          ? String(errorData.detail)
          : `Validation execution failed (${response.status})`;
      throw new ApiError(message, response.status, errorData);
    }

    return await response.json();
  } catch (error) {
    if (error instanceof ApiError) {
      throw error;
    }
    const message = error instanceof Error ? error.message : 'Unknown error';
    throw new ApiError(`Failed to execute validation: ${message}`);
  }
}

/**
 * Retrieve report for a validation execution by ID.
 */
export async function getValidationReport(
  executionId: string
): Promise<ValidationReportResponse> {
  const url = `${config.apiBaseUrl}/api/validation/${encodeURIComponent(executionId)}`;

  try {
    const response = await fetch(url, {
      method: 'GET',
      headers: {
        'Accept': 'application/json',
      },
    });

    if (!response.ok) {
      throw new ApiError(`Failed to retrieve validation report (${response.status})`, response.status);
    }

    return await response.json();
  } catch (error) {
    if (error instanceof ApiError) {
      throw error;
    }
    const message = error instanceof Error ? error.message : 'Unknown error';
    throw new ApiError(`Failed to get validation report: ${message}`);
  }
}

/**
 * Retrieve execution logs.
 */
export async function getValidationLogs(
  executionId: string
): Promise<SandboxExecutionLogResponse> {
  const url = `${config.apiBaseUrl}/api/validation/${encodeURIComponent(executionId)}/logs`;

  try {
    const response = await fetch(url, {
      method: 'GET',
      headers: {
        'Accept': 'application/json',
      },
    });

    if (!response.ok) {
      throw new ApiError(`Failed to retrieve validation logs (${response.status})`, response.status);
    }

    return await response.json();
  } catch (error) {
    if (error instanceof ApiError) {
      throw error;
    }
    const message = error instanceof Error ? error.message : 'Unknown error';
    throw new ApiError(`Failed to get validation logs: ${message}`);
  }
}

/**
 * Cancel an in-flight validation execution.
 */
export async function cancelValidationExecution(
  executionId: string
): Promise<{ success: boolean; execution_id: string; message: string }> {
  const url = `${config.apiBaseUrl}/api/validation/${encodeURIComponent(executionId)}/cancel`;

  try {
    const response = await fetch(url, {
      method: 'POST',
      headers: {
        'Accept': 'application/json',
      },
    });

    if (!response.ok) {
      throw new ApiError(`Failed to cancel validation (${response.status})`, response.status);
    }

    return await response.json();
  } catch (error) {
    if (error instanceof ApiError) {
      throw error;
    }
    const message = error instanceof Error ? error.message : 'Unknown error';
    throw new ApiError(`Failed to cancel validation: ${message}`);
  }
}

// ============================================================================
// M6: Deliverables Export Types & API Client
// ============================================================================

export type ArtifactType =
  | 'patch'
  | 'summary_json'
  | 'execution_log'
  | 'checksums'
  | 'bundle_zip';

export interface ExportItemMetadata {
  artifact_type: ArtifactType;
  filename: string;
  mime_type: string;
  size_bytes: number;
  sha256: string;
  download_url: string;
}

export interface ExportRequest {
  proposal_id?: string;
  execution_id?: string;
  run_id?: string;
  include_logs?: boolean;
  custom_notes?: string;
}

export interface ExportMetadataResponse {
  export_id: string;
  proposal_id?: string;
  execution_id?: string;
  run_id?: string;
  created_at: number;
  sanitized: boolean;
  secret_scan_passed: boolean;
  validation_status?: string;
  items: ExportItemMetadata[];
  summary: Record<string, any>;
}

/**
 * Generate verified deliverables for a proposal and validation execution.
 */
export async function createExport(request: ExportRequest): Promise<ExportMetadataResponse> {
  const url = `${config.apiBaseUrl}/api/export`;

  try {
    const response = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
      },
      body: JSON.stringify(request),
    });

    if (!response.ok) {
      const errorData = await response.json().catch(() => null);
      const detail = errorData?.detail || `HTTP ${response.status}`;
      throw new ApiError(`Export generation failed: ${detail}`, response.status);
    }

    return await response.json();
  } catch (error) {
    if (error instanceof ApiError) {
      throw error;
    }
    const message = error instanceof Error ? error.message : 'Unknown error';
    throw new ApiError(`Failed to create export: ${message}`);
  }
}

/**
 * Retrieve metadata and checksums for an existing export bundle.
 */
export async function getExportMetadata(exportId: string): Promise<ExportMetadataResponse> {
  const url = `${config.apiBaseUrl}/api/export/${encodeURIComponent(exportId)}`;

  try {
    const response = await fetch(url, {
      method: 'GET',
      headers: {
        'Accept': 'application/json',
      },
    });

    if (!response.ok) {
      throw new ApiError(`Failed to retrieve export metadata (${response.status})`, response.status);
    }

    return await response.json();
  } catch (error) {
    if (error instanceof ApiError) {
      throw error;
    }
    const message = error instanceof Error ? error.message : 'Unknown error';
    throw new ApiError(`Failed to get export metadata: ${message}`);
  }
}

/**
 * Construct download URL for an export artifact.
 */
export function getArtifactDownloadUrl(exportId: string, artifactType: ArtifactType | string): string {
  return `${config.apiBaseUrl}/api/export/${encodeURIComponent(exportId)}/download/${encodeURIComponent(artifactType)}`;
}

