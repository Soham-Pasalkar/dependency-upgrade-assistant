import React, { useEffect, useRef, useState } from 'react';
import type {
  AIProviderStatusResponse,
  DependencyStatus,
  ExportMetadataResponse,
  PackageUpgradeRecommendation,
  RecommendationBatchResponse,
  RunManifest,
  RunResponse,
  UpgradeProposalResponse,
  ValidationReportResponse,
} from '../api/client';
import {
  analyzeRepository,
  approveProposal,
  createManifestRun,
  generateRunPlan,
  generateRunRecommendations,
  getAiStatus,
} from '../api/client';
import { SandboxValidation } from './SandboxValidation';
import { PlanReviewer } from './PlanReviewer';
import { ExportManager } from './ExportManager';
import { StepRail, type RailStep } from './StepRail';

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

const STATUS_LABEL: Record<DependencyStatus, string> = {
  UP_TO_DATE: 'Up to date',
  UPGRADE_AVAILABLE: 'Upgrade available',
  PRESERVED: 'Preserved',
};

const CATEGORY_LABEL: Record<PackageUpgradeRecommendation['category'], string> = {
  RECOMMENDED: 'Recommended',
  CAUTION: 'Review carefully',
  NO_UPGRADE_NEEDED: 'No change needed',
  PRESERVED_UNSUPPORTED: 'Left as is',
};

const titleCase = (s: string) => s.charAt(0) + s.slice(1).toLowerCase();

const EXAMPLE_REPOSITORIES = [
  { label: 'miguelgrinberg/microblog', url: 'https://github.com/miguelgrinberg/microblog' },
  { label: 'expressjs/cors', url: 'https://github.com/expressjs/cors' },
];

const KIND_LABEL: Record<RunManifest['kind'], string> = {
  requirements: 'pip requirements',
  pyproject: 'pyproject.toml',
  package_json: 'npm package',
};

const REGISTRY_PROBLEM: Partial<Record<string, string>> = {
  NOT_FOUND: 'not in registry',
  RATE_LIMITED: 'registry rate-limited',
  NETWORK_ERROR: 'registry unreachable',
  TOO_LARGE: 'registry data too large',
  TIMEOUT: 'registry timed out',
};

/** True when versions came from the bundled offline catalog instead of a live registry lookup. */
const usedOfflineCatalog = (reason?: string | null) => !!reason && reason.startsWith('Serving from local catalog');

const formatBytes = (bytes: number) =>
  bytes < 1024 * 1024 ? `${Math.max(1, Math.round(bytes / 1024))} KB` : `${(bytes / 1048576).toFixed(1)} MB`;

/** How a recommendation's version was established. */
const provenanceOf = (rec: PackageUpgradeRecommendation): { label: string; tone: string; hint: string } => {
  switch (rec.validation_status) {
    case 'VALIDATED':
      return rec.is_ai_generated
        ? { label: 'Model, registry-checked', tone: 'advisory', hint: 'Suggested by the local model; the version was confirmed in the registry.' }
        : { label: 'Registry-checked', tone: 'registry', hint: 'Version confirmed in the registry.' };
    case 'FALLBACK_DETERMINISTIC':
      return { label: 'Rule-based', tone: 'registry', hint: 'Derived from registry data with semantic-versioning rules. No model involved.' };
    case 'REJECTED_HALLUCINATION':
      return { label: 'Model version rejected', tone: 'bad', hint: 'The model proposed a version that does not exist in the registry. It was discarded.' };
    case 'REJECTED_INCOMPATIBLE':
      return { label: 'Incompatible, rejected', tone: 'bad', hint: 'The proposed version does not satisfy the declared constraints.' };
    default:
      return { label: 'Not evaluated', tone: 'muted', hint: 'No recommendation was produced for this entry.' };
  }
};

const scrollToStage = (id: string) => {
  const el = document.getElementById(id);
  if (!el || typeof el.scrollIntoView !== 'function') return;
  const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
  el.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'start' });
  const heading = el.querySelector<HTMLElement>('[data-stage-heading]');
  heading?.focus({ preventScroll: true });
};

export const ManifestAnalyzer: React.FC = () => {
  const [sourceMode, setSourceMode] = useState<'repository' | 'manifest'>('repository');
  const [repoUrl, setRepoUrl] = useState<string>('');
  const [elapsed, setElapsed] = useState<number>(0);
  const [run, setRun] = useState<RunResponse | null>(null);
  const [activeIndex, setActiveIndex] = useState<number | null>(null);
  const [isApproving, setIsApproving] = useState<boolean>(false);
  const [approvalError, setApprovalError] = useState<string | null>(null);

  const [activeTab, setActiveTab] = useState<'raw' | 'upload'>('raw');
  const [manifestType, setManifestType] = useState<string>('requirements.txt');
  const [rawContent, setRawContent] = useState<string>(PYTHON_SAMPLE);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [isDragging, setIsDragging] = useState(false);

  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [errorDiagnostics, setErrorDiagnostics] = useState<string[]>([]);
  const activeManifest = run && activeIndex !== null ? run.manifests.find((m) => m.index === activeIndex) ?? null : null;
  const analysisResult = activeManifest?.analysis ?? null;

  const [aiStatus, setAiStatus] = useState<AIProviderStatusResponse | null>(null);
  const [selectedModel, setSelectedModel] = useState<string>('');
  const [forceDeterministic, setForceDeterministic] = useState<boolean>(false);
  const [isGeneratingRecs, setIsGeneratingRecs] = useState<boolean>(false);
  const [recsResult, setRecsResult] = useState<RecommendationBatchResponse | null>(null);
  const [recsError, setRecsError] = useState<string | null>(null);

  const [isGeneratingPlan, setIsGeneratingPlan] = useState<boolean>(false);
  const [proposal, setProposal] = useState<UpgradeProposalResponse | null>(null);
  const [planError, setPlanError] = useState<string | null>(null);
  const [isApproved, setIsApproved] = useState<boolean>(false);
  const [approvalNote, setApprovalNote] = useState<string>('');

  const [validationReport, setValidationReport] = useState<ValidationReportResponse | null>(null);
  const [exportData, setExportData] = useState<ExportMetadataResponse | null>(null);

  const [statusFilter, setStatusFilter] = useState<'ALL' | DependencyStatus>('ALL');
  const [searchQuery, setSearchQuery] = useState<string>('');
  const [expandedRows, setExpandedRows] = useState<Record<string, boolean>>({});
  const [expandedRecRows, setExpandedRecRows] = useState<Record<string, boolean>>({});

  const pendingScroll = useRef<string | null>(null);

  useEffect(() => {
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
          endpoint: '',
          discovered_models: [],
          default_model: null,
          message: 'Offline',
        });
      });
  }, []);

  // Move focus to a stage once it has rendered.
  useEffect(() => {
    if (pendingScroll.current) {
      const id = pendingScroll.current;
      pendingScroll.current = null;
      requestAnimationFrame(() => scrollToStage(id));
    }
  });

  const resetDownstream = () => {
    setRecsResult(null);
    setRecsError(null);
    setProposal(null);
    setPlanError(null);
    setIsApproved(false);
    setApprovalNote('');
    setValidationReport(null);
    setExportData(null);
  };

  const handleManifestTypeChange = (type: string) => {
    setManifestType(type);
    setRawContent(type === 'requirements.txt' ? PYTHON_SAMPLE : NPM_SAMPLE);
  };

  const handleLoadSample = () => {
    setRawContent(manifestType === 'requirements.txt' ? PYTHON_SAMPLE : NPM_SAMPLE);
    setErrorMessage(null);
    setErrorDiagnostics([]);
  };

  const acceptFile = (file: File) => {
    setSelectedFile(file);
    if (file.name.endsWith('.json')) setManifestType('package.json');
    else if (file.name.endsWith('.txt')) setManifestType('requirements.txt');
  };

  const handleFileDrop = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setIsDragging(false);
    if (e.dataTransfer.files && e.dataTransfer.files.length > 0) acceptFile(e.dataTransfer.files[0]);
  };

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files.length > 0) acceptFile(e.target.files[0]);
  };

  // Elapsed-time readout while a repository is being cloned and analyzed.
  useEffect(() => {
    if (!isLoading || sourceMode !== 'repository') return;
    const startedAt = Date.now();
    const timer = window.setInterval(() => setElapsed(Math.floor((Date.now() - startedAt) / 1000)), 500);
    return () => {
      window.clearInterval(timer);
      setElapsed(0);
    };
  }, [isLoading, sourceMode]);

  const reportError = (err: unknown, fallback: string) => {
    setErrorMessage(err instanceof Error ? err.message : fallback);
    if (err && typeof err === 'object' && 'details' in err) {
      const details = (err as { details?: { diagnostics?: string[] } }).details;
      if (details && Array.isArray(details.diagnostics)) setErrorDiagnostics(details.diagnostics);
    }
  };

  const openManifest = (target: RunResponse, index: number | null, scroll = true) => {
    setActiveIndex(index);
    resetDownstream();
    const m = index !== null ? target.manifests.find((x) => x.index === index) : null;
    if (m?.recommendations) setRecsResult(m.recommendations);
    if (scroll && index !== null) pendingScroll.current = 'stage-health';
  };

  const startAnalysis = () => {
    setIsLoading(true);
    setErrorMessage(null);
    setErrorDiagnostics([]);
    setRun(null);
    setActiveIndex(null);
    resetDownstream();
  };

  const handleAnalyzeRepository = async (urlOverride?: string) => {
    const url = (urlOverride ?? repoUrl).trim();
    if (urlOverride) setRepoUrl(urlOverride);
    if (!url) {
      setErrorMessage('Enter a GitHub repository URL, for example https://github.com/owner/name.');
      return;
    }
    startAnalysis();
    try {
      const result = await analyzeRepository(url);
      setRun(result);
      const usable = result.manifests.filter((m) => m.analysis);
      openManifest(result, usable.length === 1 ? usable[0].index : null, false);
      pendingScroll.current = 'stage-manifests';
    } catch (err: unknown) {
      reportError(err, 'The repository could not be analyzed.');
    } finally {
      setIsLoading(false);
    }
  };

  const handleAnalyze = async () => {
    startAnalysis();
    try {
      let content: string;
      if (activeTab === 'raw') {
        if (!rawContent.trim()) {
          throw new Error('The manifest is empty. Paste its contents or load the sample.');
        }
        content = rawContent;
      } else {
        if (!selectedFile) {
          throw new Error('Choose a requirements.txt or package.json file first.');
        }
        content = await selectedFile.text();
      }
      const result = await createManifestRun(manifestType, content);
      setRun(result);
      openManifest(result, result.manifests[0]?.index ?? null);
    } catch (err: unknown) {
      reportError(err, 'Analysis failed.');
    } finally {
      setIsLoading(false);
    }
  };

  const handleGenerateRecommendations = async () => {
    if (!run || activeIndex === null || !analysisResult) return;
    setIsGeneratingRecs(true);
    setRecsError(null);
    setProposal(null);
    setIsApproved(false);
    setValidationReport(null);
    setExportData(null);
    try {
      const result = await generateRunRecommendations(run.run_id, activeIndex, {
        preferred_model: selectedModel || undefined,
        force_deterministic: forceDeterministic,
      });
      setRecsResult(result);
      setRun((prev) =>
        prev
          ? { ...prev, manifests: prev.manifests.map((m) => (m.index === activeIndex ? { ...m, recommendations: result } : m)) }
          : prev
      );
      pendingScroll.current = 'stage-recommendations';
    } catch (err: unknown) {
      setRecsError(err instanceof Error ? err.message : 'Could not generate recommendations.');
    } finally {
      setIsGeneratingRecs(false);
    }
  };

  const handleGeneratePlan = async () => {
    if (!run || activeIndex === null || !recsResult || recsResult.recommendations.length === 0) {
      setPlanError('There are no recommendations to build a plan from.');
      return;
    }
    setIsGeneratingPlan(true);
    setPlanError(null);
    setValidationReport(null);
    setExportData(null);
    try {
      const plan = await generateRunPlan(run.run_id, activeIndex);
      setProposal(plan);
      setIsApproved(false);
      setApprovalNote('');
      setApprovalError(null);
      pendingScroll.current = 'stage-plan';
    } catch (err: unknown) {
      setPlanError(err instanceof Error ? err.message : 'Could not generate the upgrade plan.');
    } finally {
      setIsGeneratingPlan(false);
    }
  };

  const handleApproveProposal = async (note?: string) => {
    if (!proposal) return;
    setIsApproving(true);
    setApprovalError(null);
    try {
      await approveProposal(proposal.proposal_id, proposal.unified_diff, note);
      setIsApproved(true);
      if (note) setApprovalNote(note);
      pendingScroll.current = 'stage-validation';
    } catch (err: unknown) {
      setApprovalError(err instanceof Error ? err.message : 'Approval could not be recorded.');
    } finally {
      setIsApproving(false);
    }
  };

  const toggle = (setter: React.Dispatch<React.SetStateAction<Record<string, boolean>>>, key: string) =>
    setter((prev) => ({ ...prev, [key]: !prev[key] }));

  const filteredDependencies = analysisResult
    ? analysisResult.dependencies.filter((dep) => {
        if (statusFilter !== 'ALL' && dep.status !== statusFilter) return false;
        if (searchQuery.trim()) {
          const q = searchQuery.toLowerCase();
          return dep.name.toLowerCase().includes(q) || dep.original_name.toLowerCase().includes(q);
        }
        return true;
      })
    : [];

  const steps: RailStep[] = [
    { id: 'stage-source', label: 'Source', state: run ? 'done' : 'current' },
    { id: 'stage-manifests', label: 'Manifests', state: run ? (analysisResult ? 'done' : 'current') : 'locked' },
    { id: 'stage-health', label: 'Dependency health', state: analysisResult ? (recsResult ? 'done' : 'current') : 'locked' },
    { id: 'stage-recommendations', label: 'Recommendations', state: recsResult ? (proposal ? 'done' : 'current') : 'locked' },
    { id: 'stage-plan', label: 'Plan and diff', state: proposal ? (isApproved ? 'done' : 'current') : 'locked' },
    { id: 'stage-approval', label: 'Approval', state: proposal ? (isApproved ? 'done' : 'current') : 'locked' },
    { id: 'stage-validation', label: 'Validation', state: isApproved ? (validationReport ? 'done' : 'current') : 'locked' },
    { id: 'stage-export', label: 'Export', state: isApproved ? (exportData ? 'done' : 'current') : 'locked' },
  ];

  const engineLabel = aiStatus
    ? aiStatus.is_available
      ? `Local model available (${aiStatus.discovered_models.length})`
      : 'Rule-based engine'
    : 'Checking engine';

  return (
    <div className="workspace" data-testid="manifest-analyzer">
      <StepRail steps={steps} onNavigate={scrollToStage} />

      <main className="workspace-main" id="main">
        <header className="workspace-head">
          <h1 className="workspace-title">Analyze dependencies</h1>
          <p className="workspace-lede">
            Point it at a public GitHub repository or paste a manifest. Review each upgrade as a diff,
            and approve it before anything is validated or exported.
          </p>
        </header>

        {/* Stage 1: Source */}
        <section id="stage-source" className="stage" aria-labelledby="stage-source-title">
          <div className="stage-head">
            <span className="stage-index mono">01</span>
            <div>
              <h2 id="stage-source-title" className="stage-title" tabIndex={-1} data-stage-heading>
                Source
              </h2>
              <p className="stage-desc">A public GitHub repository, or a single manifest.</p>
            </div>
          </div>

          <div className="source-switch segmented" role="tablist" aria-label="Source">
            <button
              type="button"
              role="tab"
              aria-selected={sourceMode === 'repository'}
              className={sourceMode === 'repository' ? 'is-active' : ''}
              onClick={() => setSourceMode('repository')}
              data-testid="source-repository"
            >
              Repository
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={sourceMode === 'manifest'}
              className={sourceMode === 'manifest' ? 'is-active' : ''}
              onClick={() => setSourceMode('manifest')}
              data-testid="source-manifest"
            >
              Manifest
            </button>
          </div>

          {sourceMode === 'repository' ? (
            <form
              className="panel repo-form"
              onSubmit={(e) => {
                e.preventDefault();
                void handleAnalyzeRepository();
              }}
            >
              <label className="repo-field" htmlFor="repo-url-input">
                <span className="field-label">GitHub repository</span>
                <input
                  id="repo-url-input"
                  type="url"
                  inputMode="url"
                  autoComplete="off"
                  spellCheck={false}
                  className="repo-input mono"
                  placeholder="https://github.com/owner/name"
                  value={repoUrl}
                  onChange={(e) => setRepoUrl(e.target.value)}
                  disabled={isLoading}
                  data-testid="repo-url-input"
                />
              </label>
              <div className="panel-actions">
                <button
                  type="submit"
                  className="btn btn-primary"
                  disabled={isLoading}
                  aria-busy={isLoading}
                  data-testid="btn-analyze-repository"
                >
                  {isLoading ? (
                    <>
                      <span className="spinner" aria-hidden="true" /> Cloning and analyzing
                      {elapsed > 0 && <span className="mono">{elapsed}s</span>}
                    </>
                  ) : (
                    'Analyze repository'
                  )}
                </button>
                <span className="examples">
                  <span className="hint">Try</span>
                  {EXAMPLE_REPOSITORIES.map((ex) => (
                    <button
                      key={ex.url}
                      type="button"
                      className="link-button mono"
                      onClick={() => void handleAnalyzeRepository(ex.url)}
                      disabled={isLoading}
                    >
                      {ex.label}
                    </button>
                  ))}
                </span>
              </div>
              <p className="hint repo-hint">
                Public repositories up to 200 MB. A shallow copy is made on this machine; its code is never
                run outside the isolated sandbox.
              </p>
            </form>
          ) : (
            <div className="panel">
              <div className="panel-toolbar">
                <div className="segmented" role="tablist" aria-label="Input method">
                  <button
                    type="button"
                    role="tab"
                    aria-selected={activeTab === 'raw'}
                    className={activeTab === 'raw' ? 'is-active' : ''}
                    onClick={() => setActiveTab('raw')}
                    data-testid="tab-raw"
                  >
                    Paste
                  </button>
                  <button
                    type="button"
                    role="tab"
                    aria-selected={activeTab === 'upload'}
                    className={activeTab === 'upload' ? 'is-active' : ''}
                    onClick={() => setActiveTab('upload')}
                    data-testid="tab-upload"
                  >
                    Upload
                  </button>
                </div>

                <div className="toolbar-group">
                  <label htmlFor="manifest-type-select" className="field-label">
                    Format
                  </label>
                  <div className="select-wrap">
                    <select
                      id="manifest-type-select"
                      value={manifestType}
                      onChange={(e) => handleManifestTypeChange(e.target.value)}
                      className="select"
                      data-testid="manifest-type-select"
                    >
                      <option value="requirements.txt">requirements.txt</option>
                      <option value="package.json">package.json</option>
                    </select>
                  </div>
                  {activeTab === 'raw' && (
                    <button
                      type="button"
                      className="btn btn-quiet btn-sm"
                      onClick={handleLoadSample}
                      data-testid="btn-load-sample"
                    >
                      Load sample
                    </button>
                  )}
                </div>
              </div>

              {activeTab === 'raw' ? (
                <label className="editor">
                  <span className="visually-hidden">Manifest contents</span>
                  <textarea
                    className="editor-input mono"
                    rows={10}
                    value={rawContent}
                    onChange={(e) => setRawContent(e.target.value)}
                    placeholder="Paste requirements.txt or package.json contents"
                    spellCheck={false}
                    data-testid="raw-content-input"
                  />
                </label>
              ) : (
                <div
                  className={`dropzone ${isDragging ? 'is-dragging' : ''} ${selectedFile ? 'has-file' : ''}`}
                  onDragOver={(e) => {
                    e.preventDefault();
                    setIsDragging(true);
                  }}
                  onDragLeave={() => setIsDragging(false)}
                  onDrop={handleFileDrop}
                  data-testid="dropzone"
                >
                  {selectedFile ? (
                    <p className="dropzone-file">
                      <span className="mono">{selectedFile.name}</span>
                      <span className="dim">{(selectedFile.size / 1024).toFixed(1)} KB</span>
                    </p>
                  ) : (
                    <p className="dropzone-text">
                      Drop <span className="mono">requirements.txt</span> or{' '}
                      <span className="mono">package.json</span> here
                    </p>
                  )}
                  <label className="btn btn-ghost btn-sm">
                    {selectedFile ? 'Choose another file' : 'Choose file'}
                    <input
                      type="file"
                      onChange={handleFileChange}
                      accept=".txt,.json"
                      className="visually-hidden"
                      data-testid="file-input"
                    />
                  </label>
                </div>
              )}

              <div className="panel-actions">
                <button
                  type="button"
                  className="btn btn-primary"
                  onClick={handleAnalyze}
                  disabled={isLoading}
                  aria-busy={isLoading}
                  data-testid="btn-analyze"
                >
                  {isLoading ? (
                    <>
                      <span className="spinner" aria-hidden="true" /> Checking registries
                    </>
                  ) : (
                    'Analyze manifest'
                  )}
                </button>
                <span className="hint">Package names are looked up on PyPI or npm. Nothing else leaves your machine.</span>
              </div>
            </div>
          )}

          {errorMessage && (
            <div className="notice notice-bad" role="alert" data-testid="error-banner">
              <p className="notice-title">
                {sourceMode === 'repository' ? 'The repository could not be analyzed' : 'The manifest could not be analyzed'}
              </p>
              <p>{errorMessage}</p>
              {errorDiagnostics.length > 0 && (
                <ul className="notice-list mono">
                  {errorDiagnostics.map((d, idx) => (
                    <li key={idx}>{d}</li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </section>

        {/* Stage 2: Manifests */}
        <section id="stage-manifests" className={`stage ${run ? '' : 'is-locked'}`} aria-labelledby="stage-manifests-title">
          <div className="stage-head">
            <span className="stage-index mono">02</span>
            <div>
              <h2 id="stage-manifests-title" className="stage-title" tabIndex={-1} data-stage-heading>
                Manifests
              </h2>
              <p className="stage-desc">
                {run
                  ? run.manifests.length === 0
                    ? 'No supported dependency files were found.'
                    : `${run.manifests.length} dependency file${run.manifests.length === 1 ? '' : 's'} found. Choose one to review.`
                  : 'Appears after the analysis.'}
              </p>
            </div>
          </div>

          {run && (
            <div className="stage-body" data-testid="manifests-stage">
              {run.repository && (
                <dl className="repo-summary" data-testid="repo-summary">
                  <div>
                    <dt>Repository</dt>
                    <dd>
                      <a href={run.repository.url} target="_blank" rel="noreferrer noopener" className="ext-link">
                        {run.repository.full_name}
                      </a>
                    </dd>
                  </div>
                  <div>
                    <dt>Commit</dt>
                    <dd className="mono">{run.repository.commit_sha ? run.repository.commit_sha.slice(0, 10) : '—'}</dd>
                  </div>
                  <div>
                    <dt>Branch</dt>
                    <dd className="mono">{run.repository.default_branch || '—'}</dd>
                  </div>
                  <div>
                    <dt>Scanned</dt>
                    <dd>
                      {run.repository.files_scanned.toLocaleString()} files, {formatBytes(run.repository.bytes_on_disk)}
                    </dd>
                  </div>
                </dl>
              )}

              {run.manifests.length === 0 ? (
                <div className="notice" data-testid="manifests-empty">
                  <p className="notice-title">Nothing to analyze here</p>
                  <p>
                    Supported files are <span className="mono">requirements*.txt</span>,{' '}
                    <span className="mono">pyproject.toml</span> and <span className="mono">package.json</span>.
                    Vendored folders such as <span className="mono">node_modules</span> and virtual environments are skipped.
                  </p>
                </div>
              ) : (
                <div className="table-scroll">
                  <table className="data-table" data-testid="manifests-table">
                    <thead>
                      <tr>
                        <th scope="col">File</th>
                        <th scope="col" className="opt">Type</th>
                        <th scope="col">Packages</th>
                        <th scope="col">Upgrades</th>
                        <th scope="col" className="opt">Status</th>
                        <th scope="col"><span className="visually-hidden">Open</span></th>
                      </tr>
                    </thead>
                    <tbody>
                      {run.manifests.map((m) => {
                        const selected = m.index === activeIndex;
                        return (
                          <tr key={m.index} className={selected ? 'is-open' : ''} data-testid={`manifest-row-${m.index}`}>
                            <td className="mono strong">{m.path}</td>
                            <td className="dim opt">{KIND_LABEL[m.kind]}</td>
                            <td className="mono">{m.analysis ? m.analysis.total_dependencies : '—'}</td>
                            <td className="mono">
                              {m.analysis ? (
                                <span className={m.analysis.upgrade_available_count ? 'candidate' : 'dim'}>
                                  {m.analysis.upgrade_available_count}
                                </span>
                              ) : (
                                '—'
                              )}
                            </td>
                            <td className="opt">
                              {m.error ? (
                                <span className="state tone-bad" title={m.error}>Not analyzed</span>
                              ) : m.plannable ? (
                                <span className="state state-up_to_date">Ready</span>
                              ) : (
                                <span className="state tone-muted" title={m.note || undefined}>Review only</span>
                              )}
                            </td>
                            <td className="cell-action">
                              {m.analysis ? (
                                <button
                                  type="button"
                                  className={selected ? 'link-button is-current' : 'link-button'}
                                  onClick={() => openManifest(run, m.index)}
                                  aria-pressed={selected}
                                  aria-label={`Review ${m.path}`}
                                  data-testid={`open-manifest-${m.index}`}
                                >
                                  {selected ? 'Reviewing' : 'Review'}
                                </button>
                              ) : (
                                <span className="dim">{m.error ? 'Unreadable' : '—'}</span>
                              )}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              )}

              {activeManifest && !activeManifest.plannable && activeManifest.note && (
                <p className="hint">{activeManifest.note}</p>
              )}

              {run.skipped.length > 0 && (
                <details className="skipped">
                  <summary>{run.skipped.length} file{run.skipped.length === 1 ? '' : 's'} skipped</summary>
                  <ul className="notice-list mono">
                    {run.skipped.map((s) => (
                      <li key={s}>{s}</li>
                    ))}
                  </ul>
                </details>
              )}
            </div>
          )}
        </section>

        {/* Stage 2: Dependency health */}
        <section id="stage-health" className={`stage ${analysisResult ? '' : 'is-locked'}`} aria-labelledby="stage-health-title">
          <div className="stage-head">
            <span className="stage-index mono">03</span>
            <div>
              <h2 id="stage-health-title" className="stage-title" tabIndex={-1} data-stage-heading>
                Dependency health
              </h2>
              <p className="stage-desc">
                {analysisResult
                  ? `${analysisResult.total_dependencies} entries in ${activeManifest?.path ?? analysisResult.manifest_type}, checked against ${analysisResult.ecosystem === 'npm' ? 'npm' : 'PyPI'}.`
                  : run
                    ? 'Choose a manifest above to see its dependencies.'
                    : 'Appears after the analysis.'}
              </p>
            </div>
          </div>

          {analysisResult && (
            <div className="stage-body" data-testid="analysis-results">
              <dl className="tally">
                <div>
                  <dt>Total dependencies</dt>
                  <dd>{analysisResult.total_dependencies}</dd>
                </div>
                <div className="tone-warn">
                  <dt>Upgrade available</dt>
                  <dd>{analysisResult.upgrade_available_count}</dd>
                </div>
                <div className="tone-ok">
                  <dt>Up to date</dt>
                  <dd>{analysisResult.up_to_date_count}</dd>
                </div>
                <div className="tone-muted">
                  <dt>Preserved</dt>
                  <dd>{analysisResult.preserved_count}</dd>
                </div>
              </dl>

              {analysisResult.dependencies.some((d) => usedOfflineCatalog(d.deprecation_reason)) && (
                <div className="notice" data-testid="offline-catalog-notice">
                  <p className="notice-title">Some versions are from offline data</p>
                  <p>
                    The registry could not be reached for packages marked “offline data”. Their versions come from a
                    bundled catalog and may be out of date. Analyze again when the registry is reachable.
                  </p>
                </div>
              )}

              <div className="table-toolbar">
                <div className="segmented segmented-sm" role="group" aria-label="Filter by status">
                  {([
                    ['ALL', `All ${analysisResult.dependencies.length}`, 'filter-all'],
                    ['UPGRADE_AVAILABLE', `Upgrades ${analysisResult.upgrade_available_count}`, 'filter-upgrade-available'],
                    ['UP_TO_DATE', `Current ${analysisResult.up_to_date_count}`, 'filter-up-to-date'],
                    ['PRESERVED', `Preserved ${analysisResult.preserved_count}`, 'filter-preserved'],
                  ] as const).map(([value, label, testId]) => (
                    <button
                      key={value}
                      type="button"
                      aria-pressed={statusFilter === value}
                      className={statusFilter === value ? 'is-active' : ''}
                      onClick={() => setStatusFilter(value)}
                      data-testid={testId}
                    >
                      {label}
                    </button>
                  ))}
                </div>
                <label className="search">
                  <span className="visually-hidden">Filter by package name</span>
                  <input
                    type="search"
                    className="input"
                    placeholder="Filter packages"
                    value={searchQuery}
                    onChange={(e) => setSearchQuery(e.target.value)}
                    data-testid="search-filter-input"
                  />
                </label>
              </div>

              <div className="table-scroll">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th scope="col">Package</th>
                      <th scope="col" className="opt">Declared</th>
                      <th scope="col" className="opt">Resolved</th>
                      <th scope="col" className="opt">Latest compatible</th>
                      <th scope="col">Latest</th>
                      <th scope="col">Status</th>
                      <th scope="col"><span className="visually-hidden">Details</span></th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredDependencies.length === 0 ? (
                      <tr>
                        <td colSpan={7} className="empty-cell">
                          No packages match this filter.
                        </td>
                      </tr>
                    ) : (
                      filteredDependencies.map((dep, idx) => {
                        const rowKey = `${dep.ecosystem}-${dep.name}-${idx}`;
                        const isExpanded = !!expandedRows[rowKey];
                        return (
                          <React.Fragment key={rowKey}>
                            <tr className={isExpanded ? 'is-open' : ''}>
                              <td>
                                <span className="pkg mono">{dep.original_name}</span>
                                {dep.dependency_type === 'development' && <span className="tag">dev</span>}
                                {usedOfflineCatalog(dep.deprecation_reason) && (
                                  <span
                                    className="tag tag-warn"
                                    title="The registry could not be reached; versions come from a bundled catalog and may be out of date."
                                  >
                                    offline data
                                  </span>
                                )}
                                {REGISTRY_PROBLEM[dep.registry_status] && (
                                  <span className="tag tag-bad">{REGISTRY_PROBLEM[dep.registry_status]}</span>
                                )}
                                {dep.yanked_or_deprecated && (
                                  <span className="tag tag-bad" title={dep.deprecation_reason || 'Deprecated or yanked'}>
                                    deprecated
                                  </span>
                                )}
                              </td>
                              <td className="mono dim opt">{dep.declared_specifier || '—'}</td>
                              <td className="mono opt">{dep.current_version || '—'}</td>
                              <td className="mono opt">{dep.latest_compatible_version || '—'}</td>
                              <td className="mono strong">{dep.latest_version || '—'}</td>
                              <td>
                                <span className={`state state-${dep.status.toLowerCase()}`}>{STATUS_LABEL[dep.status]}</span>
                              </td>
                              <td className="cell-action">
                                <button
                                  type="button"
                                  className="link-button"
                                  onClick={() => toggle(setExpandedRows, rowKey)}
                                  aria-expanded={isExpanded}
                                  aria-label={`${isExpanded ? 'Hide' : 'Show'} details for ${dep.original_name}`}
                                >
                                  {isExpanded ? 'Hide' : 'Details'}
                                </button>
                              </td>
                            </tr>
                            {isExpanded && (
                              <tr className="detail-row">
                                <td colSpan={7}>
                                  <dl className="detail-list">
                                    <div>
                                      <dt>Evaluation</dt>
                                      <dd>{dep.status_reason}</dd>
                                    </div>
                                    {dep.line_number && (
                                      <div>
                                        <dt>Source</dt>
                                        <dd>
                                          Line {dep.line_number}
                                          {dep.raw_line && <code className="mono">{dep.raw_line}</code>}
                                        </dd>
                                      </div>
                                    )}
                                    {dep.diagnostic_message && (
                                      <div>
                                        <dt>Note</dt>
                                        <dd>{dep.diagnostic_message}</dd>
                                      </div>
                                    )}
                                    {dep.available_versions.length > 0 && (
                                      <div>
                                        <dt>Recent releases</dt>
                                        <dd className="mono">{dep.available_versions.join(', ')}</dd>
                                      </div>
                                    )}
                                  </dl>
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

              {analysisResult.diagnostics.length > 0 && (
                <div className="notice" data-testid="diagnostics-card">
                  <p className="notice-title">Notes from the parser</p>
                  <ul className="notice-list">
                    {analysisResult.diagnostics.map((diag, i) => (
                      <li key={i}>{diag}</li>
                    ))}
                  </ul>
                </div>
              )}

              <div className="next-action" data-testid="recommendations-trigger-card">
                <div className="next-action-copy">
                  <h3 className="next-action-title">Get upgrade recommendations</h3>
                  <p className="hint">
                    {aiStatus?.is_available
                      ? 'A local model explains each upgrade. Every version it proposes is checked against the registry.'
                      : 'No local model is running, so recommendations use semantic-versioning rules on registry data.'}
                  </p>
                </div>
                <div className="next-action-controls">
                  <span className="engine" data-testid="ai-provider-status" title={aiStatus?.message}>
                    <span className={`signal ${aiStatus?.is_available ? 'signal-ok' : 'signal-idle'}`} aria-hidden="true" />
                    {engineLabel}
                  </span>
                  {aiStatus?.is_available && aiStatus.discovered_models.length > 0 && (
                    <div className="toolbar-group">
                      <label htmlFor="ai-model-select" className="field-label">
                        Model
                      </label>
                      <div className="select-wrap">
                        <select
                          id="ai-model-select"
                          value={selectedModel}
                          onChange={(e) => setSelectedModel(e.target.value)}
                          className="select"
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
                    </div>
                  )}
                  {aiStatus?.is_available && (
                    <label className="check">
                      <input
                        type="checkbox"
                        checked={forceDeterministic}
                        onChange={(e) => setForceDeterministic(e.target.checked)}
                        data-testid="force-deterministic-checkbox"
                      />
                      <span>Rules only</span>
                    </label>
                  )}
                  <button
                    type="button"
                    className="btn btn-primary"
                    onClick={handleGenerateRecommendations}
                    disabled={isGeneratingRecs}
                    aria-busy={isGeneratingRecs}
                    data-testid="btn-generate-recommendations"
                  >
                    {isGeneratingRecs ? (
                      <>
                        <span className="spinner" aria-hidden="true" /> Evaluating
                      </>
                    ) : recsResult ? (
                      'Regenerate'
                    ) : (
                      'Recommend upgrades'
                    )}
                  </button>
                </div>
              </div>

              {recsError && (
                <div className="notice notice-bad" role="alert" data-testid="recs-error-alert">
                  <p className="notice-title">Recommendations failed</p>
                  <p>{recsError}</p>
                </div>
              )}
            </div>
          )}
        </section>

        {/* Stage 3: Recommendations */}
        <section
          id="stage-recommendations"
          className={`stage ${recsResult ? '' : 'is-locked'}`}
          aria-labelledby="stage-recs-title"
        >
          <div className="stage-head">
            <span className="stage-index mono">04</span>
            <div>
              <h2 id="stage-recs-title" className="stage-title" tabIndex={-1} data-stage-heading>
                Recommendations
              </h2>
              <p className="stage-desc">
                {recsResult
                  ? `${recsResult.recommendations.length} packages evaluated. ${recsResult.total_upgrades_recommended} upgrade${recsResult.total_upgrades_recommended === 1 ? '' : 's'} recommended${recsResult.total_caution ? `, ${recsResult.total_caution} to review carefully` : ''}.`
                  : 'Appears after you request recommendations.'}
              </p>
            </div>
          </div>

          {recsResult && (
            <div className="stage-body" data-testid="recommendations-results">
              <div className="meta-row">
                <span className="meta">
                  <strong>{recsResult.total_upgrades_recommended}</strong> recommended
                </span>
                {recsResult.total_caution > 0 && (
                  <span className="meta tone-warn">
                    <strong>{recsResult.total_caution}</strong> need careful review
                  </span>
                )}
                <span className="meta">
                  Engine{' '}
                  <strong>{recsResult.is_offline_fallback || recsResult.provider_used === 'deterministic' ? 'Semantic-versioning rules' : 'Local model'}</strong>
                  {!recsResult.is_offline_fallback && recsResult.provider_used !== 'deterministic' && recsResult.model_used && (
                    <span className="mono dim"> {recsResult.model_used}</span>
                  )}
                </span>
                {recsResult.is_offline_fallback && (
                  <span className="meta dim" data-testid="fallback-indicator">
                    Local model unavailable, rules used instead
                  </span>
                )}
              </div>

              <div className="table-scroll">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th scope="col">Package</th>
                      <th scope="col" className="opt">Current</th>
                      <th scope="col">Candidate</th>
                      <th scope="col" className="opt">Assessment</th>
                      <th scope="col" className="opt">Priority</th>
                      <th scope="col">Basis</th>
                      <th scope="col"><span className="visually-hidden">Details</span></th>
                    </tr>
                  </thead>
                  <tbody>
                    {recsResult.recommendations.map((rec, idx) => {
                      const recKey = `rec-${rec.ecosystem}-${rec.package_name}-${idx}`;
                      const isExpanded = !!expandedRecRows[recKey];
                      const prov = provenanceOf(rec);
                      return (
                        <React.Fragment key={recKey}>
                          <tr className={isExpanded ? 'is-open' : ''}>
                            <td className="mono strong">{rec.package_name}</td>
                            <td className="mono dim opt">{rec.current_version || '—'}</td>
                            <td className="mono">
                              {rec.candidate_version ? <span className="strong">{rec.candidate_version}</span> : '—'}
                            </td>
                            <td className="opt">
                              <span className={`state state-cat-${rec.category.toLowerCase()}`}>{CATEGORY_LABEL[rec.category]}</span>
                            </td>
                            <td className="dim opt">{titleCase(rec.priority)}</td>
                            <td>
                              <span className={`basis basis-${prov.tone}`} title={prov.hint}>
                                {prov.label}
                              </span>
                            </td>
                            <td className="cell-action">
                              <button
                                type="button"
                                className="link-button"
                                onClick={() => toggle(setExpandedRecRows, recKey)}
                                aria-expanded={isExpanded}
                                aria-label={`${isExpanded ? 'Hide' : 'Show'} reasoning for ${rec.package_name}`}
                              >
                                {isExpanded ? 'Hide' : 'Why'}
                              </button>
                            </td>
                          </tr>
                          {isExpanded && (
                            <tr className="detail-row">
                              <td colSpan={7}>
                                <dl className="detail-list">
                                  <div>
                                    <dt>Assessment</dt>
                                    <dd>
                                      {CATEGORY_LABEL[rec.category]}, {rec.priority.toLowerCase()} priority
                                      {rec.current_version ? ` (from ${rec.current_version})` : ''}
                                    </dd>
                                  </div>
                                  <div>
                                    <dt>{rec.is_ai_generated ? 'Model explanation' : 'Rule explanation'}</dt>
                                    <dd>{rec.explanation}</dd>
                                  </div>
                                  {rec.breaking_changes_summary && (
                                    <div>
                                      <dt>Risk</dt>
                                      <dd>{rec.breaking_changes_summary}</dd>
                                    </div>
                                  )}
                                  {rec.latest_registry_version && (
                                    <div>
                                      <dt>Registry latest</dt>
                                      <dd className="mono">{rec.latest_registry_version}</dd>
                                    </div>
                                  )}
                                  <div>
                                    <dt>Basis</dt>
                                    <dd>{prov.hint}</dd>
                                  </div>
                                </dl>
                              </td>
                            </tr>
                          )}
                        </React.Fragment>
                      );
                    })}
                  </tbody>
                </table>
              </div>

              <div className="next-action" data-testid="plan-trigger-section">
                <div className="next-action-copy">
                  <h3 className="next-action-title">Build the upgrade plan</h3>
                  <p className="hint">
                    {recsResult.total_upgrades_recommended === 0
                      ? 'Nothing to upgrade: every package is already at its recommended version.'
                      : activeManifest && !activeManifest.plannable
                      ? 'This file is shown for review only. Patches are written for requirements files and package.json.'
                      : 'Recommended upgrades are written into your manifest as a unified diff, then checked to apply and reverse cleanly.'}
                  </p>
                </div>
                <button
                  type="button"
                  className="btn btn-primary"
                  onClick={handleGeneratePlan}
                  disabled={isGeneratingPlan || recsResult.total_upgrades_recommended === 0 || !activeManifest?.plannable}
                  aria-busy={isGeneratingPlan}
                  data-testid="btn-generate-plan"
                >
                  {isGeneratingPlan ? (
                    <>
                      <span className="spinner" aria-hidden="true" /> Building plan
                    </>
                  ) : proposal ? (
                    'Rebuild plan'
                  ) : (
                    'Build plan and diff'
                  )}
                </button>
              </div>

              {planError && (
                <div className="notice notice-bad" role="alert" data-testid="plan-error-banner">
                  <p className="notice-title">The plan could not be built</p>
                  <p>{planError}</p>
                </div>
              )}
            </div>
          )}
        </section>

        {/* Stages 4 and 5: Plan, diff and approval */}
        <section id="stage-plan" className={`stage ${proposal ? '' : 'is-locked'}`} aria-labelledby="stage-plan-title">
          <div className="stage-head">
            <span className="stage-index mono">05</span>
            <div>
              <h2 id="stage-plan-title" className="stage-title" tabIndex={-1} data-stage-heading>
                Plan and diff
              </h2>
              <p className="stage-desc">
                {proposal
                  ? proposal.total_planned === 0
                    ? `No changes needed: every dependency in ${proposal.file_path} is already at its recommended version.`
                    : `${proposal.total_planned} change${proposal.total_planned === 1 ? '' : 's'} to ${proposal.file_path}. ${proposal.integrity.is_valid ? 'The patch applies and reverses cleanly.' : 'The patch failed its integrity checks.'}`
                  : 'Appears after the plan is built.'}
              </p>
            </div>
          </div>
          {proposal && (
            <PlanReviewer
              key={proposal.proposal_id}
              proposal={proposal}
              onApprove={handleApproveProposal}
              isApproved={isApproved}
              approvalNote={approvalNote}
              isApproving={isApproving}
              approvalError={approvalError}
              stageIndex="06"
            />
          )}
        </section>

        {/* Stage 6: Validation */}
        <section
          id="stage-validation"
          className={`stage ${isApproved ? '' : 'is-locked'}`}
          aria-labelledby="stage-validation-title"
        >
          <div className="stage-head">
            <span className="stage-index mono">07</span>
            <div>
              <h2 id="stage-validation-title" className="stage-title" tabIndex={-1} data-stage-heading>
                Validation
              </h2>
              <p className="stage-desc">
                {isApproved
                  ? 'Run checks against the approved change inside an isolated container.'
                  : 'Available after you approve the plan.'}
              </p>
            </div>
          </div>
          {proposal && (
            <SandboxValidation
              key={proposal.proposal_id}
              proposal={proposal}
              manifestType={analysisResult?.manifest_type || manifestType}
              isApproved={isApproved}
              onReport={setValidationReport}
            />
          )}
        </section>

        {/* Stage 7: Export */}
        <section id="stage-export" className={`stage ${isApproved ? '' : 'is-locked'}`} aria-labelledby="stage-export-title">
          <div className="stage-head">
            <span className="stage-index mono">08</span>
            <div>
              <h2 id="stage-export-title" className="stage-title" tabIndex={-1} data-stage-heading>
                Export
              </h2>
              <p className="stage-desc">
                {isApproved
                  ? 'Download the patch, a summary and checksums for every file.'
                  : 'Available after you approve the plan.'}
              </p>
            </div>
          </div>
          {proposal && (
            <ExportManager
              proposalId={proposal?.proposal_id}
              executionId={validationReport?.execution_id}
              validationStatus={validationReport?.status || (proposal ? 'NOT_PERFORMED' : null)}
              isApproved={isApproved}
              onExported={setExportData}
            />
          )}
        </section>
      </main>
    </div>
  );
};
