import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { BackendStatus } from './BackendStatus';
import * as apiClient from '../api/client';

describe('BackendStatus Component', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('renders loading state initially', () => {
    vi.spyOn(apiClient, 'getHealth').mockImplementation(
      () => new Promise(() => {}) // never resolves
    );

    render(<BackendStatus />);
    expect(screen.getByTestId('status-loading')).toBeInTheDocument();
    expect(screen.getByText(/Connecting to backend API/i)).toBeInTheDocument();
  });

  it('renders successful backend response with healthy status', async () => {
    const mockHealth: apiClient.HealthResponse = {
      status: 'healthy',
      app_name: 'Dependency Upgrade Assistant',
      version: '0.1.0',
      environment: 'development',
      database: {
        status: 'connected',
        path: 'dua_local.db',
        message: 'connected',
      },
      timestamp: '2026-11-10T19:00:00Z',
    };

    vi.spyOn(apiClient, 'getHealth').mockResolvedValue(mockHealth);

    render(<BackendStatus />);

    await waitFor(() => {
      expect(screen.getByTestId('status-success')).toBeInTheDocument();
    });

    expect(screen.getByText('HEALTHY')).toBeInTheDocument();
    expect(screen.getByText('Dependency Upgrade Assistant')).toBeInTheDocument();
    expect(screen.getByText('0.1.0')).toBeInTheDocument();
    expect(screen.getByText(/connected \(connected\)/i)).toBeInTheDocument();
    expect(screen.getByText('dua_local.db')).toBeInTheDocument();
  });

  it('renders backend failure state with error details and hint', async () => {
    vi.spyOn(apiClient, 'getHealth').mockRejectedValue(
      new apiClient.ApiError('Failed to connect to backend at /api/health: Network error')
    );

    render(<BackendStatus />);

    await waitFor(() => {
      expect(screen.getByTestId('status-error')).toBeInTheDocument();
    });

    expect(screen.getByText('Disconnected')).toBeInTheDocument();
    expect(screen.getByText(/Network error/i)).toBeInTheDocument();
    expect(
      screen.getByText(/Ensure the FastAPI backend server is running on/i)
    ).toBeInTheDocument();
  });

  it('allows refreshing backend status on button click', async () => {
    const getHealthMock = vi.spyOn(apiClient, 'getHealth').mockResolvedValue({
      status: 'healthy',
      app_name: 'Dependency Upgrade Assistant',
      version: '0.1.0',
      environment: 'development',
      database: {
        status: 'connected',
        path: 'dua_local.db',
        message: 'connected',
      },
      timestamp: '2026-11-10T19:00:00Z',
    });

    render(<BackendStatus />);

    await waitFor(() => {
      expect(screen.getByTestId('status-success')).toBeInTheDocument();
    });

    const refreshBtn = screen.getByTestId('refresh-status-button');
    await userEvent.click(refreshBtn);

    expect(getHealthMock).toHaveBeenCalledTimes(2);
  });
});
