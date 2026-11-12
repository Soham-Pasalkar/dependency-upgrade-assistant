import React, { useEffect, useState } from 'react';
import type {
  SandboxStatusResponse,
  SandboxValidationStatus,
  ValidationReportResponse,
  UpgradeProposalResponse,
} from '../api/client';
import { cancelValidationExecution, executeValidation, getSandboxStatus } from '../api/client';

interface SandboxValidationProps {
  proposal?: UpgradeProposalResponse | null;
  manifestType?: string;
  isApproved?: boolean;
  onReport?: (report: ValidationReportResponse | null) => void;
}

const COMMAND_LABEL: Record<string, string> = {
  dependency_install: 'install',
};

const STATUS_TEXT: Record<SandboxValidationStatus, { label: string; tone: string }> = {
  PASSED: { label: 'Passed', tone: 'ok' },
  FAILED: { label: 'Failed', tone: 'bad' },
  NO_TESTS_FOUND: { label: 'No tests found', tone: 'muted' },
  TIMED_OUT: { label: 'Timed out', tone: 'bad' },
  CANCELLED: { label: 'Cancelled', tone: 'muted' },
  INFRASTRUCTURE_ERROR: { label: 'Sandbox error', tone: 'bad' },
  NOT_RUNNABLE: { label: 'Not runnable', tone: 'warn' },
};

const isTestCommand = (name: string) => /pytest|unittest|npm test|jest|vitest|mocha|test/i.test(name);

export const SandboxValidation: React.FC<SandboxValidationProps> = ({
  proposal,
  manifestType = 'requirements.txt',
  isApproved = false,
  onReport,
}) => {
  const [sandboxStatus, setSandboxStatus] = useState<SandboxStatusResponse | null>(null);
  const [isValidating, setIsValidating] = useState<boolean>(false);
  const [validationReport, setValidationReport] = useState<ValidationReportResponse | null>(null);
  const [validationError, setValidationError] = useState<string | null>(null);
  const [showLogs, setShowLogs] = useState<boolean>(false);
  const [copyFeedback, setCopyFeedback] = useState<boolean>(false);

  useEffect(() => {
    getSandboxStatus()
      .then(setSandboxStatus)
      .catch(() => setSandboxStatus({ is_available: false, version: null, error_message: 'Sandbox probe failed.' }));
  }, []);

  const handleRunValidation = async () => {
    if (!isApproved) return;
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
      onReport?.(report);
    } catch (err) {
      setValidationError(err instanceof Error ? err.message : 'Validation could not be started.');
    } finally {
      setIsValidating(false);
    }
  };

  const handleCancelValidation = async () => {
    if (!validationReport?.execution_id) return;
    try {
      await cancelValidationExecution(validationReport.execution_id);
      const cancelled = { ...validationReport, status: 'CANCELLED' as const, summary: 'Validation was cancelled.' };
      setValidationReport(cancelled);
      onReport?.(cancelled);
    } catch (err) {
      setValidationError(err instanceof Error ? err.message : 'Could not cancel validation.');
    }
  };

  const handleCopyLogs = async () => {
    if (!validationReport?.logs) return;
    try {
      await navigator.clipboard.writeText(validationReport.logs);
      setCopyFeedback(true);
      setTimeout(() => setCopyFeedback(false), 2000);
    } catch {
      setCopyFeedback(false);
    }
  };

  const status = validationReport ? STATUS_TEXT[validationReport.status] : null;
  const ranTests =
    validationReport?.commands.some((c) => c.command_name !== 'dependency_install' && isTestCommand(c.command_name)) ?? false;
  const install = validationReport?.commands.find((c) => c.command_name === 'dependency_install');
  const stagingNotes = (validationReport?.diagnostics ?? [])
    .filter((d) => d.startsWith('Dependency staging: '))
    .map((d) => d.replace('Dependency staging: ', ''));

  return (
    <div className="stage-body" data-testid="sandbox-validation-section">
      <div className="sandbox-strip">
        <span className="engine" data-testid="sandbox-engine-status">
          {sandboxStatus ? (
            sandboxStatus.is_available ? (
              <>
                <span className="signal signal-ok" aria-hidden="true" />
                Docker available{sandboxStatus.version ? ` (${sandboxStatus.version})` : ''}
              </>
            ) : (
              <span title={sandboxStatus.error_message || undefined}>
                <span className="signal signal-warn" aria-hidden="true" />
                Docker not available. Runs will report Not runnable.
              </span>
            )
          ) : (
            <>
              <span className="signal signal-idle" aria-hidden="true" />
              Checking for Docker
            </>
          )}
        </span>
        {proposal && (
          <span
            className={`state ${isApproved ? 'state-up_to_date' : 'state-upgrade_available'}`}
            data-testid="validation-approval-gate-badge"
          >
            {isApproved ? 'Plan approved' : 'Approval required'}
          </span>
        )}
      </div>

      <dl className="spec spec-inline" aria-label="Container restrictions">
        <div><dt>Network</dt><dd className="mono">none</dd></div>
        <div><dt>User</dt><dd className="mono">1000:1000 (non-root)</dd></div>
        <div><dt>Capabilities</dt><dd className="mono">--cap-drop=ALL</dd></div>
        <div><dt>Limits</dt><dd className="mono">512 MB, 1 CPU, 64 PIDs</dd></div>
      </dl>

      <div className="panel-actions">
        <button
          type="button"
          className={`btn ${validationReport ? 'btn-ghost' : 'btn-primary'}`}
          onClick={handleRunValidation}
          disabled={isValidating || !isApproved}
          aria-busy={isValidating}
          data-testid="btn-run-validation"
        >
          {isValidating ? (
            <>
              <span className="spinner" aria-hidden="true" /> Running in sandbox
            </>
          ) : validationReport ? (
            'Run again'
          ) : (
            'Run validation'
          )}
        </button>
        {isValidating && validationReport?.execution_id && (
          <button type="button" className="btn btn-ghost" onClick={handleCancelValidation} data-testid="btn-cancel-validation">
            Cancel
          </button>
        )}
        {!isApproved && <span className="hint">Approve the plan to enable validation.</span>}
      </div>

      {validationError && (
        <div className="notice notice-bad" role="alert" data-testid="validation-error-banner">
          <p className="notice-title">Validation could not run</p>
          <p>{validationError}</p>
        </div>
      )}

      {validationReport && status && (
        <div className="result" data-testid="validation-results">
          <div className="result-head">
            <span className={`result-status tone-${status.tone}`} data-testid="validation-status-badge">
              {status.label}
            </span>
            {validationReport.is_runnable && (
              <span className="meta dim mono">{validationReport.total_duration_seconds}s</span>
            )}
            {validationReport.is_runnable && validationReport.image_used && (
              <span className="meta dim mono" title={validationReport.image_digest || undefined}>
                {validationReport.image_used}
              </span>
            )}
          </div>
          <p className="result-summary" data-testid="validation-summary-text">
            {validationReport.status === 'NOT_RUNNABLE'
              ? 'Docker is not installed or not running on this machine, so nothing was executed. Your code was not run on the host. Start Docker and run validation again.'
              : validationReport.summary}
          </p>
          {validationReport.is_runnable && (
            <div className="staging-line" data-testid="staging-line">
              <span className="field-label">Dependencies</span>
              {install ? (
                <span className={install.status === 'PASSED' ? 'state state-up_to_date' : 'state tone-bad'}>
                  {install.status === 'PASSED' ? 'Installed offline in the sandbox' : 'Offline install failed'}
                </span>
              ) : (
                <span className="state tone-muted">Not installed</span>
              )}
              {stagingNotes.length > 0 && <span className="hint">{stagingNotes[0]}</span>}
            </div>
          )}
          {validationReport.status === 'PASSED' && !ranTests && (
            <p className="hint">
              {install ? 'Dependencies installed and syntax checks passed.' : 'Only syntax checks ran.'} No test suite was
              executed.
            </p>
          )}

          {validationReport.commands.length > 0 && (
            <div className="table-scroll">
              <table className="data-table" data-testid="validation-commands-table">
                <thead>
                  <tr>
                    <th scope="col">Check</th>
                    <th scope="col">Result</th>
                    <th scope="col" className="opt">Exit</th>
                    <th scope="col" className="opt">Time</th>
                    <th scope="col" className="opt">What it does</th>
                  </tr>
                </thead>
                <tbody>
                  {validationReport.commands.map((cmd, idx) => (
                    <tr key={idx}>
                      <td className="mono strong">{COMMAND_LABEL[cmd.command_name] ?? cmd.command_name}</td>
                      <td>
                        <span className={`state tone-${STATUS_TEXT[cmd.status]?.tone || 'muted'}`}>
                          {STATUS_TEXT[cmd.status]?.label || cmd.status}
                        </span>
                      </td>
                      <td className="mono opt">{cmd.exit_code}</td>
                      <td className="mono dim opt">{cmd.duration_seconds}s</td>
                      <td className="dim wrap opt">{cmd.description}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {validationReport.logs && validationReport.is_runnable && (
            <div className="console" data-testid="validation-logs-section">
              <div className="diff-bar">
                <span>Container output</span>
                <span className="console-actions">
                  <button type="button" className="link-button" onClick={handleCopyLogs}>
                    {copyFeedback ? 'Copied' : 'Copy'}
                  </button>
                  <button
                    type="button"
                    className="link-button"
                    onClick={() => setShowLogs(!showLogs)}
                    aria-expanded={showLogs}
                  >
                    {showLogs ? 'Hide' : 'Show'}
                  </button>
                </span>
              </div>
              {showLogs && (
                <pre className="console-body mono" data-testid="validation-logs-text" tabIndex={0}>
                  {validationReport.logs}
                </pre>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
};
