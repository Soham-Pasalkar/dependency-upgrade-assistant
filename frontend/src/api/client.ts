/**
 * Centralized API Client for Dependency Upgrade Assistant 2.0
 */

import { config } from '../config';

export type Ecosystem = 'python' | 'npm';
export type DependencyType = 'runtime' | 'development';
export type DependencyStatus = 'UP_TO_DATE' | 'UPGRADE_AVAILABLE' | 'PRESERVED';
export type RegistryLookupStatus = 'SUCCESS' | 'NOT_FOUND' | 'RATE_LIMITED' | 'NETWORK_ERROR' | 'SKIPPED';

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
