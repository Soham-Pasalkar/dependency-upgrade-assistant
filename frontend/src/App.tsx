import React from 'react';
import { BackendStatus } from './components/BackendStatus';
import './App.css';

export const App: React.FC = () => {
  return (
    <div className="app-container">
      <header className="app-header">
        <div className="header-brand">
          <span className="brand-badge">DUA 2.0</span>
          <h1 className="brand-title">Dependency Upgrade Assistant</h1>
        </div>
        <p className="brand-subtitle">
          Interactive, security-first developer platform for Python and Node.js dependency upgrades.
        </p>
      </header>

      <main className="app-main">
        <section className="dashboard-section">
          <div className="section-header">
            <span className="section-label">Milestone M1 Foundation</span>
            <span className="status-pill">Active Vertical Slice</span>
          </div>
          <BackendStatus />
        </section>

        <section className="info-card">
          <h3 className="info-title">System Architecture Status</h3>
          <ul className="info-list">
            <li>
              <strong>Backend:</strong> FastAPI on Python 3.14 (Async ASGI, OpenAPI documentation at <code>/docs</code>)
            </li>
            <li>
              <strong>Frontend:</strong> React 19 + TypeScript + Vite with native reverse proxy
            </li>
            <li>
              <strong>Persistence:</strong> Embedded SQLite with WAL mode and foreign key enforcement
            </li>
            <li>
              <strong>Communication:</strong> Typed REST client polling <code>/api/health</code> with full error recovery
            </li>
          </ul>
        </section>
      </main>

      <footer className="app-footer">
        <p>Dependency Upgrade Assistant 2.0 &bull; Phase 2 Construction &bull; Milestone M1 Foundation</p>
      </footer>
    </div>
  );
};

export default App;
