import React from 'react';
import { BackendStatus } from './components/BackendStatus';
import { ManifestAnalyzer } from './components/ManifestAnalyzer';
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
        {/* M2 Manifest Ingestion and Dependency Analysis Section */}
        <section className="dashboard-section">
          <div className="section-header">
            <span className="section-label">Milestone M2 Manifest Analysis</span>
            <span className="status-pill">Active Vertical Slice</span>
          </div>
          <ManifestAnalyzer />
        </section>

        {/* M1 System Foundation & Health Section */}
        <section className="dashboard-section">
          <div className="section-header">
            <span className="section-label">Milestone M1 Foundation</span>
            <span className="status-pill">System Health</span>
          </div>
          <BackendStatus />
        </section>

        <section className="info-card">
          <h3 className="info-title">System Architecture Status</h3>
          <ul className="info-list">
            <li>
              <strong>Manifest Evaluation:</strong> Pure-Python AST &amp; SemVer evaluators with PyPI and npm registry integration
            </li>
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
              <strong>Security:</strong> Zero host code execution, in-memory credential protection, and strict URL boundaries
            </li>
          </ul>
        </section>
      </main>

      <footer className="app-footer">
        <p>Dependency Upgrade Assistant 2.0 &bull; Phase 2 Construction &bull; Milestone M2 Ingestion &amp; Analysis</p>
      </footer>
    </div>
  );
};

export default App;
