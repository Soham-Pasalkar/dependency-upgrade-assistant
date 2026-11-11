import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { ExportManager } from './ExportManager';
import * as apiClient from '../api/client';

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof apiClient>('../api/client');
  return {
    ...actual,
    createExport: vi.fn(),
    getExportMetadata: vi.fn(),
  };
});

describe('ExportManager Component', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  const mockExportResponse: apiClient.ExportMetadataResponse = {
    export_id: 'exp_test_abc123',
    proposal_id: 'prop_test_456',
    execution_id: 'exec_test_789',
    created_at: 1731300000,
    sanitized: true,
    secret_scan_passed: true,
    validation_status: 'PASSED',
    items: [
      {
        artifact_type: 'patch',
        filename: 'requirements.txt-upgrade.patch',
        mime_type: 'text/x-diff; charset=utf-8',
        size_bytes: 512,
        sha256: 'a1b2c3d4e5f60718293a4b5c6d7e8f90123456789abcdef0123456789abcdef0',
        download_url: '/api/export/exp_test_abc123/download/patch',
      },
      {
        artifact_type: 'summary_json',
        filename: 'summary.json',
        mime_type: 'application/json',
        size_bytes: 1024,
        sha256: 'b2c3d4e5f60718293a4b5c6d7e8f90123456789abcdef0123456789abcdef01',
        download_url: '/api/export/exp_test_abc123/download/summary_json',
      },
      {
        artifact_type: 'bundle_zip',
        filename: 'requirements.txt-upgrade-bundle.zip',
        mime_type: 'application/zip',
        size_bytes: 2048,
        sha256: 'c3d4e5f60718293a4b5c6d7e8f90123456789abcdef0123456789abcdef012',
        download_url: '/api/export/exp_test_abc123/download/bundle_zip',
      },
    ],
    summary: {
      total_upgrades: 1,
      validation_status: 'PASSED',
    },
  };

  it('renders section title, zero-credential badge, and disabled state when no proposalId', () => {
    render(<ExportManager proposalId={null} />);

    expect(screen.getByText('Deliverables Export & Checksum Verification')).toBeInTheDocument();
    expect(screen.getByTestId('zero-secret-badge')).toHaveTextContent('Zero Credential Leakage Verified');
    expect(screen.getByTestId('generate-export-button')).toBeDisabled();
    expect(screen.getByText(/Generate an upgrade proposal above/i)).toBeInTheDocument();
  });

  it('renders validation status badge truthfully when supplied', () => {
    render(<ExportManager proposalId="prop_1" validationStatus="NOT_RUNNABLE" />);

    const badge = screen.getByTestId('export-val-status');
    expect(badge).toHaveTextContent('Validation: NOT_RUNNABLE');
  });

  it('triggers createExport on button click and displays artifact items with checksums', async () => {
    vi.mocked(apiClient.createExport).mockResolvedValueOnce(mockExportResponse);

    render(
      <ExportManager
        proposalId="prop_test_456"
        executionId="exec_test_789"
        validationStatus="PASSED"
      />
    );

    const button = screen.getByTestId('generate-export-button');
    expect(button).not.toBeDisabled();
    fireEvent.click(button);

    await waitFor(() => {
      expect(screen.getByTestId('export-results-card')).toBeInTheDocument();
    });

    expect(apiClient.createExport).toHaveBeenCalledWith({
      proposal_id: 'prop_test_456',
      execution_id: 'exec_test_789',
      include_logs: true,
    });

    expect(screen.getByText('requirements.txt-upgrade.patch')).toBeInTheDocument();
    expect(screen.getByText('summary.json')).toBeInTheDocument();
    expect(screen.getByText('requirements.txt-upgrade-bundle.zip')).toBeInTheDocument();

    const patchDownloadBtn = screen.getByTestId('download-button-patch');
    expect(patchDownloadBtn).toHaveAttribute('download', 'requirements.txt-upgrade.patch');
    expect(patchDownloadBtn).toHaveAttribute(
      'href',
      expect.stringContaining('/api/export/exp_test_abc123/download/patch')
    );
  });

  it('displays diagnostic error banner when export fails due to secret leakage', async () => {
    vi.mocked(apiClient.createExport).mockRejectedValueOnce(
      new Error('Export generation failed: Pre-export secret verification failed')
    );

    render(<ExportManager proposalId="prop_secret_leak" />);

    fireEvent.click(screen.getByTestId('generate-export-button'));

    await waitFor(() => {
      expect(screen.getByTestId('export-error-banner')).toBeInTheDocument();
    });

    expect(screen.getByTestId('export-error-banner')).toHaveTextContent(
      'Export generation failed: Pre-export secret verification failed'
    );
  });

  it('handles copying SHA-256 checksum to clipboard', async () => {
    vi.mocked(apiClient.createExport).mockResolvedValueOnce(mockExportResponse);
    Object.assign(navigator, {
      clipboard: {
        writeText: vi.fn(),
      },
    });

    render(<ExportManager proposalId="prop_test_456" />);

    fireEvent.click(screen.getByTestId('generate-export-button'));

    await waitFor(() => {
      expect(screen.getByTestId('export-results-card')).toBeInTheDocument();
    });

    const copyBtn = screen.getByLabelText('Copy SHA-256 for requirements.txt-upgrade.patch');
    fireEvent.click(copyBtn);

    expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
      'a1b2c3d4e5f60718293a4b5c6d7e8f90123456789abcdef0123456789abcdef0'
    );
    expect(screen.getByText('✓ Copied')).toBeInTheDocument();
  });
});
