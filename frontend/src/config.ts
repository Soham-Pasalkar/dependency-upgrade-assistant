/**
 * Frontend Configuration for Dependency Upgrade Assistant 2.0
 */

export const config = {
  apiBaseUrl: import.meta.env.VITE_API_BASE_URL || '',
  isDevelopment: import.meta.env.DEV,
};
