import React, { useState, useEffect } from 'react';
import type {
  SandboxStatusResponse,
  ValidationReportResponse,
  UpgradeProposalResponse,
} from '../api/client';
import {
  cancelValidationExecution,
  executeValidation,
  getSandboxStatus,
} from '../api/client';
import { ExportManager } from './ExportManager';

interface SandboxValidationProps {
  proposal?: UpgradeProposalResponse | null;
  manifestType?: string;
  originalContent?: string;
  isApproved?: boolean;
}

export const SandboxValidation: React.FC<SandboxValidationProps> = ({
  proposal,
  manifestType = 'requirements.txt',
  isApproved = false,
}) => {
  const [sandboxStatus, setSandboxStatus] = useState<SandboxStatusResponse | null>(null);
  const [isValidating, setIsValidating] = useState<boolean>(false);
  const [validationReport, setValidationReport] = useState<ValidationReportResponse | null>(null);
  const [validationError, setValidationError] = useState<string | null>(null);
  const [showLogs, setShowLogs] = useState<boolean>(true);
  const [copyFeedback, setCopyFeedback] = useState<boolean>(false);

  useEffect(() => {
    getSandboxStatus()
      .then((status) => setSandboxStatus(status))
      .catch(() =>
        setSandboxStatus({
          is_available: false,
          version: null,
          error_message: 'Unable to connect to backend sandbox probe.',
        })
      );
  }, []);

  const handleRunValidation = async () => {
    setIsValidating(true);
    setValidationError(null);

    try {
      const report = await executeValidation({
        proposal_id: proposal?.proposal_id || undefined,
        manifest_type: manifestType,
        timeout_seconds: 60,
        memory_limit_mb: 512,
        cpu_limit: '1.0',
        pids_limit: 64,
      });
      setValidationReport(report);
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Validation execution failed.';
      setValidationError(msg);
    } finally {
      setIsValidating(false);
    }
  };

  const handleCancelValidation = async () => {
    if (!validationReport?.execution_id) return;
    try {
      await cancelValidationExecution(validationReport.execution_id);
      setValidationReport((prev) =>
        prev
          ? {
              ...prev,
              status: 'CANCELLED',
              summary: 'Validation execution was cancelled by user.',
            }
          : null
      );
    } catch (err) {
      console.error('Failed to cancel validation:', err);
    }
  };

  const handleCopyLogs = () => {
    if (!validationReport?.logs) return;
    navigator.clipboard.writeText(validationReport.logs);
    setCopyFeedback(true);
    setTimeout(() => setCopyFeedback(false), 2000);
  };

  return (
    <div className="sandbox-validation-card" data-testid="sandbox-validation-section">
      <div className="sandbox-header">
        <div className="sandbox-title-group">
          <h3 className="section-title">Sandboxed Container Validation</h3>
          <p className="section-description">
            Executes verification checks, syntax compilation, and test discovery strictly inside
            an isolated Docker container with <code>--network none</code> and resource caps.
          </p>
        </div>

        {/* Engine Probe Status Badge */}
        <div className="sandbox-engine-badge" data-testid="sandbox-engine-status">
          {sandboxStatus ? (
            sandboxStatus.is_available ? (
              <span className="badge badge-success" title="Docker daemon is active and responsive">
                ✓ Docker Engine Active {sandboxStatus.version ? `(v${sandboxStatus.version})` : ''}
              </span>
            ) : (
              <span
                className="badge badge-warning"
                title={sandboxStatus.error_message || 'Docker is not installed or daemon is stopped'}
              >
                ⚠ Docker Offline (Fail-Closed NOT_RUNNABLE)
              </span>
            )
          ) : (
            <span className="badge badge-neutral">Probing Docker runtime...</span>
          )}

          {proposal && (
            <span
              className={`badge ${isApproved ? 'badge-success' : 'badge-warning'}`}
              data-testid="validation-approval-gate-badge"
              title={
                isApproved
                  ? 'Human approval recorded for this proposal'
                  : 'Explicit human approval pending in Plan Reviewer'
              }
            >
              {isApproved ? '✓ Proposal Human-Approved' : '⚠ Proposal Review Pending'}
            </span>
          )}
        </div>
      </div>

      {/* Security Guarantees Banner */}
      <div className="containment-spec-card">
        <span className="spec-item">
          <strong>Network:</strong> <code>--network none</code>
        </span>
        <span className="spec-item">
          <strong>User:</strong> <code>1000:1000 (non-root)</code>
        </span>
        <span className="spec-item">
          <strong>Capabilities:</strong> <code>--cap-drop=ALL</code>
        </span>
        <span className="spec-item">
          <strong>Memory:</strong> <code>512 MB</code>
        </span>
        <span className="spec-item">
          <strong>CPU:</strong> <code>1.0 core</code>
        </span>
        <span className="spec-item">
          <strong>PIDs:</strong> <code>64 max</code>
        </span>
      </div>

      {/* Trigger Button and Controls */}
      <div className="sandbox-actions-row">
        <button
          type="button"
          className="btn btn-primary"
          onClick={handleRunValidation}
          disabled={isValidating}
          data-testid="btn-run-validation"
        >
          {isValidating ? 'Validating in Sandbox...' : 'Run Sandboxed Validation'}
        </button>

        {isValidating && validationReport?.execution_id && (
          <button
            type="button"
            className="btn btn-secondary"
            onClick={handleCancelValidation}
            data-testid="btn-cancel-validation"
          >
            Cancel Execution
          </button>
        )}
      </div>

      {/* Validation Error Banner */}
      {validationError && (
        <div className="error-banner" data-testid="validation-error-banner">
          <strong>Validation Error:</strong> {validationError}
        </div>
      )}

      {/* Validation Results Display */}
      {validationReport && (
        <div className="validation-results-card" data-testid="validation-results">
          <div className="validation-status-header">
            <div className="validation-outcome">
              <span
                className={`status-pill status-${validationReport.status.toLowerCase()}`}
                data-testid="validation-status-badge"
              >
                {validationReport.status === 'PASSED' && '✓ PASSED'}
                {validationReport.status === 'FAILED' && '✗ FAILED'}
                {validationReport.status === 'NO_TESTS_FOUND' && 'ℹ NO TESTS FOUND'}
                {validationReport.status === 'NOT_RUNNABLE' && 'ℹ NOT RUNNABLE'}
                {validationReport.status === 'TIMED_OUT' && '⏱ TIMED OUT'}
                {validationReport.status === 'CANCELLED' && '⏹ CANCELLED'}
                {validationReport.status === 'INFRASTRUCTURE_ERROR' && '⚠ INFRASTRUCTURE ERROR'}
              </span>
              <span className="validation-duration">
                Duration: {validationReport.total_duration_seconds}s
              </span>
              {validationReport.image_used && (
                <span className="validation-image">
                  Image: <code>{validationReport.image_used}</code>
                  {validationReport.image_digest && (
                    <span className="image-digest-tag" title={validationReport.image_digest}>
                      {' '}({validationReport.image_digest.slice(0, 19)}…)
                    </span>
                  )}
                </span>
              )}
            </div>

            <p className="validation-summary" data-testid="validation-summary-text">
              {validationReport.summary}
            </p>

            <div className="validation-submetrics">
              {validationReport.test_discovery_status && (
                <span className="submetric-item">
                  <strong>Discovery:</strong> {validationReport.test_discovery_status}
                </span>
              )}
              {validationReport.snapshot_files_count !== undefined && (
                <span className="submetric-item">
                  <strong>Snapshot:</strong> {validationReport.snapshot_files_count} files included,{' '}
                  {validationReport.snapshot_excluded_count || 0} scrubbed
                </span>
              )}
            </div>
          </div>

          {/* Commands Breakdown Table */}
          {validationReport.commands.length > 0 && (
            <div className="commands-table-container">
              <h4 className="table-heading">Command Execution Checks</h4>
              <table className="table commands-table" data-testid="validation-commands-table">
                <thead>
                  <tr>
                    <th>Command</th>
                    <th>Status</th>
                    <th>Exit Code</th>
                    <th>Duration</th>
                    <th>Description</th>
                  </tr>
                </thead>
                <tbody>
                  {validationReport.commands.map((cmd, idx) => (
                    <tr key={idx}>
                      <td>
                        <code>{cmd.command_name}</code>
                      </td>
                      <td>
                        <span className={`badge badge-${cmd.status.toLowerCase()}`}>
                          {cmd.status}
                        </span>
                      </td>
                      <td>{cmd.exit_code}</td>
                      <td>{cmd.duration_seconds}s</td>
                      <td>{cmd.description}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {/* Logs Terminal Box */}
          {validationReport.logs && (
            <div className="logs-container" data-testid="validation-logs-section">
              <div className="logs-header">
                <span className="logs-title">Execution Console Output</span>
                <div className="logs-actions">
                  <button
                    type="button"
                    className="btn-icon"
                    onClick={handleCopyLogs}
                    title="Copy logs to clipboard"
                  >
                    {copyFeedback ? '✓ Copied' : 'Copy Logs'}
                  </button>
                  <button
                    type="button"
                    className="btn-icon"
                    onClick={() => setShowLogs(!showLogs)}
                  >
                    {showLogs ? 'Collapse' : 'Expand'}
                  </button>
                </div>
              </div>

              {showLogs && (
                <pre className="logs-terminal" data-testid="validation-logs-text">
                  {validationReport.logs}
                </pre>
              )}
            </div>
          )}
        </div>
      )}

      {/* Deliverables Export Subsystem */}
      <ExportManager
        proposalId={proposal?.proposal_id}
        executionId={validationReport?.execution_id}
        validationStatus={validationReport?.status || (proposal ? 'NOT_PERFORMED' : null)}
      />
    </div>
  );
};
