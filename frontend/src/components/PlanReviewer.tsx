import React, { useState } from 'react';
import type { UpgradeProposalResponse } from '../api/client';

interface PlanReviewerProps {
  proposal: UpgradeProposalResponse;
  onApprove: (approvalNote?: string) => void;
  isApproved: boolean;
  approvalNote?: string;
}

export const PlanReviewer: React.FC<PlanReviewerProps> = ({
  proposal,
  onApprove,
  isApproved,
  approvalNote,
}) => {
  const [isChecked, setIsChecked] = useState<boolean>(isApproved);
  const [noteInput, setNoteInput] = useState<string>(approvalNote || '');
  const [isDiffExpanded, setIsDiffExpanded] = useState<boolean>(true);

  const handleConfirmApproval = () => {
    if (!isChecked) return;
    onApprove(noteInput.trim() || undefined);
  };

  const renderDiffLines = (diff: string) => {
    if (!diff) return <div className="diff-empty text-muted">No differences detected.</div>;

    const lines = diff.split('\n');
    return lines.map((line, idx) => {
      let lineClass = 'diff-line-context';
      if (line.startsWith('+') && !line.startsWith('+++')) {
        lineClass = 'diff-line-add';
      } else if (line.startsWith('-') && !line.startsWith('---')) {
        lineClass = 'diff-line-del';
      } else if (line.startsWith('@@')) {
        lineClass = 'diff-line-hunk';
      } else if (line.startsWith('---') || line.startsWith('+++')) {
        lineClass = 'diff-line-header';
      }

      return (
        <div key={idx} className={`diff-line ${lineClass}`}>
          <span className="diff-line-num">{idx + 1}</span>
          <span className="diff-line-content">{line}</span>
        </div>
      );
    });
  };

  return (
    <div className="plan-reviewer-card" data-testid="plan-reviewer-section">
      <div className="plan-header">
        <div className="plan-title-group">
          <h3 className="section-title">Upgrade Plan &amp; Diff Review</h3>
          <p className="section-description">
            Inspect deterministic dependency changes, verify unified patch integrity, and record
            explicit human approval before sandboxed testing.
          </p>
        </div>

        <div className="plan-badges">
          <span className="badge-proposal-id font-mono" data-testid="proposal-id-badge">
            {proposal.proposal_id}
          </span>
          <span
            className={`status-badge ${isApproved ? 'status-approved' : 'status-review'}`}
            data-testid="approval-status-badge"
          >
            {isApproved ? '✓ Human Approved' : 'Pending Review'}
          </span>
        </div>
      </div>

      {/* Metrics Summary Strip */}
      <div className="plan-metrics-grid">
        <div className="plan-metric-card">
          <span className="metric-label">Target Manifest</span>
          <span className="metric-value font-mono">{proposal.file_path}</span>
        </div>
        <div className="plan-metric-card">
          <span className="metric-label">Total Planned</span>
          <span className="metric-value text-emerald">{proposal.total_planned}</span>
        </div>
        <div className="plan-metric-card">
          <span className="metric-label">Unchanged</span>
          <span className="metric-value text-muted">{proposal.total_unchanged}</span>
        </div>
        <div className="plan-metric-card">
          <span className="metric-label">Patch Integrity</span>
          <span
            className={`metric-value ${
              proposal.integrity.is_valid ? 'text-emerald' : 'text-rose'
            }`}
          >
            {proposal.integrity.is_valid ? '✓ Verified' : '⚠ Invalid'}
          </span>
        </div>
      </div>

      {/* Planned Upgrade Items Table */}
      {proposal.plan_items.length > 0 && (
        <div className="plan-items-section">
          <h4 className="subheading">Planned Package Modifications</h4>
          <div className="artifacts-table-container">
            <table className="artifacts-table" aria-label="Planned upgrade items">
              <thead>
                <tr>
                  <th scope="col">Package</th>
                  <th scope="col">Current Version</th>
                  <th scope="col">Target Version</th>
                  <th scope="col">Specifier Transition</th>
                  <th scope="col">Action</th>
                  <th scope="col">Rationale</th>
                </tr>
              </thead>
              <tbody>
                {proposal.plan_items.map((item, idx) => (
                  <tr key={`${item.package_name}-${idx}`} data-testid={`plan-item-${item.package_name}`}>
                    <td className="font-mono text-bold">{item.package_name}</td>
                    <td className="font-mono">{item.current_version || '—'}</td>
                    <td className="font-mono text-emerald font-bold">
                      {item.target_version || '—'}
                    </td>
                    <td className="font-mono">
                      <code>{item.declared_specifier || 'none'}</code> &rarr;{' '}
                      <code>{item.proposed_specifier || 'none'}</code>
                    </td>
                    <td>
                      <span className={`artifact-badge badge-${item.action.toLowerCase()}`}>
                        {item.action}
                      </span>
                    </td>
                    <td className="text-secondary">{item.rationale}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Unified Diff Viewer */}
      <div className="diff-viewer-section" data-testid="unified-diff-viewer">
        <div className="diff-header">
          <span className="diff-title">Unified Patch Diff (<code>{proposal.file_path}</code>)</span>
          <button
            type="button"
            className="btn-icon"
            onClick={() => setIsDiffExpanded(!isDiffExpanded)}
            data-testid="toggle-diff-btn"
          >
            {isDiffExpanded ? 'Collapse Diff' : 'Expand Diff'}
          </button>
        </div>

        {isDiffExpanded && (
          <div className="diff-content-box" data-testid="diff-content">
            {renderDiffLines(proposal.unified_diff)}
          </div>
        )}
      </div>

      {/* Explicit Human Approval Gate */}
      <div className="approval-gate-card" data-testid="human-approval-gate">
        <div className="approval-gate-header">
          <span className="gate-icon">🛡</span>
          <div className="gate-header-text">
            <h4>Human-in-the-Loop Review Gate</h4>
            <p>
              Security policy requires explicit human developer confirmation before any dependency
              modifications can be tested or exported.
            </p>
          </div>
        </div>

        {isApproved ? (
          <div className="approval-confirmed-box" data-testid="approval-confirmed-banner">
            <span className="confirmed-icon">✓</span>
            <div className="confirmed-details">
              <strong>Human Confirmation Recorded</strong>
              <p className="note-text">
                {noteInput ? `Reviewer Note: "${noteInput}"` : 'Explicit authorization confirmed.'}
              </p>
            </div>
          </div>
        ) : (
          <div className="approval-form">
            <label className="checkbox-label" htmlFor="approval-confirmation-checkbox">
              <input
                id="approval-confirmation-checkbox"
                type="checkbox"
                checked={isChecked}
                onChange={(e) => setIsChecked(e.target.checked)}
                data-testid="approval-checkbox"
              />
              <span>I have inspected the planned changes and approve proceeding with this upgrade proposal.</span>
            </label>

            <div className="approval-input-row">
              <input
                type="text"
                className="approval-note-input"
                placeholder="Optional reviewer notes or confirmation audit comment..."
                value={noteInput}
                onChange={(e) => setNoteInput(e.target.value)}
                data-testid="approval-note-input"
              />
              <button
                type="button"
                className="btn-primary btn-approve"
                onClick={handleConfirmApproval}
                disabled={!isChecked}
                data-testid="confirm-approval-btn"
              >
                Confirm Human Approval
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
};
