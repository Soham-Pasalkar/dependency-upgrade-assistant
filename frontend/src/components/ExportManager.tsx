import React, { useState } from 'react';
import type { ExportItemMetadata, ExportMetadataResponse } from '../api/client';
import { createExport, getArtifactDownloadUrl } from '../api/client';

interface ExportManagerProps {
  proposalId?: string | null;
  executionId?: string | null;
  validationStatus?: string | null;
  isApproved?: boolean;
  initialExport?: ExportMetadataResponse | null;
  onExported?: (data: ExportMetadataResponse) => void;
}

const ARTIFACT_LABEL: Record<string, string> = {
  patch: 'Patch',
  summary_json: 'Summary',
  execution_log: 'Validation log',
  checksums: 'Checksums',
  bundle_zip: 'Complete bundle',
};

const VALIDATION_TEXT: Record<string, string> = {
  PASSED: 'Validation passed',
  FAILED: 'Validation failed',
  NO_TESTS_FOUND: 'No tests found',
  TIMED_OUT: 'Validation timed out',
  CANCELLED: 'Validation cancelled',
  INFRASTRUCTURE_ERROR: 'Sandbox error',
  NOT_RUNNABLE: 'Validation not runnable',
  NOT_PERFORMED: 'Not validated',
};

const formatFileSize = (bytes: number): string =>
  bytes < 1024 ? `${bytes} B` : bytes < 1024 * 1024 ? `${(bytes / 1024).toFixed(1)} KB` : `${(bytes / 1048576).toFixed(1)} MB`;

export const ExportManager: React.FC<ExportManagerProps> = ({
  proposalId,
  executionId,
  validationStatus,
  isApproved = false,
  initialExport = null,
  onExported,
}) => {
  const [isExporting, setIsExporting] = useState<boolean>(false);
  const [exportData, setExportData] = useState<ExportMetadataResponse | null>(initialExport);
  const [exportError, setExportError] = useState<string | null>(null);
  const [copiedHash, setCopiedHash] = useState<string | null>(null);

  const handleGenerateExport = async () => {
    if (!proposalId) {
      setExportError('Build and approve an upgrade plan before exporting.');
      return;
    }
    if (!isApproved) {
      setExportError('Approve the upgrade plan before exporting.');
      return;
    }
    setIsExporting(true);
    setExportError(null);
    try {
      const data = await createExport({
        proposal_id: proposalId,
        execution_id: executionId || undefined,
        include_logs: true,
      });
      setExportData(data);
      onExported?.(data);
    } catch (err) {
      setExportError(err instanceof Error ? err.message : 'Export failed.');
    } finally {
      setIsExporting(false);
    }
  };

  const handleCopyHash = async (sha256: string, filename: string) => {
    try {
      await navigator.clipboard.writeText(sha256);
      setCopiedHash(filename);
      setTimeout(() => setCopiedHash(null), 2500);
    } catch {
      setCopiedHash(null);
    }
  };

  const bundle = exportData?.items.find((i) => i.artifact_type === 'bundle_zip');

  return (
    <div className="stage-body" data-testid="export-deliverables-section">
      <div className="sandbox-strip">
        <span className="engine" data-testid="zero-secret-badge">
          <span className="signal signal-ok" aria-hidden="true" />
          Files are scanned for secrets before they are written
        </span>
        {validationStatus && (
          <span className="state" data-testid="export-val-status">
            {VALIDATION_TEXT[validationStatus] || validationStatus}
          </span>
        )}
      </div>

      <div className="panel-actions">
        <button
          type="button"
          className={`btn ${exportData ? 'btn-ghost' : 'btn-primary'}`}
          onClick={handleGenerateExport}
          disabled={isExporting || !proposalId || !isApproved}
          aria-busy={isExporting}
          data-testid="generate-export-button"
        >
          {isExporting ? (
            <>
              <span className="spinner" aria-hidden="true" /> Preparing files
            </>
          ) : exportData ? (
            'Prepare again'
          ) : (
            'Prepare export'
          )}
        </button>
        {!proposalId ? (
          <span className="hint">Build an upgrade plan first.</span>
        ) : !isApproved ? (
          <span className="hint">Approve the plan to enable export.</span>
        ) : validationStatus === 'NOT_PERFORMED' ? (
          <span className="hint">You can export without validating. The report will say it was not validated.</span>
        ) : null}
      </div>

      {exportError && (
        <div className="notice notice-bad" role="alert" data-testid="export-error-banner">
          <p className="notice-title">Export failed</p>
          <p>{exportError}</p>
        </div>
      )}

      {exportData && (
        <div className="result" data-testid="export-results-card">
          {bundle && (
            <a
              className="download-hero"
              href={getArtifactDownloadUrl(exportData.export_id, bundle.artifact_type)}
              download={bundle.filename}
            >
              <span>
                <span className="download-hero-title">Download everything</span>
                <span className="mono dim">{bundle.filename}</span>
              </span>
              <span className="mono dim">{formatFileSize(bundle.size_bytes)}</span>
            </a>
          )}
          <div className="table-scroll">
            <table className="data-table" aria-label="Exported files">
              <thead>
                <tr>
                  <th scope="col">File</th>
                  <th scope="col" className="opt">Name</th>
                  <th scope="col">Size</th>
                  <th scope="col" className="opt">SHA-256</th>
                  <th scope="col"><span className="visually-hidden">Download</span></th>
                </tr>
              </thead>
              <tbody>
                {exportData.items.map((item: ExportItemMetadata) => (
                  <tr key={item.artifact_type} data-testid={`artifact-row-${item.artifact_type}`}>
                    <td className="strong">{ARTIFACT_LABEL[item.artifact_type] || item.artifact_type}</td>
                    <td className="mono opt">{item.filename}</td>
                    <td className="mono dim">{formatFileSize(item.size_bytes)}</td>
                    <td className="opt">
                      <span className="hash">
                        <code className="mono dim" title={item.sha256}>
                          {item.sha256.substring(0, 12)}…{item.sha256.substring(56)}
                        </code>
                        <button
                          type="button"
                          className="link-button"
                          onClick={() => handleCopyHash(item.sha256, item.filename)}
                          aria-label={`Copy SHA-256 for ${item.filename}`}
                        >
                          {copiedHash === item.filename ? 'Copied' : 'Copy'}
                        </button>
                      </span>
                    </td>
                    <td className="cell-action">
                      <a
                        href={getArtifactDownloadUrl(exportData.export_id, item.artifact_type)}
                        download={item.filename}
                        className="link-button"
                        data-testid={`download-button-${item.artifact_type}`}
                      >
                        Download
                      </a>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
};
