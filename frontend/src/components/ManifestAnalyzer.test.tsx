import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { ManifestAnalyzer } from './ManifestAnalyzer';
import * as apiClient from '../api/client';

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof apiClient>('../api/client');
  return {
    ...actual,
    analyzeManifest: vi.fn(),
    uploadManifest: vi.fn(),
    getAiStatus: vi.fn().mockResolvedValue({
      is_available: false,
      provider_name: 'ollama',
      endpoint: 'http://127.0.0.1:11434',
      discovered_models: [],
      default_model: null,
      message: 'Offline (Connection refused)',
    }),
    generateRecommendations: vi.fn(),
  };
});

describe('ManifestAnalyzer Component', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders analyzer header, format selector, and raw editor with initial sample', () => {
    render(<ManifestAnalyzer />);

    expect(screen.getByText('Manifest Ingestion & Dependency Analysis')).toBeInTheDocument();
    expect(screen.getByTestId('tab-raw')).toHaveClass('active');
    expect(screen.getByTestId('manifest-type-select')).toHaveValue('requirements.txt');
    const textarea = screen.getByTestId('raw-content-input') as HTMLTextAreaElement;
    expect(textarea.value).toContain('requests==2.28.0');
    expect(screen.getByTestId('btn-analyze')).toBeInTheDocument();
  });

  it('switches tabs between Raw Editor and File Upload', () => {
    render(<ManifestAnalyzer />);

    fireEvent.click(screen.getByTestId('tab-upload'));
    expect(screen.getByTestId('tab-upload')).toHaveClass('active');
    expect(screen.getByTestId('dropzone')).toBeInTheDocument();

    fireEvent.click(screen.getByTestId('tab-raw'));
    expect(screen.getByTestId('tab-raw')).toHaveClass('active');
    expect(screen.getByTestId('raw-content-input')).toBeInTheDocument();
  });

  it('changes format selector to package.json and updates sample content', () => {
    render(<ManifestAnalyzer />);

    const select = screen.getByTestId('manifest-type-select');
    fireEvent.change(select, { target: { value: 'package.json' } });

    const textarea = screen.getByTestId('raw-content-input') as HTMLTextAreaElement;
    expect(textarea.value).toContain('"dependencies"');
  });

  it('analyzes raw manifest and displays summary metrics and dependency rows', async () => {
    const mockResponse: apiClient.ManifestAnalysisResponse = {
      manifest_type: 'requirements.txt',
      ecosystem: 'python',
      total_dependencies: 2,
      up_to_date_count: 1,
      upgrade_available_count: 1,
      preserved_count: 0,
      dependencies: [
        {
          name: 'requests',
          original_name: 'requests',
          ecosystem: 'python',
          dependency_type: 'runtime',
          declared_specifier: '==2.28.0',
          current_version: '2.28.0',
          latest_version: '2.32.0',
          latest_compatible_version: '2.32.0',
          status: 'UPGRADE_AVAILABLE',
          status_reason: 'Upgrade available from 2.28.0 to 2.32.0',
          registry_status: 'SUCCESS',
          available_versions: ['2.28.0', '2.32.0'],
          yanked_or_deprecated: false,
          deprecation_reason: null,
          line_number: 1,
          raw_line: 'requests==2.28.0',
          diagnostic_message: null,
        },
        {
          name: 'fastapi',
          original_name: 'fastapi',
          ecosystem: 'python',
          dependency_type: 'runtime',
          declared_specifier: '==0.115.0',
          current_version: '0.115.0',
          latest_version: '0.115.0',
          latest_compatible_version: '0.115.0',
          status: 'UP_TO_DATE',
          status_reason: 'Pinned to latest release',
          registry_status: 'SUCCESS',
          available_versions: ['0.115.0'],
          yanked_or_deprecated: false,
          deprecation_reason: null,
          line_number: 2,
          raw_line: 'fastapi==0.115.0',
          diagnostic_message: null,
        },
      ],
      diagnostics: [],
    };

    vi.mocked(apiClient.analyzeManifest).mockResolvedValueOnce(mockResponse);

    render(<ManifestAnalyzer />);
    fireEvent.click(screen.getByTestId('btn-analyze'));

    await waitFor(() => {
      expect(screen.getByTestId('analysis-results')).toBeInTheDocument();
    });

    expect(screen.getByText('Total Dependencies')).toBeInTheDocument();
    expect(screen.getByText('requests')).toBeInTheDocument();
    expect(screen.getByText('fastapi')).toBeInTheDocument();
  });

  it('filters dependencies by status using chips', async () => {
    const mockResponse: apiClient.ManifestAnalysisResponse = {
      manifest_type: 'requirements.txt',
      ecosystem: 'python',
      total_dependencies: 2,
      up_to_date_count: 1,
      upgrade_available_count: 1,
      preserved_count: 0,
      dependencies: [
        {
          name: 'requests',
          original_name: 'requests',
          ecosystem: 'python',
          dependency_type: 'runtime',
          declared_specifier: '==2.28.0',
          current_version: '2.28.0',
          latest_version: '2.32.0',
          latest_compatible_version: '2.32.0',
          status: 'UPGRADE_AVAILABLE',
          status_reason: 'Upgrade available',
          registry_status: 'SUCCESS',
          available_versions: ['2.28.0', '2.32.0'],
          yanked_or_deprecated: false,
          deprecation_reason: null,
          line_number: 1,
          raw_line: 'requests==2.28.0',
          diagnostic_message: null,
        },
        {
          name: 'fastapi',
          original_name: 'fastapi',
          ecosystem: 'python',
          dependency_type: 'runtime',
          declared_specifier: '==0.115.0',
          current_version: '0.115.0',
          latest_version: '0.115.0',
          latest_compatible_version: '0.115.0',
          status: 'UP_TO_DATE',
          status_reason: 'Up to date',
          registry_status: 'SUCCESS',
          available_versions: ['0.115.0'],
          yanked_or_deprecated: false,
          deprecation_reason: null,
          line_number: 2,
          raw_line: 'fastapi==0.115.0',
          diagnostic_message: null,
        },
      ],
      diagnostics: [],
    };

    vi.mocked(apiClient.analyzeManifest).mockResolvedValueOnce(mockResponse);

    render(<ManifestAnalyzer />);
    fireEvent.click(screen.getByTestId('btn-analyze'));

    await waitFor(() => {
      expect(screen.getByTestId('analysis-results')).toBeInTheDocument();
    });

    // Click 'Upgrades' chip
    fireEvent.click(screen.getByTestId('filter-upgrade-available'));
    expect(screen.getByText('requests')).toBeInTheDocument();
    expect(screen.queryByText('fastapi')).not.toBeInTheDocument();

    // Click 'Up to Date' chip
    fireEvent.click(screen.getByTestId('filter-up-to-date'));
    expect(screen.queryByText('requests')).not.toBeInTheDocument();
    expect(screen.getByText('fastapi')).toBeInTheDocument();
  });

  it('displays error banner when analysis fails with diagnostics', async () => {
    const errorWithDetails = new apiClient.ApiError(
      'Invalid JSON syntax in package.json',
      422,
      { diagnostics: ['Line 1, column 5: Unexpected token'] }
    );

    vi.mocked(apiClient.analyzeManifest).mockRejectedValueOnce(errorWithDetails);

    render(<ManifestAnalyzer />);
    fireEvent.click(screen.getByTestId('btn-analyze'));

    await waitFor(() => {
      expect(screen.getByTestId('error-banner')).toBeInTheDocument();
    });

    expect(screen.getByText('Invalid JSON syntax in package.json')).toBeInTheDocument();
    expect(screen.getByText('Line 1, column 5: Unexpected token')).toBeInTheDocument();
  });

  it('renders AI provider status indicator and generates recommendations', async () => {
    vi.mocked(apiClient.getAiStatus).mockResolvedValueOnce({
      is_available: true,
      provider_name: 'ollama',
      endpoint: 'http://127.0.0.1:11434',
      discovered_models: [{ name: 'llama3.2:latest', parameter_size: '3B' }],
      default_model: 'llama3.2:latest',
      message: 'Ollama 0.5.1 active',
    });

    const mockAnalysis: apiClient.ManifestAnalysisResponse = {
      manifest_type: 'requirements.txt',
      ecosystem: 'python',
      total_dependencies: 1,
      up_to_date_count: 0,
      upgrade_available_count: 1,
      preserved_count: 0,
      dependencies: [
        {
          name: 'requests',
          original_name: 'requests',
          ecosystem: 'python',
          dependency_type: 'runtime',
          declared_specifier: '==2.28.0',
          current_version: '2.28.0',
          latest_version: '2.32.3',
          latest_compatible_version: '2.32.3',
          status: 'UPGRADE_AVAILABLE',
          status_reason: 'Upgrade available',
          registry_status: 'SUCCESS',
          available_versions: ['2.28.0', '2.32.3'],
          yanked_or_deprecated: false,
          deprecation_reason: null,
          line_number: 1,
          raw_line: 'requests==2.28.0',
          diagnostic_message: null,
        },
      ],
      diagnostics: [],
    };

    const mockRecs: apiClient.RecommendationBatchResponse = {
      manifest_type: 'requirements.txt',
      ecosystem: 'python',
      recommendations: [
        {
          package_name: 'requests',
          ecosystem: 'python',
          current_version: '2.28.0',
          declared_specifier: '==2.28.0',
          latest_registry_version: '2.32.3',
          candidate_version: '2.32.3',
          category: 'RECOMMENDED',
          priority: 'LOW',
          explanation: 'Security patch update with bug fixes.',
          breaking_changes_summary: null,
          confidence_score: 0.95,
          provider_used: 'ollama',
          model_used: 'llama3.2:latest',
          is_ai_generated: true,
          validation_status: 'VALIDATED',
          raw_line: 'requests==2.28.0',
          line_number: 1,
        },
      ],
      provider_used: 'ollama',
      model_used: 'llama3.2:latest',
      is_offline_fallback: false,
      total_upgrades_recommended: 1,
      total_caution: 0,
      summary: '1 upgrade recommended.',
    };

    vi.mocked(apiClient.analyzeManifest).mockResolvedValueOnce(mockAnalysis);
    vi.mocked(apiClient.generateRecommendations).mockResolvedValueOnce(mockRecs);

    render(<ManifestAnalyzer />);

    await waitFor(() => {
      expect(screen.getByTestId('ai-provider-status')).toBeInTheDocument();
    });
    expect(screen.getByText(/Ollama/)).toBeInTheDocument();

    // Analyze manifest first
    fireEvent.click(screen.getByTestId('btn-analyze'));
    await waitFor(() => {
      expect(screen.getByTestId('recommendations-trigger-card')).toBeInTheDocument();
    });

    // Click 'Get Upgrade Recommendations'
    fireEvent.click(screen.getByTestId('btn-generate-recommendations'));

    await waitFor(() => {
      expect(screen.getByTestId('recommendations-results')).toBeInTheDocument();
    });

    expect(screen.getByText('1 Upgrades Recommended')).toBeInTheDocument();
    expect(screen.getAllByText('2.32.3').length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText('✓ Validated')).toBeInTheDocument();
    expect(screen.getByText('LOW')).toBeInTheDocument();
  });

  it('handles offline deterministic fallback recommendations', async () => {
    vi.mocked(apiClient.getAiStatus).mockResolvedValueOnce({
      is_available: false,
      provider_name: 'ollama',
      endpoint: 'http://127.0.0.1:11434',
      discovered_models: [],
      default_model: null,
      message: 'Offline',
    });

    const mockAnalysis: apiClient.ManifestAnalysisResponse = {
      manifest_type: 'package.json',
      ecosystem: 'npm',
      total_dependencies: 1,
      up_to_date_count: 0,
      upgrade_available_count: 1,
      preserved_count: 0,
      dependencies: [
        {
          name: 'lodash',
          original_name: 'lodash',
          ecosystem: 'npm',
          dependency_type: 'runtime',
          declared_specifier: '4.17.20',
          current_version: '4.17.20',
          latest_version: '4.17.21',
          latest_compatible_version: '4.17.21',
          status: 'UPGRADE_AVAILABLE',
          status_reason: 'Upgrade available',
          registry_status: 'SUCCESS',
          available_versions: ['4.17.20', '4.17.21'],
          yanked_or_deprecated: false,
          deprecation_reason: null,
          line_number: 5,
          raw_line: '"lodash": "4.17.20"',
          diagnostic_message: null,
        },
      ],
      diagnostics: [],
    };

    const mockFallbackRecs: apiClient.RecommendationBatchResponse = {
      manifest_type: 'package.json',
      ecosystem: 'npm',
      recommendations: [
        {
          package_name: 'lodash',
          ecosystem: 'npm',
          current_version: '4.17.20',
          declared_specifier: '4.17.20',
          latest_registry_version: '4.17.21',
          candidate_version: '4.17.21',
          category: 'RECOMMENDED',
          priority: 'LOW',
          explanation: 'Deterministic patch upgrade.',
          breaking_changes_summary: null,
          confidence_score: 1.0,
          provider_used: 'deterministic',
          model_used: 'deterministic-semver',
          is_ai_generated: false,
          validation_status: 'FALLBACK_DETERMINISTIC',
          raw_line: '"lodash": "4.17.20"',
          line_number: 5,
        },
      ],
      provider_used: 'deterministic',
      model_used: 'deterministic-semver',
      is_offline_fallback: true,
      total_upgrades_recommended: 1,
      total_caution: 0,
      summary: '1 upgrade recommended. Engine: deterministic [Offline Fallback Active].',
    };

    vi.mocked(apiClient.analyzeManifest).mockResolvedValueOnce(mockAnalysis);
    vi.mocked(apiClient.generateRecommendations).mockResolvedValueOnce(mockFallbackRecs);

    render(<ManifestAnalyzer />);

    fireEvent.click(screen.getByTestId('btn-analyze'));
    await waitFor(() => {
      expect(screen.getByTestId('recommendations-trigger-card')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTestId('btn-generate-recommendations'));

    await waitFor(() => {
      expect(screen.getByTestId('recommendations-results')).toBeInTheDocument();
    });

    expect(screen.getByText('[Deterministic Fallback Active]')).toBeInTheDocument();
    expect(screen.getByText('Rules')).toBeInTheDocument();
  });
});
