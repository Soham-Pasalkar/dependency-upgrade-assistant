import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { BackendStatus } from './BackendStatus';
import * as apiClient from '../api/client';

const healthy: apiClient.HealthResponse = {
  status: 'healthy',
  app_name: 'Dependency Upgrade Assistant',
  version: '0.1.0',
  environment: 'development',
  database: { status: 'connected', path: 'dua_local.db', message: 'connected' },
  timestamp: '2026-11-10T19:00:00Z',
};

describe('BackendStatus Component', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('renders loading state initially', () => {
    vi.spyOn(apiClient, 'getHealth').mockImplementation(() => new Promise(() => {}));
    render(<BackendStatus />);
    expect(screen.getByTestId('status-loading')).toHaveTextContent('Connecting');
  });

  it('renders a healthy service without exposing internal details', async () => {
    vi.spyOn(apiClient, 'getHealth').mockResolvedValue(healthy);
    render(<BackendStatus />);

    await waitFor(() => expect(screen.getByTestId('status-success')).toBeInTheDocument());
    expect(screen.getByTestId('status-success')).toHaveTextContent('Service online');
    expect(screen.queryByText(/dua_local\.db/)).not.toBeInTheDocument();
  });

  it('renders a degraded service distinctly', async () => {
    vi.spyOn(apiClient, 'getHealth').mockResolvedValue({ ...healthy, status: 'degraded' });
    render(<BackendStatus />);
    await waitFor(() => expect(screen.getByTestId('status-success')).toHaveTextContent('Service degraded'));
  });

  it('renders failure state and retries on request', async () => {
    const getHealthMock = vi
      .spyOn(apiClient, 'getHealth')
      .mockRejectedValueOnce(new apiClient.ApiError('Network error'))
      .mockResolvedValueOnce(healthy);

    render(<BackendStatus />);
    await waitFor(() => expect(screen.getByTestId('status-error')).toHaveTextContent('Service unavailable'));

    await userEvent.click(screen.getByTestId('refresh-status-button'));
    await waitFor(() => expect(screen.getByTestId('status-success')).toBeInTheDocument());
    expect(getHealthMock).toHaveBeenCalledTimes(2);
  });
});
