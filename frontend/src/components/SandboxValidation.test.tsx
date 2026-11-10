import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { SandboxValidation } from './SandboxValidation';
import * as apiClient from '../api/client';

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof apiClient>('../api/client');
  return {
    ...actual,
    getSandboxStatus: vi.fn(),
    executeValidation: vi.fn(),
    cancelValidationExecution: vi.fn(),
  };
});

describe('SandboxValidation Component', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders title, containment specs, and offline status when Docker is unavailable', async () => {
    vi.mocked(apiClient.getSandboxStatus).mockResolvedValueOnce({
      is_available: false,
      version: null,
      error_message: 'Docker binary not found on host.',
    });

    render(<SandboxValidation manifestType="requirements.txt" />);

    expect(screen.getByText('Sandboxed Container Validation')).toBeInTheDocument();
    expect(screen.getAllByText('--network none').length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText('1000:1000 (non-root)')).toBeInTheDocument();
    expect(screen.getByText('--cap-drop=ALL')).toBeInTheDocument();

    await waitFor(() => {
      expect(screen.getByText(/Docker Offline/i)).toBeInTheDocument();
    });
  });

  it('displays active Docker version badge when Docker is available', async () => {
    vi.mocked(apiClient.getSandboxStatus).mockResolvedValueOnce({
      is_available: true,
      version: '26.1.0',
      error_message: null,
    });

    render(<SandboxValidation manifestType="requirements.txt" />);

    await waitFor(() => {
      expect(screen.getByText(/Docker Engine Active \(v26\.1\.0\)/i)).toBeInTheDocument();
    });
  });

  it('executes validation and renders report with NOT_RUNNABLE status', async () => {
    vi.mocked(apiClient.getSandboxStatus).mockResolvedValueOnce({
      is_available: false,
      version: null,
      error_message: 'Docker daemon is not running.',
    });

    const mockReport: apiClient.ValidationReportResponse = {
      execution_id: 'val-test-123',
      proposal_id: undefined,
      status: 'NOT_RUNNABLE',
      is_runnable: false,
      summary: 'Docker sandbox unavailable: Docker daemon is not running.',
      commands: [],
      total_duration_seconds: 0.05,
      image_used: 'python:3.11-slim',
      network_enabled: false,
      resource_limits: { memory_limit_mb: 512, cpu_limit: '1.0', pids_limit: 64 },
      logs: '[DIAGNOSTIC] Docker daemon is not running.\n',
      created_at: Date.now() / 1000,
      completed_at: Date.now() / 1000,
      diagnostics: ['Docker daemon is not running.'],
    };

    vi.mocked(apiClient.executeValidation).mockResolvedValueOnce(mockReport);

    render(<SandboxValidation manifestType="requirements.txt" />);

    fireEvent.click(screen.getByTestId('btn-run-validation'));

    await waitFor(() => {
      expect(screen.getByTestId('validation-results')).toBeInTheDocument();
    });

    expect(screen.getByTestId('validation-status-badge')).toHaveTextContent('NOT RUNNABLE');
    expect(screen.getByTestId('validation-summary-text')).toHaveTextContent(
      'Docker sandbox unavailable: Docker daemon is not running.'
    );
    expect(screen.getByTestId('validation-logs-text')).toHaveTextContent(
      '[DIAGNOSTIC] Docker daemon is not running.'
    );
  });

  it('executes validation and displays PASSED status with command breakdown', async () => {
    vi.mocked(apiClient.getSandboxStatus).mockResolvedValueOnce({
      is_available: true,
      version: '25.0.3',
      error_message: null,
    });

    const mockReport: apiClient.ValidationReportResponse = {
      execution_id: 'val-test-passed',
      proposal_id: undefined,
      status: 'PASSED',
      is_runnable: true,
      summary: 'All 2 validation checks passed successfully inside isolated sandbox.',
      commands: [
        {
          command_name: 'py_compile',
          command_args: ['python', '-m', 'compileall', '-q', '.'],
          status: 'PASSED',
          exit_code: 0,
          stdout: 'Bytecode compiled successfully.\n',
          stderr: '',
          duration_seconds: 0.45,
          description: 'Python bytecode syntax compilation.',
          error_message: null,
        },
        {
          command_name: 'pytest',
          command_args: ['pytest', '-q'],
          status: 'PASSED',
          exit_code: 0,
          stdout: '3 passed in 0.12s\n',
          stderr: '',
          duration_seconds: 1.25,
          description: 'Python test suite execution.',
          error_message: null,
        },
      ],
      total_duration_seconds: 1.7,
      image_used: 'python:3.11-slim',
      network_enabled: false,
      resource_limits: { memory_limit_mb: 512, cpu_limit: '1.0', pids_limit: 64 },
      logs: '>>> Running py_compile: python -m compileall -q .\n>>> Running pytest: pytest -q\n3 passed in 0.12s\n',
      created_at: Date.now() / 1000,
      completed_at: Date.now() / 1000,
      diagnostics: [],
    };

    vi.mocked(apiClient.executeValidation).mockResolvedValueOnce(mockReport);

    render(<SandboxValidation manifestType="requirements.txt" />);

    fireEvent.click(screen.getByTestId('btn-run-validation'));

    await waitFor(() => {
      expect(screen.getByTestId('validation-results')).toBeInTheDocument();
    });

    expect(screen.getByTestId('validation-status-badge')).toHaveTextContent('PASSED');
    expect(screen.getByTestId('validation-commands-table')).toBeInTheDocument();
    expect(screen.getByText('py_compile')).toBeInTheDocument();
    expect(screen.getByText('pytest')).toBeInTheDocument();
  });
});
