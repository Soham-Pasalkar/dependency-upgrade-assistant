import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  cancelResolutionJob,
  getResolution,
  getResolutionJob,
  startResolution,
  type JobView,
  type ResolutionReport,
  type ResolutionView,
} from '../api/client';

interface CompatibilityPanelProps {
  proposalId: string;
  /** Called with true only when the server reports current, verified COMPATIBLE evidence. */
  onEligibilityChange: (eligible: boolean) => void;
  pollMs?: number;
}

const ACTIVE = new Set(['QUEUED', 'RUNNING']);

const STATUS_LABEL: Record<string, string> = {
  COMPATIBLE: 'Compatible',
  CONFLICT: 'Conflict',
  UNSUPPORTED: 'Not supported',
  RESOLUTION_FAILED: 'Check failed',
  RESOLVER_UNAVAILABLE: 'Resolver unavailable',
  UNKNOWN: 'Not verified',
};

const STATUS_TONE: Record<string, string> = {
  COMPATIBLE: 'state-up_to_date',
  CONFLICT: 'state-act-reject_conflict',
  UNSUPPORTED: 'tone-muted',
  RESOLUTION_FAILED: 'state-act-reject_conflict',
  RESOLVER_UNAVAILABLE: 'tone-muted',
  UNKNOWN: 'tone-muted',
};

function unknownReason(view: ResolutionView): string {
  const reason = view.reason || '';
  if (reason.startsWith('evidence_stale')) {
    return 'The plan, its constraints or the resolver settings changed since the last check. Run it again.';
  }
  if (reason === 'job_cancelled') return 'The last check was cancelled. Run it again to verify this plan.';
  if (reason === 'job_interrupted') return 'The last check was interrupted by a server restart. Run it again.';
  return 'This plan has not been checked yet. Approval stays locked until the proposed dependency set resolves.';
}

function failureText(view: ResolutionView): string {
  if (view.reason === 'timeout') return 'The check did not finish within its time limit. No result was recorded.';
  const failure = view.proposed?.failure || view.reason || '';
  return failure ? `The check could not produce verifiable evidence (${failure}).` : 'The check failed.';
}

function roleStep(job: JobView): string {
  if (job.status === 'QUEUED') return 'Waiting for a resolver slot';
  if (job.current_role === 'original') return 'Resolving the current dependency set (1 of 2)';
  if (job.current_role === 'proposed') return 'Resolving the proposed dependency set (2 of 2)';
  return 'Starting the isolated resolver';
}

const PackageTable: React.FC<{ report: ResolutionReport; title: string; testId: string }> = ({ report, title, testId }) => (
  <details className="compat-details" data-testid={testId}>
    <summary>
      {title} <span className="hint">({report.packages.length} packages)</span>
    </summary>
    <div className="table-scroll">
      <table className="data-table">
        <thead>
          <tr>
            <th scope="col">Package</th>
            <th scope="col">Version</th>
            <th scope="col">Dependency</th>
            <th scope="col">Introduced</th>
            <th scope="col">Uploaded (UTC)</th>
          </tr>
        </thead>
        <tbody>
          {report.packages.map((p) => (
            <tr key={p.name} className={p.introduced ? 'compat-introduced' : undefined}>
              <td className="mono">{p.name}</td>
              <td className="mono">{p.version}</td>
              <td>{p.direct ? 'direct' : 'transitive'}</td>
              <td>{p.introduced == null ? '—' : p.introduced ? 'new' : 'no'}</td>
              <td className="mono" title={p.upload.available ? `${p.upload.source} · ${p.upload.filename}` : p.upload.reason ?? ''}>
                {p.upload.available && p.upload.value ? p.upload.value.replace('T', ' ').replace(/\.\d+Z$|Z$/, '') : 'date unavailable'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  </details>
);

export const CompatibilityPanel: React.FC<CompatibilityPanelProps> = ({ proposalId, onEligibilityChange, pollMs = 1000 }) => {
  const [view, setView] = useState<ResolutionView | null>(null);
  const [job, setJob] = useState<JobView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const timer = useRef<number | null>(null);
  const alive = useRef(true);

  const apply = useCallback(
    (next: ResolutionView) => {
      if (!alive.current) return;
      setView(next);
      onEligibilityChange(next.approval_eligible === true && next.status === 'COMPATIBLE');
      if (next.job && ACTIVE.has(next.job.status)) setJob(next.job);
    },
    [onEligibilityChange]
  );

  const refresh = useCallback(async () => {
    try {
      apply(await getResolution(proposalId));
      setError(null);
    } catch (e) {
      if (alive.current) setError(e instanceof Error ? e.message : String(e));
      onEligibilityChange(false);
    }
  }, [apply, onEligibilityChange, proposalId]);

  useEffect(() => {
    alive.current = true;
    void refresh();
    return () => {
      alive.current = false;
      if (timer.current) window.clearTimeout(timer.current);
    };
  }, [proposalId, refresh, onEligibilityChange]);

  useEffect(() => {
    if (!job || !ACTIVE.has(job.status)) return;
    timer.current = window.setTimeout(async () => {
      try {
        const next = await getResolutionJob(job.id);
        if (!alive.current) return;
        if (ACTIVE.has(next.status)) setJob(next);
        else {
          setJob(null);
          await refresh();
        }
      } catch (e) {
        if (alive.current) setError(e instanceof Error ? e.message : String(e));
      }
    }, pollMs);
    return () => {
      if (timer.current) window.clearTimeout(timer.current);
    };
  }, [job, pollMs, refresh]);

  const start = async () => {
    setBusy(true);
    setError(null);
    onEligibilityChange(false);
    try {
      const res = await startResolution(proposalId);
      apply(res.resolution);
      if (res.job) setJob(res.job);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const cancel = async () => {
    if (!job) return;
    try {
      setJob(await cancelResolutionJob(job.id));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const running = job !== null && ACTIVE.has(job.status);
  const status = running ? 'CHECKING' : view?.status ?? 'UNKNOWN';
  const proposed = view?.proposed ?? null;
  const comparison = view?.comparison ?? null;

  return (
    <div className="compat" data-testid="compatibility-panel" aria-live="polite">
      <div className="compat-head">
        <div>
          <h4 className="compat-title">Compatibility</h4>
          <p className="stage-desc">
            pip resolves the complete proposed dependency set in an isolated container. Only a verified compatible
            result can be approved.
          </p>
        </div>
        <span className={`state ${running ? 'tone-muted' : STATUS_TONE[status] ?? 'tone-muted'}`} data-testid="compat-status">
          {running ? 'Checking' : STATUS_LABEL[status] ?? status}
        </span>
      </div>

      {running && job && (
        <div className="compat-progress" data-testid="compat-progress">
          <span className="spinner" aria-hidden="true" />
          <span>{job.cancel_requested ? 'Cancelling' : roleStep(job)}</span>
          <button type="button" className="btn btn-sm btn-ghost" onClick={cancel} disabled={job.cancel_requested} data-testid="compat-cancel">
            Cancel
          </button>
        </div>
      )}

      {!running && view && view.status === 'COMPATIBLE' && proposed && (
        <div className="compat-result" data-testid="compat-compatible">
          <p className="notice compat-disclosure" data-testid="compat-disclosure">
            {view.disclosure}
          </p>
          <dl className="tally tally-compact">
            <div>
              <dt>Resolved</dt>
              <dd className="mono">{proposed.packages.length}</dd>
            </div>
            <div>
              <dt>Direct</dt>
              <dd className="mono">{proposed.packages.filter((p) => p.direct).length}</dd>
            </div>
            <div>
              <dt>Transitive</dt>
              <dd className="mono">{proposed.packages.filter((p) => !p.direct).length}</dd>
            </div>
            <div>
              <dt>Resolver</dt>
              <dd className="mono">
                {proposed.resolver} · CPython {proposed.target?.python} {proposed.target?.machine}
              </dd>
            </div>
          </dl>
          {comparison?.available ? (
            <p data-testid="compat-introduced">
              {comparison.introduced.length === 0 ? (
                'This upgrade introduces no new packages.'
              ) : (
                <>
                  New packages introduced by this upgrade:{' '}
                  {comparison.introduced.map((n, i) => (
                    <React.Fragment key={n}>
                      {i > 0 && ', '}
                      <span className="tag tag-warn mono">{n}</span>
                    </React.Fragment>
                  ))}
                  . Review them before approving.
                </>
              )}
            </p>
          ) : (
            <p className="hint" data-testid="compat-introduced">
              comparison unavailable{comparison?.reason ? ` (${comparison.reason.replace(/_/g, ' ')})` : ''}
            </p>
          )}
          {proposed.warnings.length > 0 && (
            <ul className="notice-list hint" data-testid="compat-warnings">
              {proposed.warnings.map((w) => (
                <li key={w}>{w}</li>
              ))}
            </ul>
          )}
          <PackageTable report={proposed} title="Proposed resolved dependencies" testId="compat-proposed-table" />
          {view.original && view.original.packages.length > 0 && (
            <PackageTable report={view.original} title="Current resolved dependencies" testId="compat-original-table" />
          )}
        </div>
      )}

      {!running && view && view.status === 'CONFLICT' && proposed && (
        <div className="notice notice-bad" role="alert" data-testid="compat-conflict">
          <p className="notice-title">The proposed dependency set does not resolve</p>
          {proposed.conflicts.map((c, i) => (
            <div key={i}>
              <p>{c.summary}</p>
              <ul className="notice-list mono">
                {c.causes.map((cause) => (
                  <li key={cause}>{cause}</li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      )}

      {!running && view && view.status === 'UNSUPPORTED' && proposed && (
        <div className="notice" data-testid="compat-unsupported">
          <p className="notice-title">Compatibility cannot be verified for this plan</p>
          <ul className="notice-list">
            {proposed.unsupported.map((u, i) => (
              <li key={i}>
                {u.message}
                {u.line_number ? <span className="hint mono"> (line {u.line_number})</span> : null}
              </li>
            ))}
          </ul>
        </div>
      )}

      {!running && view && view.status === 'RESOLUTION_FAILED' && (
        <div className="notice notice-bad" role="alert" data-testid="compat-failed">
          <p className="notice-title">Compatibility not verified</p>
          <p>{failureText(view)}</p>
        </div>
      )}

      {!running && view && view.status === 'RESOLVER_UNAVAILABLE' && (
        <div className="notice" data-testid="compat-unavailable">
          <p className="notice-title">The isolated resolver is unavailable</p>
          <p>{view.job?.error_message || 'Docker is required for compatibility checks. Start Docker and run the check again.'}</p>
        </div>
      )}

      {!running && view && view.status === 'UNKNOWN' && (
        <p className="hint" data-testid="compat-unknown">
          {unknownReason(view)}
        </p>
      )}

      {error && (
        <div className="notice notice-bad" role="alert" data-testid="compat-error">
          <p>{error}</p>
        </div>
      )}

      {!running && view?.status !== 'UNSUPPORTED' && (
        <div>
          <button type="button" className="btn btn-primary" onClick={start} disabled={busy} aria-busy={busy} data-testid="compat-start">
            {busy ? 'Starting' : view?.status === 'UNKNOWN' && !view.proposed ? 'Check compatibility' : 'Check again'}
          </button>
        </div>
      )}
    </div>
  );
};
