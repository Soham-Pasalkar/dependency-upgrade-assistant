import React, { useCallback, useEffect, useState } from 'react';
import { getHealth, type HealthResponse } from '../api/client';

/**
 * Compact service indicator for the application header.
 * Shows whether the analysis service is reachable; offers a retry when it is not.
 */
export const BackendStatus: React.FC = () => {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [failed, setFailed] = useState<boolean>(false);

  const probe = useCallback(async () => {
    try {
      setHealth(await getHealth());
      setFailed(false);
    } catch {
      setHealth(null);
      setFailed(true);
    } finally {
      setLoading(false);
    }
  }, []);

  const check = () => {
    setLoading(true);
    void probe();
  };

  useEffect(() => {
    void probe();
  }, [probe]);

  if (loading) {
    return (
      <span className="service-status" data-testid="status-loading" role="status">
        <span className="signal signal-idle" aria-hidden="true" />
        Connecting
      </span>
    );
  }

  if (failed || !health) {
    return (
      <span className="service-status is-down" data-testid="status-error" role="status">
        <span className="signal signal-bad" aria-hidden="true" />
        Service unavailable
        <button
          type="button"
          className="link-button"
          onClick={check}
          data-testid="refresh-status-button"
        >
          Retry
        </button>
      </span>
    );
  }

  const healthy = health.status === 'healthy';
  return (
    <span
      className={`service-status ${healthy ? '' : 'is-degraded'}`}
      data-testid="status-success"
      role="status"
      title={`Version ${health.version}`}
    >
      <span className={`signal ${healthy ? 'signal-ok' : 'signal-warn'}`} aria-hidden="true" />
      {healthy ? 'Service online' : 'Service degraded'}
    </span>
  );
};
