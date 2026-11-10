import React, { useState } from 'react';
import type {
  DependencyStatus,
  ManifestAnalysisResponse,
} from '../api/client';
import {
  analyzeManifest,
  uploadManifest,
} from '../api/client';

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

  const [statusFilter, setStatusFilter] = useState<'ALL' | DependencyStatus>('ALL');
  const [searchQuery, setSearchQuery] = useState<string>('');
  const [expandedRows, setExpandedRows] = useState<Record<string, boolean>>({});

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
  };

  const handleClear = () => {
    setRawContent('');
    setSelectedFile(null);
    setAnalysisResult(null);
    setErrorMessage(null);
    setErrorDiagnostics([]);
  };

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files[0]) {
      const file = e.target.files[0];
      setSelectedFile(file);
      setErrorMessage(null);
      // Auto-update manifest type selector if possible
      if (file.name.endsWith('.json') || file.name.includes('package')) {
        setManifestType('package.json');
      } else if (file.name.endsWith('.txt') || file.name.includes('requirements')) {
        setManifestType('requirements.txt');
      }
    }
  };

  const handleDrop = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    if (e.dataTransfer.files && e.dataTransfer.files[0]) {
      const file = e.dataTransfer.files[0];
      setSelectedFile(file);
      setErrorMessage(null);
      if (file.name.endsWith('.json') || file.name.includes('package')) {
        setManifestType('package.json');
      } else if (file.name.endsWith('.txt') || file.name.includes('requirements')) {
        setManifestType('requirements.txt');
      }
    }
  };

  const handleAnalyze = async () => {
    setIsLoading(true);
    setErrorMessage(null);
    setErrorDiagnostics([]);

    try {
      let result: ManifestAnalysisResponse;
      if (activeTab === 'raw') {
        if (!rawContent.trim()) {
          throw new Error('Please enter manifest content to analyze.');
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
      if (err && typeof err === 'object' && 'details' in err) {
        const anyErr = err as { message: string; details?: { diagnostics?: string[] } };
        setErrorMessage(anyErr.message || 'Manifest analysis failed');
        if (anyErr.details?.diagnostics) {
          setErrorDiagnostics(anyErr.details.diagnostics);
        }
      } else if (err instanceof Error) {
        setErrorMessage(err.message);
      } else {
        setErrorMessage('An unexpected error occurred during analysis.');
      }
    } finally {
      setIsLoading(false);
    }
  };

  const toggleRowExpansion = (key: string) => {
    setExpandedRows((prev) => ({ ...prev, [key]: !prev[key] }));
  };

  const filteredDependencies = (analysisResult?.dependencies || []).filter((dep) => {
    if (statusFilter !== 'ALL' && dep.status !== statusFilter) {
      return false;
    }
    if (searchQuery.trim()) {
      const q = searchQuery.toLowerCase();
      return (
        dep.name.toLowerCase().includes(q) ||
        dep.original_name.toLowerCase().includes(q) ||
        dep.declared_specifier.toLowerCase().includes(q)
      );
    }
    return true;
  });

  return (
    <div className="manifest-analyzer-card" data-testid="manifest-analyzer">
      <div className="manifest-analyzer-header">
        <div>
          <h2 className="analyzer-title">Manifest Ingestion &amp; Dependency Analysis</h2>
          <p className="analyzer-desc">
            Deterministic evaluation of Python (PEP 440/508) and Node.js (SemVer) manifests with PyPI and npm registry queries.
          </p>
        </div>
        <div className="tab-pill-group">
          <button
            type="button"
            className={`tab-pill ${activeTab === 'raw' ? 'active' : ''}`}
            onClick={() => setActiveTab('raw')}
            data-testid="tab-raw"
          >
            Raw Editor
          </button>
          <button
            type="button"
            className={`tab-pill ${activeTab === 'upload' ? 'active' : ''}`}
            onClick={() => setActiveTab('upload')}
            data-testid="tab-upload"
          >
            Upload File
          </button>
        </div>
      </div>

      <div className="analyzer-input-section">
        <div className="format-bar">
          <label className="format-label">
            Ecosystem Format:
            <select
              className="format-select"
              value={manifestType}
              onChange={(e) => handleManifestTypeChange(e.target.value)}
              data-testid="manifest-type-select"
            >
              <option value="requirements.txt">Python (requirements.txt)</option>
              <option value="package.json">Node.js (package.json)</option>
            </select>
          </label>

          <div className="format-actions">
            {activeTab === 'raw' && (
              <button
                type="button"
                className="btn-secondary"
                onClick={handleLoadSample}
                data-testid="btn-load-sample"
              >
                Load Sample
              </button>
            )}
            <button
              type="button"
              className="btn-secondary"
              onClick={handleClear}
              data-testid="btn-clear"
            >
              Clear
            </button>
          </div>
        </div>

        {activeTab === 'raw' ? (
          <div className="editor-container">
            <textarea
              className="raw-manifest-editor"
              value={rawContent}
              onChange={(e) => setRawContent(e.target.value)}
              placeholder={
                manifestType === 'requirements.txt'
                  ? 'e.g.\nrequests==2.28.0\nfastapi==0.115.0\npytest>=7.0.0'
                  : 'e.g.\n{\n  "dependencies": {\n    "express": "^4.18.0"\n  }\n}'
              }
              rows={8}
              spellCheck={false}
              data-testid="raw-content-input"
            />
          </div>
        ) : (
          <div
            className="dropzone-container"
            onDragOver={(e) => e.preventDefault()}
            onDrop={handleDrop}
            data-testid="dropzone"
          >
            <input
              type="file"
              id="manifest-file-input"
              className="hidden-file-input"
              accept=".txt,.json"
              onChange={handleFileChange}
              data-testid="file-input"
            />
            <label htmlFor="manifest-file-input" className="dropzone-label">
              <span className="dropzone-icon">📁</span>
              <span className="dropzone-text">
                {selectedFile ? (
                  <strong>Selected: {selectedFile.name} ({(selectedFile.size / 1024).toFixed(1)} KB)</strong>
                ) : (
                  <>Drag &amp; drop <code>requirements.txt</code> or <code>package.json</code> here, or <span className="browse-link">browse</span></>
                )}
              </span>
            </label>
          </div>
        )}

        <div className="submit-bar">
          <button
            type="button"
            className="btn-primary analyze-submit-btn"
            onClick={handleAnalyze}
            disabled={isLoading}
            data-testid="btn-analyze"
          >
            {isLoading ? (
              <>
                <span className="spinner-inline" /> Analyzing Dependencies...
              </>
            ) : (
              'Analyze Manifest'
            )}
          </button>
        </div>
      </div>

      {errorMessage && (
        <div className="error-banner" data-testid="error-banner">
          <div className="error-header">
            <span className="error-icon">⚠</span>
            <strong>{errorMessage}</strong>
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
                  <th>Current Pinned</th>
                  <th>Latest Compatible</th>
                  <th>Latest Release</th>
                  <th>Status</th>
                  <th>Details</th>
                </tr>
              </thead>
              <tbody>
                {filteredDependencies.length === 0 ? (
                  <tr>
                    <td colSpan={8} className="empty-table-cell">
                      No dependencies match the current filter.
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
        </div>
      )}
    </div>
  );
};
