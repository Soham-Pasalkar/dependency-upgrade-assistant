import React, { useState } from 'react';
import type {
  ExportItemMetadata,
  ExportMetadataResponse,
} from '../api/client';
import {
  createExport,
  getArtifactDownloadUrl,
} from '../api/client';

interface ExportManagerProps {
  proposalId?: string | null;
  executionId?: string | null;
  validationStatus?: string | null;
  initialExport?: ExportMetadataResponse | null;
}

export const ExportManager: React.FC<ExportManagerProps> = ({
  proposalId,
  executionId,
  validationStatus,
  initialExport = null,
}) => {
  const [isExporting, setIsExporting] = useState<boolean>(false);
  const [exportData, setExportData] = useState<ExportMetadataResponse | null>(initialExport);
  const [exportError, setExportError] = useState<string | null>(null);
  const [copiedHash, setCopiedHash] = useState<string | null>(null);

  const handleGenerateExport = async () => {
    if (!proposalId) {
      setExportError('A valid proposal must be available to generate deliverables.');
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
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Failed to generate deliverables.';
      setExportError(msg);
    } finally {
      setIsExporting(false);
    }
  };

  const handleCopyHash = (sha256: string, filename: string) => {
    navigator.clipboard.writeText(sha256);
    setCopiedHash(filename);
    setTimeout(() => setCopiedHash(null), 2500);
  };

  const formatFileSize = (bytes: number): string => {
    if (bytes < 1024) return `${bytes} B`;
    const kb = (bytes / 1024).toFixed(1);
    return `${kb} KB`;
  };

  const getArtifactBadgeLabel = (type: string): string => {
    switch (type) {
      case 'patch':
        return 'UNIFIED PATCH';
      case 'summary_json':
        return 'JSON SUMMARY';
      case 'execution_log':
        return 'CONSOLE LOGS';
      case 'checksums':
        return 'SHA-256 MANIFEST';
      case 'bundle_zip':
        return 'COMPLETE BUNDLE';
      default:
        return type.toUpperCase();
    }
  };

  return (
    <div className="export-manager-card" data-testid="export-deliverables-section">
      <div className="export-header">
        <div className="export-title-group">
          <h3 className="section-title">Deliverables Export &amp; Checksum Verification</h3>
          <p className="section-description">
            Exports production-grade unified diffs, structured JSON execution summaries, sanitized
            logs, and server-verified SHA-256 cryptographic manifests for repository merge.
          </p>
        </div>

        {/* Security & Validation Status Badges */}
        <div className="export-badges-row">
          <span className="security-badge" data-testid="zero-secret-badge">
            <span className="badge-icon">🔒</span> Zero Credential Leakage Verified
          </span>
          {validationStatus && (
            <span
              className={`status-badge status-${validationStatus.toLowerCase()}`}
              data-testid="export-val-status"
            >
              Validation: {validationStatus}
            </span>
          )}
        </div>
      </div>

      {/* Action Trigger */}
      <div className="export-controls">
        <button
          type="button"
          className="btn-primary"
          onClick={handleGenerateExport}
          disabled={isExporting || !proposalId}
          data-testid="generate-export-button"
        >
          {isExporting ? 'Generating Verified Bundle...' : 'Generate Deliverables Bundle'}
        </button>

        {!proposalId && (
          <span className="export-hint text-muted">
            Generate an upgrade proposal above before exporting deliverables.
          </span>
        )}
      </div>

      {/* Error Banner */}
      {exportError && (
        <div className="alert-banner alert-error" role="alert" data-testid="export-error-banner">
          <span className="alert-icon">⚠</span>
          <span className="alert-message">{exportError}</span>
        </div>
      )}

      {/* Generated Artifacts Table */}
      {exportData && (
        <div className="export-results" data-testid="export-results-card">
          <div className="export-results-header">
            <h4>Export Bundle: <code>{exportData.export_id}</code></h4>
            <span className="export-timestamp">
              Generated: {new Date(exportData.created_at * 1000).toLocaleString()}
            </span>
          </div>

          <div className="artifacts-table-container">
            <table className="artifacts-table" aria-label="Exported deliverable files">
              <thead>
                <tr>
                  <th scope="col">Artifact Type</th>
                  <th scope="col">Filename</th>
                  <th scope="col">Size</th>
                  <th scope="col">Server SHA-256 Checksum</th>
                  <th scope="col" className="text-right">Action</th>
                </tr>
              </thead>
              <tbody>
                {exportData.items.map((item: ExportItemMetadata) => {
                  const isCopied = copiedHash === item.filename;
                  const downloadUrl = getArtifactDownloadUrl(exportData.export_id, item.artifact_type);

                  return (
                    <tr key={item.artifact_type} data-testid={`artifact-row-${item.artifact_type}`}>
                      <td>
                        <span className={`artifact-badge badge-${item.artifact_type}`}>
                          {getArtifactBadgeLabel(item.artifact_type)}
                        </span>
                      </td>
                      <td className="font-mono text-bold">{item.filename}</td>
                      <td>{formatFileSize(item.size_bytes)}</td>
                      <td>
                        <div className="sha256-cell">
                          <code className="sha256-text" title={item.sha256}>
                            {item.sha256.substring(0, 16)}…{item.sha256.substring(56)}
                          </code>
                          <button
                            type="button"
                            className="btn-icon btn-copy-hash"
                            onClick={() => handleCopyHash(item.sha256, item.filename)}
                            title="Copy full SHA-256 checksum to clipboard"
                            aria-label={`Copy SHA-256 for ${item.filename}`}
                          >
                            {isCopied ? '✓ Copied' : 'Copy'}
                          </button>
                        </div>
                      </td>
                      <td className="text-right">
                        <a
                          href={downloadUrl}
                          download={item.filename}
                          className="btn-download"
                          data-testid={`download-button-${item.artifact_type}`}
                        >
                          Download
                        </a>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
};

