import React, { useState, useEffect } from 'react';
import type {
  AIProviderStatusResponse,
  DependencyStatus,
  ManifestAnalysisResponse,
  RecommendationBatchResponse,
  UpgradeProposalResponse,
} from '../api/client';
import {
  analyzeManifest,
  generateRecommendations,
  generateUpgradePlan,
  getAiStatus,
  uploadManifest,
} from '../api/client';
import { SandboxValidation } from './SandboxValidation';
import { PlanReviewer } from './PlanReviewer';

const PYTHON_SAMPLE = `# Production dependencies
requests==2.28.0
fastapi==0.115.0
pydantic==2.8.2
# Testing and dev tooling
pytest>=7.0.0
uvicorn~=0.30.0
# Preserved directive
-r base-requirements.txt
`;

const NPM_SAMPLE = `{
  "name": "sample-service",
  "version": "1.0.0",
  "dependencies": {
    "express": "^4.18.0",
    "lodash": "4.17.21",
    "react": "18.2.0"
  },
  "devDependencies": {
    "vitest": "^2.1.0",
    "@types/node": ">=20.0.0"
  }
}`;

export const ManifestAnalyzer: React.FC = () => {
  const [activeTab, setActiveTab] = useState<'raw' | 'upload'>('raw');
  const [manifestType, setManifestType] = useState<string>('requirements.txt');
  const [rawContent, setRawContent] = useState<string>(PYTHON_SAMPLE);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);

  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [errorDiagnostics, setErrorDiagnostics] = useState<string[]>([]);
  const [analysisResult, setAnalysisResult] = useState<ManifestAnalysisResponse | null>(null);

  // Recommendations state
  const [aiStatus, setAiStatus] = useState<AIProviderStatusResponse | null>(null);
  const [selectedModel, setSelectedModel] = useState<string>('');
  const [forceDeterministic, setForceDeterministic] = useState<boolean>(false);
  const [isGeneratingRecs, setIsGeneratingRecs] = useState<boolean>(false);
  const [recsResult, setRecsResult] = useState<RecommendationBatchResponse | null>(null);
  const [recsError, setRecsError] = useState<string | null>(null);

  // M4/M7 Planning & Approval State
  const [isGeneratingPlan, setIsGeneratingPlan] = useState<boolean>(false);
  const [proposal, setProposal] = useState<UpgradeProposalResponse | null>(null);
  const [planError, setPlanError] = useState<string | null>(null);
  const [isApproved, setIsApproved] = useState<boolean>(false);
  const [approvalNote, setApprovalNote] = useState<string>('');

  const [statusFilter, setStatusFilter] = useState<'ALL' | DependencyStatus>('ALL');
  const [searchQuery, setSearchQuery] = useState<string>('');
  const [expandedRows, setExpandedRows] = useState<Record<string, boolean>>({});
  const [expandedRecRows, setExpandedRecRows] = useState<Record<string, boolean>>({});

  useEffect(() => {
    // Check AI provider status on mount
    getAiStatus()
      .then((status) => {
        setAiStatus(status);
        if (status.default_model) {
          setSelectedModel(status.default_model);
        } else if (status.discovered_models.length > 0) {
          setSelectedModel(status.discovered_models[0].name);
        }
      })
      .catch(() => {
        setAiStatus({
          is_available: false,
          provider_name: 'ollama',
          endpoint: 'http://127.0.0.1:11434',
          discovered_models: [],
          default_model: null,
          message: 'Offline',
        });
      });
  }, []);

  const handleManifestTypeChange = (type: string) => {
    setManifestType(type);
    if (type === 'requirements.txt') {
      setRawContent(PYTHON_SAMPLE);
    } else {
      setRawContent(NPM_SAMPLE);
    }
  };

  const handleLoadSample = () => {
    if (manifestType === 'requirements.txt') {
      setRawContent(PYTHON_SAMPLE);
    } else {
      setRawContent(NPM_SAMPLE);
    }
    setErrorMessage(null);
    setErrorDiagnostics([]);
  };

  const handleFileDrop = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
      const file = e.dataTransfer.files[0];
      setSelectedFile(file);
      if (file.name.endsWith('.json')) {
        setManifestType('package.json');
      } else if (file.name.endsWith('.txt')) {
        setManifestType('requirements.txt');
      }
    }
  };

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files.length > 0) {
      const file = e.target.files[0];
      setSelectedFile(file);
      if (file.name.endsWith('.json')) {
        setManifestType('package.json');
      } else if (file.name.endsWith('.txt')) {
        setManifestType('requirements.txt');
      }
    }
  };

  const handleAnalyze = async () => {
    setIsLoading(true);
    setErrorMessage(null);
    setErrorDiagnostics([]);
    setAnalysisResult(null);
    setRecsResult(null);
    setRecsError(null);
    setProposal(null);
    setPlanError(null);
    setIsApproved(false);
    setApprovalNote('');

    try {
      let result: ManifestAnalysisResponse;
      if (activeTab === 'raw') {
        if (!rawContent.trim()) {
          throw new Error('Manifest content cannot be empty.');
        }
        result = await analyzeManifest({
          manifest_type: manifestType,
          content: rawContent,
        });
      } else {
        if (!selectedFile) {
          throw new Error('Please select a manifest file to upload.');
        }
        result = await uploadManifest(selectedFile, manifestType);
      }
      setAnalysisResult(result);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Analysis failed';
      setErrorMessage(msg);
      if (err && typeof err === 'object' && 'details' in err) {
        const details = (err as { details?: { diagnostics?: string[] } }).details;
        if (details && Array.isArray(details.diagnostics)) {
          setErrorDiagnostics(details.diagnostics);
        }
      }
    } finally {
      setIsLoading(false);
    }
  };

  const handleGenerateRecommendations = async () => {
    if (!analysisResult) return;

    setIsGeneratingRecs(true);
    setRecsError(null);

    try {
      const result = await generateRecommendations({
        manifest_analysis: analysisResult,
        preferred_model: selectedModel || undefined,
        force_deterministic: forceDeterministic,
      });
      setRecsResult(result);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Failed to generate recommendations';
      setRecsError(msg);
    } finally {
      setIsGeneratingRecs(false);
    }
  };

  const handleGeneratePlan = async () => {
    if (!recsResult || recsResult.recommendations.length === 0) {
      setPlanError('No recommendations available to construct an upgrade plan.');
      return;
    }

    setIsGeneratingPlan(true);
    setPlanError(null);

    try {
      const plan = await generateUpgradePlan({
        manifest_type: manifestType,
        original_content: rawContent,
        recommendations: recsResult.recommendations,
        file_path: manifestType,
      });
      setProposal(plan);
      setIsApproved(false);
      setApprovalNote('');
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Failed to generate upgrade plan.';
      setPlanError(msg);
    } finally {
      setIsGeneratingPlan(false);
    }
  };

  const handleApproveProposal = (note?: string) => {
    setIsApproved(true);
    if (note) setApprovalNote(note);
  };

  const toggleRowExpansion = (rowKey: string) => {
    setExpandedRows((prev) => ({
      ...prev,
      [rowKey]: !prev[rowKey],
    }));
  };

  const toggleRecRowExpansion = (rowKey: string) => {
    setExpandedRecRows((prev) => ({
      ...prev,
      [rowKey]: !prev[rowKey],
    }));
  };

  const filteredDependencies = analysisResult
    ? analysisResult.dependencies.filter((dep) => {
        if (statusFilter !== 'ALL' && dep.status !== statusFilter) {
          return false;
        }
        if (searchQuery.trim()) {
          const q = searchQuery.toLowerCase();
          return (
            dep.name.toLowerCase().includes(q) ||
            dep.original_name.toLowerCase().includes(q)
          );
        }
        return true;
      })
    : [];

  return (
    <div className="manifest-analyzer-container" data-testid="manifest-analyzer">
      <div className="section-header">
        <div>
          <h2 className="section-title">Manifest Ingestion &amp; Dependency Analysis</h2>
          <p className="section-subtitle">
            Deterministic evaluation of Python (requirements.txt) and Node.js (package.json) dependencies against PyPI and npm.
          </p>
        </div>

        {/* AI Provider Status Pill */}
        {aiStatus && (
          <div
            className={`ai-provider-pill ${aiStatus.is_available ? 'active' : 'fallback'}`}
            data-testid="ai-provider-status"
            title={aiStatus.message}
          >
            <span className="status-dot" />
            <span className="provider-name">
              {aiStatus.is_available ? `Ollama (${aiStatus.discovered_models.length} models)` : 'Deterministic Engine'}
            </span>
          </div>
        )}
      </div>

      {/* Tabs and Controls Bar */}
      <div className="controls-card">
        <div className="controls-header">
          <div className="tab-buttons">
            <button
              type="button"
              className={`tab-btn ${activeTab === 'raw' ? 'active' : ''}`}
              onClick={() => setActiveTab('raw')}
              data-testid="tab-raw"
            >
              Raw Editor
            </button>
            <button
              type="button"
              className={`tab-btn ${activeTab === 'upload' ? 'active' : ''}`}
              onClick={() => setActiveTab('upload')}
              data-testid="tab-upload"
            >
              File Upload
            </button>
          </div>

          <div className="format-selector">
            <label htmlFor="manifest-type-select" className="format-label">
              Format:
            </label>
            <select
              id="manifest-type-select"
              value={manifestType}
              onChange={(e) => handleManifestTypeChange(e.target.value)}
              className="select-dropdown"
              data-testid="manifest-type-select"
            >
              <option value="requirements.txt">Python (requirements.txt)</option>
              <option value="package.json">Node.js (package.json)</option>
            </select>

            {activeTab === 'raw' && (
              <button
                type="button"
                className="btn-secondary btn-sm"
                onClick={handleLoadSample}
                title="Reset to sample manifest"
                data-testid="btn-load-sample"
              >
                Reset Sample
              </button>
            )}
          </div>
        </div>

        {/* Tab 1: Raw Content Editor */}
        {activeTab === 'raw' && (
          <div className="editor-container">
            <textarea
              className="raw-textarea font-mono"
              rows={9}
              value={rawContent}
              onChange={(e) => setRawContent(e.target.value)}
              placeholder="Paste requirements.txt or package.json contents here..."
              spellCheck={false}
              data-testid="raw-content-input"
            />
          </div>
        )}

        {/* Tab 2: File Upload Dropzone */}
        {activeTab === 'upload' && (
          <div
            className="dropzone-container"
            onDragOver={(e) => e.preventDefault()}
            onDrop={handleFileDrop}
            data-testid="dropzone"
          >
            <div className="dropzone-content">
              <svg className="dropzone-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12" />
              </svg>
              <p className="dropzone-text">
                {selectedFile ? (
                  <span className="selected-filename">Selected: {selectedFile.name} ({(selectedFile.size / 1024).toFixed(1)} KB)</span>
                ) : (
                  <span>Drag and drop <code>requirements.txt</code> or <code>package.json</code>, or browse</span>
                )}
              </p>
              <label className="btn-secondary btn-browse">
                Browse Files
                <input
                  type="file"
                  onChange={handleFileChange}
                  accept=".txt,.json"
                  className="hidden-file-input"
                  data-testid="file-input"
                />
              </label>
            </div>
          </div>
        )}

        {/* Primary Action Button */}
        <div className="action-row">
          <button
            type="button"
            className="btn-primary"
            onClick={handleAnalyze}
            disabled={isLoading}
            data-testid="btn-analyze"
          >
            {isLoading ? (
              <>
                <span className="spinner" /> Analyzing Registry Metadata...
              </>
            ) : (
              'Analyze Manifest'
            )}
          </button>
        </div>
      </div>

      {/* Error & Diagnostics Notification */}
      {errorMessage && (
        <div className="error-alert error-banner" data-testid="error-banner">
          <div className="error-alert-header">
            <strong>Analysis Error:</strong> {errorMessage}
          </div>
          {errorDiagnostics.length > 0 && (
            <ul className="error-diagnostics-list">
              {errorDiagnostics.map((d, idx) => (
                <li key={idx}>{d}</li>
              ))}
            </ul>
          )}
        </div>
      )}

      {/* Analysis Results Display */}
      {analysisResult && (
        <div className="analysis-results-section" data-testid="analysis-results">
          {/* Summary Metrics */}
          <div className="summary-metrics-grid">
            <div className="metric-box total">
              <span className="metric-label">Total Dependencies</span>
              <span className="metric-value">{analysisResult.total_dependencies}</span>
            </div>
            <div className="metric-box up-to-date">
              <span className="metric-label">Up to Date</span>
              <span className="metric-value">{analysisResult.up_to_date_count}</span>
            </div>
            <div className="metric-box upgrade-available">
              <span className="metric-label">Upgrade Available</span>
              <span className="metric-value">{analysisResult.upgrade_available_count}</span>
            </div>
            <div className="metric-box preserved">
              <span className="metric-label">Preserved / Directives</span>
              <span className="metric-value">{analysisResult.preserved_count}</span>
            </div>
          </div>

          {/* AI Upgrade Recommendations Action Card */}
          <div className="recommendations-trigger-card" data-testid="recommendations-trigger-card">
            <div className="rec-trigger-header">
              <div>
                <h3 className="rec-trigger-title">Upgrade Recommendations</h3>
                <p className="rec-trigger-desc">
                  Generate structured candidate upgrades with SemVer safety categorization and registry validation.
                </p>
              </div>

              <div className="rec-trigger-actions">
                {aiStatus?.is_available && aiStatus.discovered_models.length > 0 && (
                  <div className="model-selector-group">
                    <label htmlFor="ai-model-select" className="model-label">
                      Model:
                    </label>
                    <select
                      id="ai-model-select"
                      value={selectedModel}
                      onChange={(e) => setSelectedModel(e.target.value)}
                      className="select-dropdown select-sm"
                      data-testid="ai-model-select"
                      disabled={forceDeterministic}
                    >
                      {aiStatus.discovered_models.map((m) => (
                        <option key={m.name} value={m.name}>
                          {m.name} {m.parameter_size ? `(${m.parameter_size})` : ''}
                        </option>
                      ))}
                    </select>
                  </div>
                )}

                <label className="checkbox-label" title="Bypass LLM and use deterministic rule-based evaluation">
                  <input
                    type="checkbox"
                    checked={forceDeterministic}
                    onChange={(e) => setForceDeterministic(e.target.checked)}
                    data-testid="force-deterministic-checkbox"
                  />
                  <span>Deterministic only</span>
                </label>

                <button
                  type="button"
                  className="btn-accent"
                  onClick={handleGenerateRecommendations}
                  disabled={isGeneratingRecs}
                  data-testid="btn-generate-recommendations"
                >
                  {isGeneratingRecs ? (
                    <>
                      <span className="spinner" /> Generating...
                    </>
                  ) : (
                    'Get Upgrade Recommendations'
                  )}
                </button>
              </div>
            </div>

            {recsError && (
              <div className="error-alert rec-error-alert" data-testid="recs-error-alert">
                <strong>Recommendation Error:</strong> {recsError}
              </div>
            )}
          </div>

          {/* Recommendations Result Card */}
          {recsResult && (
            <div className="recommendations-results-card" data-testid="recommendations-results">
              <div className="recs-summary-header">
                <div className="recs-meta-info">
                  <span className="recs-count-tag">
                    {recsResult.total_upgrades_recommended} Upgrades Recommended
                  </span>
                  {recsResult.total_caution > 0 && (
                    <span className="recs-caution-tag">
                      {recsResult.total_caution} Breaking Changes / Caution
                    </span>
                  )}
                  <span className="recs-engine-tag">
                    Engine: <strong>{recsResult.provider_used}</strong>
                    {recsResult.model_used && ` (${recsResult.model_used})`}
                  </span>
                  {recsResult.is_offline_fallback && (
                    <span className="fallback-indicator" title="Ollama offline: zero-hallucination deterministic fallback active">
                      [Deterministic Fallback Active]
                    </span>
                  )}
                </div>
                <p className="recs-summary-text">{recsResult.summary}</p>
              </div>

              <div className="table-wrapper">
                <table className="dependencies-table recs-table">
                  <thead>
                    <tr>
                      <th>Package</th>
                      <th>Current</th>
                      <th>Candidate</th>
                      <th>Category</th>
                      <th>Priority</th>
                      <th>Validation</th>
                      <th>Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    {recsResult.recommendations.map((rec, idx) => {
                      const recKey = `rec-${rec.ecosystem}-${rec.package_name}-${idx}`;
                      const isExpanded = !!expandedRecRows[recKey];

                      return (
                        <React.Fragment key={recKey}>
                          <tr className={`rec-row ${rec.category.toLowerCase()}`}>
                            <td className="dep-name-cell font-mono font-bold">
                              {rec.package_name}
                            </td>
                            <td className="code-cell font-mono">{rec.current_version || '—'}</td>
                            <td className="code-cell font-mono font-bold candidate-cell">
                              {rec.candidate_version ? (
                                <span className="candidate-version-tag">
                                  {rec.candidate_version}
                                </span>
                              ) : (
                                '—'
                              )}
                            </td>
                            <td>
                              <span className={`status-badge category-${rec.category.toLowerCase()}`}>
                                {rec.category}
                              </span>
                            </td>
                            <td>
                              <span className={`priority-badge priority-${rec.priority.toLowerCase()}`}>
                                {rec.priority}
                              </span>
                            </td>
                            <td>
                              <span
                                className={`val-badge val-${rec.validation_status.toLowerCase()}`}
                                title={`Registry Ground-Truth: ${rec.validation_status}`}
                              >
                                {rec.validation_status === 'VALIDATED' && '✓ Validated'}
                                {rec.validation_status === 'REJECTED_HALLUCINATION' && '⚠ Reverted'}
                                {rec.validation_status === 'REJECTED_INCOMPATIBLE' && '⚠ Incompatible'}
                                {rec.validation_status === 'FALLBACK_DETERMINISTIC' && 'Rules'}
                                {rec.validation_status === 'SKIPPED' && '—'}
                              </span>
                            </td>
                            <td>
                              <button
                                type="button"
                                className="btn-details-toggle"
                                onClick={() => toggleRecRowExpansion(recKey)}
                                aria-expanded={isExpanded}
                                title="Toggle details"
                              >
                                {isExpanded ? 'Hide' : 'Details'}
                              </button>
                            </td>
                          </tr>

                          {isExpanded && (
                            <tr className="details-expanded-row">
                              <td colSpan={7}>
                                <div className="details-box">
                                  <div className="details-line">
                                    <strong>Explanation:</strong> {rec.explanation}
                                  </div>
                                  {rec.breaking_changes_summary && (
                                    <div className="details-line warning">
                                      <strong>Risk Assessment:</strong> {rec.breaking_changes_summary}
                                    </div>
                                  )}
                                  <div className="details-line meta-subline">
                                    <span>
                                      <strong>Confidence:</strong> {(rec.confidence_score * 100).toFixed(0)}%
                                    </span>
                                    <span>
                                      <strong>Source:</strong> {rec.is_ai_generated ? 'AI Model' : 'Deterministic Rules'}
                                    </span>
                                    {rec.latest_registry_version && (
                                      <span>
                                        <strong>Registry Latest:</strong> {rec.latest_registry_version}
                                      </span>
                                    )}
                                  </div>
                                </div>
                              </td>
                            </tr>
                          )}
                        </React.Fragment>
                      );
                    })}
                  </tbody>
                </table>
              </div>

              {/* Upgrade Planning Action Bar */}
              <div className="plan-trigger-card" data-testid="plan-trigger-section">
                <div className="plan-trigger-info">
                  <h4>Deterministic Upgrade Plan &amp; Patch Synthesis</h4>
                  <p>
                    Compile validated recommendations into an atomic upgrade proposal, generate unified Git diffs,
                    and verify patch applicability.
                  </p>
                </div>
                <button
                  type="button"
                  className="btn-primary btn-plan-trigger"
                  onClick={handleGeneratePlan}
                  disabled={isGeneratingPlan || recsResult.recommendations.length === 0}
                  data-testid="btn-generate-plan"
                >
                  {isGeneratingPlan ? 'Synthesizing Plan & Unified Diff...' : 'Generate Upgrade Plan & Unified Diff'}
                </button>
              </div>

              {planError && (
                <div className="alert-banner alert-error" role="alert" data-testid="plan-error-banner">
                  <span className="alert-icon">⚠</span>
                  <span className="alert-message">{planError}</span>
                </div>
              )}
            </div>
          )}

          {/* Filtering Controls */}
          <div className="table-controls-bar">
            <div className="filter-chips">
              <button
                type="button"
                className={`filter-chip ${statusFilter === 'ALL' ? 'active' : ''}`}
                onClick={() => setStatusFilter('ALL')}
                data-testid="filter-all"
              >
                All ({analysisResult.dependencies.length})
              </button>
              <button
                type="button"
                className={`filter-chip amber ${statusFilter === 'UPGRADE_AVAILABLE' ? 'active' : ''}`}
                onClick={() => setStatusFilter('UPGRADE_AVAILABLE')}
                data-testid="filter-upgrade-available"
              >
                Upgrades ({analysisResult.upgrade_available_count})
              </button>
              <button
                type="button"
                className={`filter-chip emerald ${statusFilter === 'UP_TO_DATE' ? 'active' : ''}`}
                onClick={() => setStatusFilter('UP_TO_DATE')}
                data-testid="filter-up-to-date"
              >
                Up to Date ({analysisResult.up_to_date_count})
              </button>
              <button
                type="button"
                className={`filter-chip indigo ${statusFilter === 'PRESERVED' ? 'active' : ''}`}
                onClick={() => setStatusFilter('PRESERVED')}
                data-testid="filter-preserved"
              >
                Preserved ({analysisResult.preserved_count})
              </button>
            </div>

            <input
              type="text"
              className="search-input"
              placeholder="Filter by package name..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              data-testid="search-filter-input"
            />
          </div>

          {/* High-density Dependencies Table */}
          <div className="table-wrapper">
            <table className="dependencies-table">
              <thead>
                <tr>
                  <th>Package</th>
                  <th>Ecosystem</th>
                  <th>Declared Specifier</th>
                  <th>Resolved</th>
                  <th>Compatible Latest</th>
                  <th>Registry Latest</th>
                  <th>Evaluation</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {filteredDependencies.length === 0 ? (
                  <tr>
                    <td colSpan={8} className="empty-table-cell">
                      No dependencies match the selected filter.
                    </td>
                  </tr>
                ) : (
                  filteredDependencies.map((dep, idx) => {
                    const rowKey = `${dep.ecosystem}-${dep.name}-${idx}`;
                    const isExpanded = !!expandedRows[rowKey];

                    return (
                      <React.Fragment key={rowKey}>
                        <tr className={`dep-row ${dep.status.toLowerCase()}`}>
                          <td className="dep-name-cell">
                            <span className="pkg-name">{dep.original_name}</span>
                            <span className={`scope-badge ${dep.dependency_type}`}>
                              {dep.dependency_type === 'runtime' ? 'run' : 'dev'}
                            </span>
                            {dep.yanked_or_deprecated && (
                              <span className="deprecated-tag" title={dep.deprecation_reason || 'Deprecated / Yanked'}>
                                ⚠ Deprecated
                              </span>
                            )}
                          </td>
                          <td className="dep-eco-cell">
                            <span className="eco-badge">{dep.ecosystem}</span>
                          </td>
                          <td className="code-cell font-mono">{dep.declared_specifier || '—'}</td>
                          <td className="code-cell font-mono">{dep.current_version || '—'}</td>
                          <td className="code-cell font-mono">{dep.latest_compatible_version || '—'}</td>
                          <td className="code-cell font-mono font-bold">{dep.latest_version || '—'}</td>
                          <td>
                            <span className={`status-badge status-${dep.status.toLowerCase()}`}>
                              {dep.status === 'UP_TO_DATE' && '✓ Up to Date'}
                              {dep.status === 'UPGRADE_AVAILABLE' && '▲ Upgrade Available'}
                              {dep.status === 'PRESERVED' && 'ℹ Preserved'}
                            </span>
                          </td>
                          <td>
                            <button
                              type="button"
                              className="btn-details-toggle"
                              onClick={() => toggleRowExpansion(rowKey)}
                              aria-expanded={isExpanded}
                              title="Toggle details"
                            >
                              {isExpanded ? 'Hide' : 'Details'}
                            </button>
                          </td>
                        </tr>

                        {isExpanded && (
                          <tr className="details-expanded-row">
                            <td colSpan={8}>
                              <div className="details-box">
                                <div className="details-line">
                                    <strong>Evaluation:</strong> {dep.status_reason}
                                </div>
                                {dep.line_number && (
                                  <div className="details-line">
                                    <strong>Source line:</strong> Line {dep.line_number}
                                    {dep.raw_line && <code>: {dep.raw_line}</code>}
                                  </div>
                                )}
                                {dep.diagnostic_message && (
                                  <div className="details-line warning">
                                    <strong>Diagnostic:</strong> {dep.diagnostic_message}
                                  </div>
                                )}
                                {dep.available_versions.length > 0 && (
                                  <div className="details-line">
                                    <strong>Sample Releases:</strong>{' '}
                                    <span className="versions-list">
                                      {dep.available_versions.join(', ')}
                                    </span>
                                  </div>
                                )}
                              </div>
                            </td>
                          </tr>
                        )}
                      </React.Fragment>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>

          {/* Global Diagnostics Banner */}
          {analysisResult.diagnostics.length > 0 && (
            <div className="diagnostics-card" data-testid="diagnostics-card">
              <h4 className="diagnostics-title">Manifest Diagnostics &amp; Notices</h4>
              <ul className="diagnostics-list">
                {analysisResult.diagnostics.map((diag, i) => (
                  <li key={i}>{diag}</li>
                ))}
              </ul>
            </div>
          )}

          {/* M4/M7 Upgrade Plan, Unified Diff & Human Approval Section */}
          {proposal && (
            <PlanReviewer
              proposal={proposal}
              onApprove={handleApproveProposal}
              isApproved={isApproved}
              approvalNote={approvalNote}
            />
          )}

          {/* Sandboxed Validation Subsystem */}
          <SandboxValidation
            proposal={proposal}
            manifestType={manifestType}
            originalContent={rawContent}
            isApproved={isApproved}
          />
        </div>
      )}
    </div>
  );
};
