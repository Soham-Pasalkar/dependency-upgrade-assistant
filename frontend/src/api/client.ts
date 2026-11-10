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
