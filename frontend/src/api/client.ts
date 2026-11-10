/**
 * Centralized API Client for Dependency Upgrade Assistant 2.0
 */

import { config } from '../config';

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
