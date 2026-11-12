import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { ManifestAnalyzer } from './ManifestAnalyzer';
import * as apiClient from '../api/client';

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof apiClient>('../api/client');
  return {
    ...actual,
    analyzeRepository: vi.fn(),
    createManifestRun: vi.fn(),
    generateRunRecommendations: vi.fn(),
    generateRunPlan: vi.fn(),
    approveProposal: vi.fn(),
    // M12: the server reports verified COMPATIBLE evidence for the plan, so approval is offered.
    getResolution: vi.fn().mockResolvedValue({
      proposal_id: 'prop_1',
      status: 'COMPATIBLE',
      approval_eligible: true,
      disclosure: 'Compatible means the complete dependency set resolves successfully.',
      job: null,
      proposed: {
        policy_version: 'dua-resolution/1', role: 'proposed', status: 'COMPATIBLE', fingerprint: 'f'.repeat(64),
        resolver: 'pip 24.0', image: 'python:3.11-slim@sha256:x', target: { python: '3.11.18', machine: 'x86_64' },
        packages: [], conflicts: [], unsupported: [], warnings: [], created_at: 1,
        comparison: { available: true, introduced: [] },
      },
      original: null,
      comparison: { available: true, introduced: [] },
    }),
    getSandboxStatus: vi.fn().mockResolvedValue({ is_available: false, version: null, error_message: 'no docker' }),
    getAiStatus: vi.fn().mockResolvedValue({
      is_available: false,
      provider_name: 'ollama',
      endpoint: 'http://127.0.0.1:11434',
      discovered_models: [],
      default_model: null,
      message: 'Offline (Connection refused)',
    }),
  };
});

const dep = (
  name: string,
  status: apiClient.DependencyStatus,
  current: string,
  latest: string
): apiClient.AnalyzedDependency => ({
  name,
  original_name: name,
  ecosystem: 'python',
  dependency_type: 'runtime',
  declared_specifier: `==${current}`,
  current_version: current,
  latest_version: latest,
  latest_compatible_version: latest,
  status,
  status_reason: status === 'UP_TO_DATE' ? 'Pinned to latest release' : `Upgrade available from ${current} to ${latest}`,
  registry_status: 'SUCCESS',
  available_versions: [current, latest],
  yanked_or_deprecated: false,
  deprecation_reason: null,
  line_number: 1,
  raw_line: `${name}==${current}`,
  diagnostic_message: null,
});

const ANALYSIS: apiClient.ManifestAnalysisResponse = {
  manifest_type: 'requirements.txt',
  ecosystem: 'python',
  total_dependencies: 2,
  up_to_date_count: 1,
  upgrade_available_count: 1,
  preserved_count: 0,
  dependencies: [dep('requests', 'UPGRADE_AVAILABLE', '2.28.0', '2.32.3'), dep('fastapi', 'UP_TO_DATE', '0.115.0', '0.115.0')],
  diagnostics: [],
};

const manifestRun = (overrides: Partial<apiClient.RunManifest> = {}): apiClient.RunResponse => ({
  run_id: 'run_test',
  source: 'manifest',
  created_at: 0,
  repository: null,
  manifests: [
    {
      index: 0,
      path: 'requirements.txt',
      manifest_type: 'requirements.txt',
      kind: 'requirements',
      ecosystem: 'python',
      plannable: true,
      analysis: ANALYSIS,
      ...overrides,
    },
  ],
  skipped: [],
  has_workspace: false,
});

const rec = (
  overrides: Partial<apiClient.PackageUpgradeRecommendation> = {}
): apiClient.PackageUpgradeRecommendation => ({
  package_name: 'requests',
  ecosystem: 'python',
  current_version: '2.28.0',
  declared_specifier: '==2.28.0',
  latest_registry_version: '2.32.3',
  candidate_version: '2.32.3',
  category: 'RECOMMENDED',
  priority: 'LOW',
  explanation: 'Minor update',
  breaking_changes_summary: null,
  confidence_score: 0.9,
  provider_used: 'ollama',
  model_used: 'llama3',
  is_ai_generated: true,
  validation_status: 'VALIDATED',
  raw_line: 'requests==2.28.0',
  line_number: 1,
  ...overrides,
});

const RECS: apiClient.RecommendationBatchResponse = {
  manifest_type: 'requirements.txt',
  ecosystem: 'python',
  recommendations: [rec()],
  provider_used: 'ollama',
  model_used: 'llama3',
  is_offline_fallback: false,
  total_upgrades_recommended: 1,
  total_caution: 0,
  summary: '1 upgrade',
};

const PROPOSAL: apiClient.UpgradeProposalResponse = {
  proposal_id: 'prop_1',
  manifest_type: 'requirements.txt',
  ecosystem: 'python',
  file_path: 'requirements.txt',
  plan_items: [],
  total_planned: 1,
  total_unchanged: 1,
  total_conflicts: 0,
  original_content: 'requests==2.28.0\n',
  proposed_content: 'requests==2.32.3\n',
  unified_diff: '--- a/requirements.txt\n+++ b/requirements.txt\n@@ -1 +1 @@\n-requests==2.28.0\n+requests==2.32.3\n',
  integrity: {
    is_valid: true,
    can_apply: true,
    can_reverse: true,
    syntax_valid: true,
    target_versions_match: true,
    unrelated_dependencies_unchanged: true,
    original_unchanged: true,
    checks_passed: [],
    checks_failed: [],
    errors: [],
    warnings: [],
  },
  status: 'READY_FOR_REVIEW',
  requires_human_approval: true,
  diagnostics: [],
  summary: '1 upgrade',
};

const openManifestMode = () => fireEvent.click(screen.getByTestId('source-manifest'));

const analyzeManually = async () => {
  vi.mocked(apiClient.createManifestRun).mockResolvedValueOnce(manifestRun());
  openManifestMode();
  fireEvent.click(screen.getByTestId('btn-analyze'));
  await waitFor(() => expect(screen.getByTestId('analysis-results')).toBeInTheDocument());
};

describe('ManifestAnalyzer Component', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('opens on repository input and offers the manual manifest editor', () => {
    render(<ManifestAnalyzer />);
    expect(screen.getByRole('heading', { level: 1, name: 'Analyze dependencies' })).toBeInTheDocument();
    expect(screen.getByTestId('repo-url-input')).toBeInTheDocument();
    expect(screen.getByTestId('btn-analyze-repository')).toHaveTextContent('Analyze repository');

    openManifestMode();
    expect(screen.getByTestId('tab-raw')).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByTestId('manifest-type-select')).toHaveValue('requirements.txt');
    const textarea = screen.getByTestId('raw-content-input') as HTMLTextAreaElement;
    expect(textarea.value).toContain('requests==2.28.0');
  });

  it('switches tabs between paste and upload', () => {
    render(<ManifestAnalyzer />);
    openManifestMode();
    fireEvent.click(screen.getByTestId('tab-upload'));
    expect(screen.getByTestId('tab-upload')).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByTestId('dropzone')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('tab-raw'));
    expect(screen.getByTestId('raw-content-input')).toBeInTheDocument();
  });

  it('changes format selector to package.json and updates sample content', () => {
    render(<ManifestAnalyzer />);
    openManifestMode();
    fireEvent.change(screen.getByTestId('manifest-type-select'), { target: { value: 'package.json' } });
    const textarea = screen.getByTestId('raw-content-input') as HTMLTextAreaElement;
    expect(textarea.value).toContain('"dependencies"');
  });

  it('analyzes a pasted manifest on the server and shows dependency health', async () => {
    render(<ManifestAnalyzer />);
    await analyzeManually();
    expect(apiClient.createManifestRun).toHaveBeenCalledWith('requirements.txt', expect.stringContaining('requests==2.28.0'));
    expect(screen.getByText('Total dependencies')).toBeInTheDocument();
    expect(screen.getAllByText('requests').length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText('fastapi').length).toBeGreaterThanOrEqual(1);
  });

  it('filters dependencies by status', async () => {
    render(<ManifestAnalyzer />);
    await analyzeManually();
    const health = screen.getByTestId('analysis-results');
    fireEvent.click(screen.getByTestId('filter-upgrade-available'));
    expect(health).toHaveTextContent('requests');
    expect(health).not.toHaveTextContent('fastapi');
    fireEvent.click(screen.getByTestId('filter-up-to-date'));
    expect(health).not.toHaveTextContent('requests');
    expect(health).toHaveTextContent('fastapi');
  });

  it('shows the error and parser diagnostics when analysis fails', async () => {
    vi.mocked(apiClient.createManifestRun).mockRejectedValueOnce(
      new apiClient.ApiError('Invalid JSON syntax in package.json', 422, {
        diagnostics: ['Line 1, column 5: Unexpected token'],
      })
    );
    render(<ManifestAnalyzer />);
    openManifestMode();
    fireEvent.click(screen.getByTestId('btn-analyze'));
    await waitFor(() => expect(screen.getByTestId('error-banner')).toBeInTheDocument());
    expect(screen.getByText('Invalid JSON syntax in package.json')).toBeInTheDocument();
    expect(screen.getByText('Line 1, column 5: Unexpected token')).toBeInTheDocument();
  });

  it('generates recommendations from the server-held analysis and labels model output', async () => {
    vi.mocked(apiClient.getAiStatus).mockResolvedValueOnce({
      is_available: true,
      provider_name: 'ollama',
      endpoint: 'http://127.0.0.1:11434',
      discovered_models: [{ name: 'llama3' }],
      default_model: 'llama3',
      message: 'ok',
    });
    vi.mocked(apiClient.generateRunRecommendations).mockResolvedValueOnce(RECS);
    render(<ManifestAnalyzer />);
    await analyzeManually();
    await waitFor(() => expect(screen.getByTestId('ai-provider-status')).toHaveTextContent(/Local model available/));

    fireEvent.click(screen.getByTestId('btn-generate-recommendations'));
    await waitFor(() => expect(screen.getByTestId('recommendations-results')).toBeInTheDocument());
    expect(apiClient.generateRunRecommendations).toHaveBeenCalledWith('run_test', 0, {
      preferred_model: 'llama3',
      force_deterministic: false,
    });
    expect(screen.getByTestId('recommendations-results')).toHaveTextContent('1 recommended');
    expect(screen.getByText('Model, registry-checked')).toBeInTheDocument();
    expect(screen.getByText('Low')).toBeInTheDocument();
  });

  it('labels rule-based fallback recommendations', async () => {
    vi.mocked(apiClient.generateRunRecommendations).mockResolvedValueOnce({
      ...RECS,
      recommendations: [rec({ is_ai_generated: false, validation_status: 'FALLBACK_DETERMINISTIC', provider_used: 'deterministic' })],
      provider_used: 'deterministic',
      model_used: null,
      is_offline_fallback: true,
    });
    render(<ManifestAnalyzer />);
    await analyzeManually();
    fireEvent.click(screen.getByTestId('btn-generate-recommendations'));
    await waitFor(() => expect(screen.getByTestId('recommendations-results')).toBeInTheDocument());
    expect(screen.getByTestId('fallback-indicator')).toHaveTextContent('rules used instead');
    expect(screen.getByText('Rule-based')).toBeInTheDocument();
    expect(screen.queryByText('Model, registry-checked')).not.toBeInTheDocument();
  });

  it('analyzes a repository and lists its manifests for review', async () => {
    vi.mocked(apiClient.analyzeRepository).mockResolvedValueOnce({
      run_id: 'run_repo',
      source: 'github',
      created_at: 0,
      repository: {
        full_name: 'octo/demo',
        url: 'https://github.com/octo/demo',
        commit_sha: 'abcdef1234567890',
        default_branch: 'main',
        files_scanned: 12,
        bytes_on_disk: 4096,
      },
      manifests: [
        { index: 0, path: 'api/requirements.txt', manifest_type: 'requirements.txt', kind: 'requirements', plannable: true, analysis: ANALYSIS },
        { index: 1, path: 'pyproject.toml', manifest_type: 'requirements.txt', kind: 'pyproject', plannable: false, note: 'Read-only', analysis: ANALYSIS },
      ],
      skipped: ['big/requirements.txt: larger than 512 KB'],
      has_workspace: true,
    });
    render(<ManifestAnalyzer />);
    fireEvent.change(screen.getByTestId('repo-url-input'), { target: { value: 'https://github.com/octo/demo' } });
    fireEvent.click(screen.getByTestId('btn-analyze-repository'));
    await waitFor(() => expect(screen.getByTestId('manifests-stage')).toBeInTheDocument());

    expect(apiClient.analyzeRepository).toHaveBeenCalledWith('https://github.com/octo/demo');
    expect(screen.getByTestId('repo-summary')).toHaveTextContent('octo/demo');
    expect(screen.getByTestId('repo-summary')).toHaveTextContent('abcdef1234');
    expect(screen.getByText('1 file skipped')).toBeInTheDocument();
    // Two usable manifests: nothing is opened until the user chooses one.
    expect(screen.queryByTestId('analysis-results')).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId('open-manifest-1'));
    expect(screen.getByTestId('analysis-results')).toBeInTheDocument();
    expect(screen.getByTestId('manifest-row-1')).toHaveTextContent('Review only');
  });

  it('shows repository errors from the server', async () => {
    vi.mocked(apiClient.analyzeRepository).mockRejectedValueOnce(
      new apiClient.ApiError('Only public repositories on github.com are supported.', 422)
    );
    render(<ManifestAnalyzer />);
    fireEvent.change(screen.getByTestId('repo-url-input'), { target: { value: 'https://gitlab.com/a/b' } });
    fireEvent.click(screen.getByTestId('btn-analyze-repository'));
    await waitFor(() => expect(screen.getByTestId('error-banner')).toBeInTheDocument());
    expect(screen.getByTestId('error-banner')).toHaveTextContent('The repository could not be analyzed');
    expect(screen.getByTestId('error-banner')).toHaveTextContent('Only public repositories');
  });

  it('records approval on the server before unlocking validation', async () => {
    vi.mocked(apiClient.generateRunRecommendations).mockResolvedValueOnce(RECS);
    vi.mocked(apiClient.generateRunPlan).mockResolvedValueOnce(PROPOSAL);
    vi.mocked(apiClient.approveProposal).mockResolvedValueOnce({
      proposal_id: 'prop_1',
      run_id: 'run_test',
      diff_sha256: 'x'.repeat(64),
      approved: true,
    });
    render(<ManifestAnalyzer />);
    await analyzeManually();
    fireEvent.click(screen.getByTestId('btn-generate-recommendations'));
    await waitFor(() => expect(screen.getByTestId('btn-generate-plan')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('btn-generate-plan'));
    await waitFor(() => expect(screen.getByTestId('plan-reviewer-section')).toBeInTheDocument());
    expect(apiClient.generateRunPlan).toHaveBeenCalledWith('run_test', 0);
    expect(screen.getByTestId('btn-run-validation')).toBeDisabled();

    fireEvent.click(screen.getByTestId('approval-checkbox'));
    fireEvent.click(screen.getByTestId('confirm-approval-btn'));
    await waitFor(() => expect(screen.getByTestId('approval-confirmed-banner')).toBeInTheDocument());
    expect(apiClient.approveProposal).toHaveBeenCalledWith('prop_1', PROPOSAL.unified_diff, undefined);
    expect(screen.getByTestId('btn-run-validation')).not.toBeDisabled();
  });

  it('keeps validation locked when the server refuses approval', async () => {
    vi.mocked(apiClient.generateRunRecommendations).mockResolvedValueOnce(RECS);
    vi.mocked(apiClient.generateRunPlan).mockResolvedValueOnce(PROPOSAL);
    vi.mocked(apiClient.approveProposal).mockRejectedValueOnce(
      new apiClient.ApiError('The diff you reviewed does not match this plan.', 409)
    );
    render(<ManifestAnalyzer />);
    await analyzeManually();
    fireEvent.click(screen.getByTestId('btn-generate-recommendations'));
    await waitFor(() => expect(screen.getByTestId('btn-generate-plan')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('btn-generate-plan'));
    await waitFor(() => expect(screen.getByTestId('plan-reviewer-section')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('approval-checkbox'));
    fireEvent.click(screen.getByTestId('confirm-approval-btn'));
    await waitFor(() => expect(screen.getByTestId('approval-error')).toHaveTextContent('does not match'));
    expect(screen.getByTestId('btn-run-validation')).toBeDisabled();
  });

  it('disables plan building for review-only manifests', async () => {
    vi.mocked(apiClient.createManifestRun).mockResolvedValueOnce(manifestRun({ plannable: false, kind: 'pyproject' }));
    vi.mocked(apiClient.generateRunRecommendations).mockResolvedValueOnce(RECS);
    render(<ManifestAnalyzer />);
    openManifestMode();
    fireEvent.click(screen.getByTestId('btn-analyze'));
    await waitFor(() => expect(screen.getByTestId('analysis-results')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('btn-generate-recommendations'));
    await waitFor(() => expect(screen.getByTestId('btn-generate-plan')).toBeInTheDocument());
    expect(screen.getByTestId('btn-generate-plan')).toBeDisabled();
    expect(screen.getByText(/shown for review only/)).toBeInTheDocument();
  });

  it('marks versions that came from offline data instead of the registry', async () => {
    const offline = {
      ...ANALYSIS,
      dependencies: [
        { ...dep('requests', 'UPGRADE_AVAILABLE', '2.28.0', '2.32.3'), registry_status: 'OFFLINE_CATALOG' as const, data_source: 'OFFLINE_CATALOG' as const },
        { ...dep('ghost-pkg', 'PRESERVED', '1.0.0', '1.0.0'), registry_status: 'NOT_FOUND' as const },
      ],
    };
    vi.mocked(apiClient.createManifestRun).mockResolvedValueOnce(manifestRun({ analysis: offline }));
    render(<ManifestAnalyzer />);
    openManifestMode();
    fireEvent.click(screen.getByTestId('btn-analyze'));
    await waitFor(() => expect(screen.getByTestId('analysis-results')).toBeInTheDocument());
    expect(screen.getByTestId('offline-catalog-notice')).toBeInTheDocument();
    expect(screen.getByText('offline data')).toBeInTheDocument();
    expect(screen.getByText('not in registry')).toBeInTheDocument();
  });

  it('labels recommendations built on offline data as unverified', async () => {
    vi.mocked(apiClient.generateRunRecommendations).mockResolvedValueOnce({
      ...RECS,
      recommendations: [rec({ validation_status: 'UNVERIFIED_OFFLINE', data_source: 'OFFLINE_CATALOG', is_ai_generated: false })],
    });
    render(<ManifestAnalyzer />);
    await analyzeManually();
    fireEvent.click(screen.getByTestId('btn-generate-recommendations'));
    await waitFor(() => expect(screen.getByTestId('recommendations-results')).toBeInTheDocument());
    expect(screen.getByText('Offline data, unverified')).toBeInTheDocument();
    expect(screen.queryByText('Registry-checked')).not.toBeInTheDocument();
  });
});
