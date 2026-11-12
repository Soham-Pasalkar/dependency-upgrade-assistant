import React, { useState } from 'react';
import type { UpgradeProposalResponse } from '../api/client';

interface PlanReviewerProps {
  proposal: UpgradeProposalResponse;
  onApprove: (approvalNote?: string) => void;
  isApproved: boolean;
  approvalNote?: string;
  isApproving?: boolean;
  approvalError?: string | null;
  stageIndex?: string;
}

const ACTION_LABEL: Record<string, string> = {
  UPGRADE: 'Upgrade',
  RETAIN: 'Keep',
  REJECT_CONFLICT: 'Conflict',
  REJECT_UNSUPPORTED: 'Not managed',
  REJECT_STALE: 'Stale',
};

const lineClass = (line: string) => {
  if (line.startsWith('+++') || line.startsWith('---')) return 'd-file';
  if (line.startsWith('@@')) return 'd-hunk';
  if (line.startsWith('+')) return 'd-add';
  if (line.startsWith('-')) return 'd-del';
  return 'd-ctx';
};

export const PlanReviewer: React.FC<PlanReviewerProps> = ({
  proposal,
  onApprove,
  isApproved,
  approvalNote,
  isApproving = false,
  approvalError = null,
  stageIndex = '05',
}) => {
  const [isChecked, setIsChecked] = useState<boolean>(isApproved);
  const [noteInput, setNoteInput] = useState<string>(approvalNote || '');
  const [isDiffExpanded, setIsDiffExpanded] = useState<boolean>(true);

  const changes = proposal.plan_items.filter((i) => i.action === 'UPGRADE');
  const integrity = proposal.integrity;
  const canApprove = integrity.is_valid && proposal.total_planned > 0;
  const diffLines = proposal.unified_diff ? proposal.unified_diff.replace(/\n$/, '').split('\n') : [];

  const handleConfirmApproval = () => {
    if (!isChecked || !canApprove) return;
    onApprove(noteInput.trim() || undefined);
  };

  return (
    <div className="stage-body" data-testid="plan-reviewer-section">
      <dl className="tally tally-compact">
        <div>
          <dt>File</dt>
          <dd className="mono">{proposal.file_path}</dd>
        </div>
        <div className="tone-warn">
          <dt>Total planned</dt>
          <dd>{proposal.total_planned}</dd>
        </div>
        <div className="tone-muted">
          <dt>Unchanged</dt>
          <dd>{proposal.total_unchanged}</dd>
        </div>
        <div className={integrity.is_valid ? 'tone-ok' : 'tone-bad'}>
          <dt>Patch integrity</dt>
          <dd>{integrity.is_valid ? 'Verified' : 'Failed'}</dd>
        </div>
      </dl>

      {changes.length > 0 && (
        <div className="table-scroll">
          <table className="data-table" aria-label="Planned changes">
            <thead>
              <tr>
                <th scope="col">Package</th>
                <th scope="col" className="opt">From</th>
                <th scope="col">To</th>
                <th scope="col" className="opt">Specifier</th>
                <th scope="col">Action</th>
                <th scope="col" className="opt">Reason</th>
              </tr>
            </thead>
            <tbody>
              {proposal.plan_items.map((item, idx) => (
                <tr key={`${item.package_name}-${idx}`} data-testid={`plan-item-${item.package_name}`}>
                  <td className="mono strong">{item.package_name}</td>
                  <td className="mono dim opt">{item.current_version || '—'}</td>
                  <td className="mono">{item.target_version || '—'}</td>
                  <td className="mono dim opt">
                    {item.declared_specifier || 'none'} <span aria-hidden="true">›</span>
                    <span className="visually-hidden"> to </span> {item.proposed_specifier || 'none'}
                  </td>
                  <td>
                    <span className={`state state-act-${item.action.toLowerCase()}`}>
                      {ACTION_LABEL[item.action] || item.action}
                    </span>
                  </td>
                  <td className="dim wrap opt">{item.rationale}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="diff" data-testid="unified-diff-viewer">
        <div className="diff-bar">
          <span className="mono">{proposal.file_path}</span>
          <span className="diff-stats mono">
            <span className="d-add-text">+{diffLines.filter((l) => l.startsWith('+') && !l.startsWith('+++')).length}</span>{' '}
            <span className="d-del-text">−{diffLines.filter((l) => l.startsWith('-') && !l.startsWith('---')).length}</span>
          </span>
          <button
            type="button"
            className="link-button"
            onClick={() => setIsDiffExpanded(!isDiffExpanded)}
            aria-expanded={isDiffExpanded}
            data-testid="toggle-diff-btn"
          >
            {isDiffExpanded ? 'Collapse' : 'Expand'}
          </button>
        </div>
        {isDiffExpanded && (
          <div className="diff-body" data-testid="diff-content" tabIndex={0} aria-label="Unified diff">
            {diffLines.length === 0 ? (
              <p className="diff-empty">No changes. Every package is already at its recommended version.</p>
            ) : (
              diffLines.map((line, idx) => (
                <div key={idx} className={`diff-line ${lineClass(line)}`}>
                  <span className="diff-num" aria-hidden="true">{idx + 1}</span>
                  <span className="diff-text">{line}</span>
                </div>
              ))
            )}
          </div>
        )}
      </div>

      {(integrity.checks_failed.length > 0 || integrity.errors.length > 0) && (
        <div className="notice notice-bad" role="alert">
          <p className="notice-title">The patch did not pass its checks</p>
          <ul className="notice-list">
            {[...integrity.checks_failed, ...integrity.errors].map((m, i) => (
              <li key={i}>{m}</li>
            ))}
          </ul>
        </div>
      )}

      <div id="stage-approval" className="approval" data-testid="human-approval-gate">
        <div className="approval-head">
          <span className="stage-index mono">{stageIndex}</span>
          <div>
            <h3 className="approval-title" tabIndex={-1} data-stage-heading>
              Approval
            </h3>
            <p className="stage-desc">
              Validation and export stay locked until you approve this exact diff. Approval is recorded on the
              server against the diff's checksum.
            </p>
          </div>
          <span
            className={`state ${isApproved ? 'state-up_to_date' : canApprove ? 'state-upgrade_available' : 'tone-muted'}`}
            data-testid="approval-status-badge"
          >
            {isApproved ? 'Approved' : canApprove ? 'Awaiting review' : 'Nothing to approve'}
          </span>
        </div>

        {isApproved ? (
          <p className="approval-done" data-testid="approval-confirmed-banner">
            Approval recorded{noteInput ? <> with note <q>{noteInput}</q></> : ''}. You can now validate and export this change.
          </p>
        ) : !canApprove ? (
          <p className="hint">
            {proposal.total_planned === 0
              ? 'There is nothing to approve: the plan contains no changes.'
              : 'This plan cannot be approved because its patch failed integrity checks.'}
          </p>
        ) : (
          <div className="approval-form">
            <label className="check" htmlFor="approval-confirmation-checkbox">
              <input
                id="approval-confirmation-checkbox"
                type="checkbox"
                checked={isChecked}
                onChange={(e) => setIsChecked(e.target.checked)}
                data-testid="approval-checkbox"
              />
              <span>I have read the diff above and approve these changes.</span>
            </label>
            <div className="approval-row">
              <label className="field">
                <span className="visually-hidden">Approval note (optional)</span>
                <input
                  type="text"
                  className="input"
                  placeholder="Note for the report (optional)"
                  value={noteInput}
                  onChange={(e) => setNoteInput(e.target.value)}
                  data-testid="approval-note-input"
                />
              </label>
              <button
                type="button"
                className="btn btn-primary"
                onClick={handleConfirmApproval}
                disabled={!isChecked || isApproving}
                aria-busy={isApproving}
                data-testid="confirm-approval-btn"
              >
                {isApproving ? (
                  <>
                    <span className="spinner" aria-hidden="true" /> Recording approval
                  </>
                ) : (
                  'Approve changes'
                )}
              </button>
            </div>
            {approvalError && (
              <div className="notice notice-bad" role="alert" data-testid="approval-error">
                <p className="notice-title">Approval was not recorded</p>
                <p>{approvalError}</p>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
};
