import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { PlanReviewer } from './PlanReviewer';
import type { UpgradeProposalResponse } from '../api/client';

describe('PlanReviewer Component', () => {
  const mockProposal: UpgradeProposalResponse = {
    proposal_id: 'prop_unit_test_123',
    manifest_type: 'requirements.txt',
    ecosystem: 'python',
    file_path: 'requirements.txt',
    plan_items: [
      {
        package_name: 'requests',
        ecosystem: 'python',
        current_version: '2.28.0',
        target_version: '2.32.3',
        declared_specifier: '==2.28.0',
        proposed_specifier: '==2.32.3',
        action: 'UPGRADE',
        rationale: 'Security update addressing CVE-2023-32681',
        line_number: 1,
        dependency_type: 'runtime',
        is_conflict: false,
      },
    ],
    total_planned: 1,
    total_unchanged: 3,
    total_conflicts: 0,
    original_content: 'requests==2.28.0\n',
    proposed_content: 'requests==2.32.3\n',
    unified_diff:
      '--- a/requirements.txt\n+++ b/requirements.txt\n@@ -1,1 +1,1 @@\n-requests==2.28.0\n+requests==2.32.3\n',
    integrity: {
      is_valid: true,
      can_apply: true,
      can_reverse: true,
      syntax_valid: true,
      target_versions_match: true,
      unrelated_dependencies_unchanged: true,
      original_unchanged: true,
      checks_passed: ['syntax_check', 'applicability_check'],
      checks_failed: [],
      errors: [],
      warnings: [],
    },
    status: 'READY_FOR_REVIEW',
    requires_human_approval: true,
    diagnostics: [],
    summary: '1 dependency planned for upgrade',
  };

  it('renders proposal metrics, proposal ID, and patch integrity badge', () => {
    render(
      <PlanReviewer
        proposal={mockProposal}
        onApprove={vi.fn()}
        isApproved={false}
        approvalEligible
      />
    );

    expect(screen.getAllByText('requirements.txt').length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText('Total planned')).toBeInTheDocument();
    expect(screen.getAllByText('1').length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText('Verified')).toBeInTheDocument();
    expect(screen.getByTestId('approval-status-badge')).toHaveTextContent('Awaiting review');
  });

  it('renders planned upgrade items table with versions and rationale', () => {
    render(
      <PlanReviewer
        proposal={mockProposal}
        onApprove={vi.fn()}
        isApproved={false}
      />
    );

    const row = screen.getByTestId('plan-item-requests');
    expect(row).toBeInTheDocument();
    expect(row).toHaveTextContent('requests');
    expect(row).toHaveTextContent('2.28.0');
    expect(row).toHaveTextContent('2.32.3');
    expect(row).toHaveTextContent('Security update addressing CVE-2023-32681');
    expect(row).toHaveTextContent('Upgrade');
  });

  it('renders unified diff lines and toggles diff visibility', () => {
    render(
      <PlanReviewer
        proposal={mockProposal}
        onApprove={vi.fn()}
        isApproved={false}
      />
    );

    const diffContent = screen.getByTestId('diff-content');
    expect(diffContent).toBeInTheDocument();
    expect(screen.getByText('-requests==2.28.0')).toBeInTheDocument();
    expect(screen.getByText('+requests==2.32.3')).toBeInTheDocument();

    const toggleBtn = screen.getByTestId('toggle-diff-btn');
    fireEvent.click(toggleBtn);
    expect(screen.queryByTestId('diff-content')).not.toBeInTheDocument();

    fireEvent.click(toggleBtn);
    expect(screen.getByTestId('diff-content')).toBeInTheDocument();
  });

  it('keeps approval locked until compatibility is verified', () => {
    render(<PlanReviewer proposal={mockProposal} onApprove={vi.fn()} isApproved={false} />);
    expect(screen.getByTestId('approval-status-badge')).toHaveTextContent('Locked');
    expect(screen.getByTestId('approval-locked')).toBeInTheDocument();
    expect(screen.queryByTestId('confirm-approval-btn')).not.toBeInTheDocument();
  });

  it('renders the compatibility panel above the approval gate', () => {
    render(
      <PlanReviewer
        proposal={mockProposal}
        onApprove={vi.fn()}
        isApproved={false}
        compatibility={<div data-testid="compat-slot" />}
      />
    );
    const slot = screen.getByTestId('compat-slot');
    const gate = screen.getByTestId('human-approval-gate');
    expect(slot.compareDocumentPosition(gate) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('enforces human confirmation checkbox before enabling approval button', () => {
    const handleApprove = vi.fn();
    render(
      <PlanReviewer
        proposal={mockProposal}
        onApprove={handleApprove}
        isApproved={false}
        approvalEligible
      />
    );

    const confirmBtn = screen.getByTestId('confirm-approval-btn');
    const checkbox = screen.getByTestId('approval-checkbox');

    expect(confirmBtn).toBeDisabled();

    fireEvent.click(checkbox);
    expect(confirmBtn).not.toBeDisabled();

    const noteInput = screen.getByTestId('approval-note-input');
    fireEvent.change(noteInput, { target: { value: 'LGTM by security lead' } });

    fireEvent.click(confirmBtn);
    expect(handleApprove).toHaveBeenCalledWith('LGTM by security lead');
  });

  it('renders confirmed approval banner when isApproved is true', () => {
    render(
      <PlanReviewer
        proposal={mockProposal}
        onApprove={vi.fn()}
        isApproved={true}
        approvalNote="Approved in sprint review"
      />
    );

    expect(screen.getByTestId('approval-status-badge')).toHaveTextContent('Approved');
    expect(screen.getByTestId('approval-confirmed-banner')).toHaveTextContent(
      'Approved in sprint review'
    );
    expect(screen.queryByTestId('confirm-approval-btn')).not.toBeInTheDocument();
  });

  it('does not allow approving a patch that failed integrity checks', () => {
    render(
      <PlanReviewer
        proposal={{
          ...mockProposal,
          integrity: { ...mockProposal.integrity, is_valid: false, checks_failed: ['applicability_check'] },
        }}
        onApprove={vi.fn()}
        isApproved={false}
      />
    );
    expect(screen.getByText('Failed')).toBeInTheDocument();
    expect(screen.queryByTestId('confirm-approval-btn')).not.toBeInTheDocument();
    expect(screen.getByText(/cannot be approved/i)).toBeInTheDocument();
  });
});
