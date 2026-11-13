import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { CompatibilityPanel } from './CompatibilityPanel';
import * as apiClient from '../api/client';
import type { ResolutionView, ResolutionReport, ResolvedPackage } from '../api/client';

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof apiClient>('../api/client');
  return {
    ...actual,
    getResolution: vi.fn(),
    startResolution: vi.fn(),
    getResolutionJob: vi.fn(),
    cancelResolutionJob: vi.fn(),
  };
});

const DISCLOSURE =
  'Compatible means the complete dependency set resolves successfully for the validation target. It does not mean the packages have been vetted or are safe. DUA does not perform malware detection.';

const pkg = (name: string, extra: Partial<ResolvedPackage> = {}): ResolvedPackage => ({
  name,
  version: '1.0.0',
  direct: false,
  requested_extras: [],
  constraints: [],
  filename: `${name}-1.0.0-py3-none-any.whl`,
  upload: { available: true, value: '2025-01-02T03:04:05.000000Z', source: 'pep700-simple-json', filename: `${name}-1.0.0-py3-none-any.whl` },
  introduced: false,
  ...extra,
});

const report = (overrides: Partial<ResolutionReport> = {}): ResolutionReport => ({
  policy_version: 'dua-resolution/1',
  role: 'proposed',
  status: 'COMPATIBLE',
  fingerprint: 'f'.repeat(64),
  resolver: 'pip 24.0',
  image: 'python:3.11-slim@sha256:abc',
  target: { python: '3.11.18', machine: 'x86_64' },
  packages: [pkg('flask', { direct: true }), pkg('blinker', { introduced: true, upload: { available: false, reason: 'field_missing' } })],
  conflicts: [],
  unsupported: [],
  warnings: [],
  comparison: { available: true, introduced: ['blinker'] },
  created_at: 1,
  ...overrides,
});

const view = (overrides: Partial<ResolutionView> = {}): ResolutionView => ({
  proposal_id: 'prop_1',
  status: 'UNKNOWN',
  reason: 'not_resolved',
  approval_eligible: false,
  job: null,
  proposed: null,
  original: null,
  comparison: null,
  disclosure: DISCLOSURE,
  ...overrides,
});

describe('CompatibilityPanel', () => {
  beforeEach(() => vi.clearAllMocks());

  it('starts unverified and keeps approval locked', async () => {
    vi.mocked(apiClient.getResolution).mockResolvedValue(view());
    const onEligible = vi.fn();
    render(<CompatibilityPanel proposalId="prop_1" onEligibilityChange={onEligible} />);
    await waitFor(() => expect(screen.getByTestId('compat-unknown')).toBeInTheDocument());
    expect(screen.getByTestId('compat-status')).toHaveTextContent('Not verified');
    expect(onEligible).not.toHaveBeenCalledWith(true);
    expect(screen.getByTestId('compat-start')).toHaveTextContent('Check compatibility');
  });

  it('shows honest progress from the server and never success before evidence', async () => {
    vi.mocked(apiClient.getResolution)
      .mockResolvedValueOnce(view())
      .mockResolvedValueOnce(view({ status: 'COMPATIBLE', approval_eligible: true, proposed: report(), comparison: report().comparison }));
    vi.mocked(apiClient.startResolution).mockResolvedValue({
      job: { id: 'job1', status: 'QUEUED', cancel_requested: false, created_at: 1 },
      resolution: view(),
    });
    vi.mocked(apiClient.getResolutionJob)
      .mockResolvedValueOnce({ id: 'job1', status: 'RUNNING', current_role: 'proposed', cancel_requested: false, created_at: 1 })
      .mockResolvedValueOnce({ id: 'job1', status: 'SUCCEEDED', cancel_requested: false, created_at: 1 });
    const onEligible = vi.fn();
    render(<CompatibilityPanel proposalId="prop_1" onEligibilityChange={onEligible} pollMs={5} />);
    await waitFor(() => expect(screen.getByTestId('compat-start')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('compat-start'));
    await waitFor(() => expect(screen.getByTestId('compat-progress')).toBeInTheDocument());
    expect(screen.getByTestId('compat-status')).toHaveTextContent('Checking');
    expect(onEligible).not.toHaveBeenCalledWith(true);
    await waitFor(() => expect(screen.getByTestId('compat-compatible')).toBeInTheDocument());
    expect(onEligible).toHaveBeenLastCalledWith(true);
  });

  it('shows the disclosure, introduced packages and exact upload dates for COMPATIBLE', async () => {
    vi.mocked(apiClient.getResolution).mockResolvedValue(
      view({ status: 'COMPATIBLE', approval_eligible: true, proposed: report(), comparison: report().comparison })
    );
    render(<CompatibilityPanel proposalId="prop_1" onEligibilityChange={vi.fn()} />);
    await waitFor(() => expect(screen.getByTestId('compat-compatible')).toBeInTheDocument());
    expect(screen.getByTestId('compat-disclosure')).toHaveTextContent('does not mean the packages have been vetted or are safe');
    expect(screen.getByTestId('compat-introduced')).toHaveTextContent('blinker');
    const table = screen.getByTestId('compat-proposed-table');
    expect(table).toHaveTextContent('2025-01-02 03:04:05');
    expect(table).toHaveTextContent('date unavailable');
    expect(table).toHaveTextContent('transitive');
    expect(screen.queryByText(/safe to install|malware-free/i)).toBeNull();
  });

  it('says comparison unavailable when the original did not resolve', async () => {
    const proposed = report({ comparison: { available: false, reason: 'original_conflict', introduced: [] } });
    vi.mocked(apiClient.getResolution).mockResolvedValue(
      view({ status: 'COMPATIBLE', approval_eligible: true, proposed, comparison: proposed.comparison })
    );
    render(<CompatibilityPanel proposalId="prop_1" onEligibilityChange={vi.fn()} />);
    await waitFor(() => expect(screen.getByTestId('compat-introduced')).toHaveTextContent('comparison unavailable (original conflict)'));
  });

  it.each([
    ['CONFLICT', 'compat-conflict', { conflicts: [{ summary: 'Cannot install numpy and scipy', causes: ['scipy 1.10.0 depends on numpy<1.27.0'] }] }, 'scipy 1.10.0 depends on numpy<1.27.0'],
    ['UNSUPPORTED', 'compat-unsupported', { unsupported: [{ reason: 'non_python_manifest', message: 'npm compatibility verification is not yet supported.', source: 'manifest' }] }, 'npm compatibility verification is not yet supported'],
  ])('renders %s without enabling approval', async (status, testId, extra, text) => {
    vi.mocked(apiClient.getResolution).mockResolvedValue(
      view({ status: status as ResolutionView['status'], proposed: report({ status: status as ResolutionView['status'], packages: [], ...extra }) })
    );
    const onEligible = vi.fn();
    render(<CompatibilityPanel proposalId="prop_1" onEligibilityChange={onEligible} />);
    await waitFor(() => expect(screen.getByTestId(testId)).toHaveTextContent(text));
    expect(onEligible).not.toHaveBeenCalledWith(true);
  });

  it.each([
    [{ status: 'RESOLUTION_FAILED', reason: 'timeout' }, 'compat-failed', 'did not finish within its time limit'],
    [{ status: 'RESOLVER_UNAVAILABLE', job: { id: 'j', status: 'FAILED', cancel_requested: false, created_at: 1, error_message: 'Docker is not running.' } }, 'compat-unavailable', 'Docker is not running.'],
    [{ status: 'UNKNOWN', reason: 'job_cancelled' }, 'compat-unknown', 'cancelled'],
    [{ status: 'UNKNOWN', reason: 'job_interrupted' }, 'compat-unknown', 'interrupted'],
    [{ status: 'UNKNOWN', reason: 'evidence_stale (inputs_mismatch)' }, 'compat-unknown', 'changed since the last check'],
  ])('explains %o', async (overrides, testId, text) => {
    vi.mocked(apiClient.getResolution).mockResolvedValue(view(overrides as Partial<ResolutionView>));
    render(<CompatibilityPanel proposalId="prop_1" onEligibilityChange={vi.fn()} />);
    await waitFor(() => expect(screen.getByTestId(testId)).toHaveTextContent(text));
  });

  it('cancels a running check', async () => {
    vi.mocked(apiClient.getResolution).mockResolvedValue(
      view({ job: { id: 'job1', status: 'RUNNING', current_role: 'original', cancel_requested: false, created_at: 1 } })
    );
    vi.mocked(apiClient.getResolutionJob).mockResolvedValue({ id: 'job1', status: 'RUNNING', current_role: 'original', cancel_requested: false, created_at: 1 });
    vi.mocked(apiClient.cancelResolutionJob).mockResolvedValue({ id: 'job1', status: 'RUNNING', current_role: 'original', cancel_requested: true, created_at: 1 });
    render(<CompatibilityPanel proposalId="prop_1" onEligibilityChange={vi.fn()} pollMs={10000} />);
    await waitFor(() => expect(screen.getByTestId('compat-progress')).toHaveTextContent('1 of 2'));
    fireEvent.click(screen.getByTestId('compat-cancel'));
    await waitFor(() => expect(screen.getByTestId('compat-progress')).toHaveTextContent('Cancelling'));
    expect(apiClient.cancelResolutionJob).toHaveBeenCalledWith('job1');
  });

  it('reports an expired plan as an error and stays locked', async () => {
    vi.mocked(apiClient.getResolution).mockRejectedValue(new apiClient.ApiError('This plan has expired. Build it again.', 410));
    const onEligible = vi.fn();
    render(<CompatibilityPanel proposalId="prop_1" onEligibilityChange={onEligible} />);
    await waitFor(() => expect(screen.getByTestId('compat-error')).toHaveTextContent('expired'));
    expect(onEligible).not.toHaveBeenCalledWith(true);
  });
});
