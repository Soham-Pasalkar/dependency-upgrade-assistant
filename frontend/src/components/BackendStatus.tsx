import React, { useEffect, useState } from 'react';
import { getHealth, type HealthResponse, ApiError } from '../api/client';

export const BackendStatus: React.FC = () => {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [lastChecked, setLastChecked] = useState<string | null>(null);

  const checkConnectivity = async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await getHealth();
      setHealth(data);
      setLastChecked(new Date().toLocaleTimeString());
    } catch (err) {
      setHealth(null);
      if (err instanceof ApiError) {
        setError(err.message);
      } else if (err instanceof Error) {
        setError(err.message);
      } else {
        setError('An unexpected error occurred while contacting the backend.');
      }
      setLastChecked(new Date().toLocaleTimeString());
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    checkConnectivity();
  }, []);

  return (
    <div className="status-card" data-testid="backend-status-card">
      <div className="status-card-header">
        <h2 className="status-title">Backend Connectivity</h2>
        <button
          type="button"
          onClick={checkConnectivity}
          disabled={loading}
          className="refresh-button"
          aria-label="Refresh backend status"
          data-testid="refresh-status-button"
        >
          {loading ? 'Checking...' : 'Refresh Status'}
        </button>
      </div>

      {loading && (
        <div className="status-state loading-state" data-testid="status-loading">
          <div className="spinner" role="status" aria-label="Loading" />
          <p>Connecting to backend API at <code>/api/health</code>...</p>
        </div>
      )}

      {!loading && error && (
        <div className="status-state error-state" data-testid="status-error">
          <div className="badge badge-error">Disconnected</div>
          <p className="error-message">{error}</p>
          <p className="error-hint">
            Ensure the FastAPI backend server is running on <code>http://127.0.0.1:8000</code>.
          </p>
          {lastChecked && <span className="timestamp-note">Last check attempted: {lastChecked}</span>}
        </div>
      )}

      {!loading && health && (
        <div className="status-state success-state" data-testid="status-success">
          <div className="status-badge-row">
            <span
              className={`badge ${health.status === 'healthy' ? 'badge-success' : 'badge-warning'}`}
            >
              {health.status.toUpperCase()}
            </span>
            <span className="environment-tag">{health.environment}</span>
          </div>

          <div className="details-grid">
            <div className="detail-item">
              <span className="detail-label">Service</span>
              <span className="detail-value">{health.app_name}</span>
            </div>
            <div className="detail-item">
              <span className="detail-label">Version</span>
              <span className="detail-value font-mono">{health.version}</span>
            </div>
            <div className="detail-item">
              <span className="detail-label">Database Status</span>
              <span className="detail-value font-mono">
                {health.database.status} ({health.database.message})
              </span>
            </div>
            <div className="detail-item">
              <span className="detail-label">Database Path</span>
              <span className="detail-value font-mono">{health.database.path}</span>
            </div>
          </div>

          <div className="timestamp-row">
            <span className="timestamp-note">
              Server Time (UTC): {new Date(health.timestamp).toUTCString()}
            </span>
            {lastChecked && <span className="timestamp-note">Local Check: {lastChecked}</span>}
          </div>
        </div>
      )}
    </div>
  );
};
