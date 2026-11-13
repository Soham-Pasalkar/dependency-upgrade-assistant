import React from 'react';
import { SiteHeader, BrandMark } from './SiteHeader';

const READOUT_ROWS: Array<{ name: string; from: string; to: string; source: 'registry' | 'advisory' | 'held' }> = [
  { name: 'requests', from: '2.28.0', to: '2.32.3', source: 'registry' },
  { name: 'fastapi', from: '0.115.0', to: '0.115.6', source: 'registry' },
  { name: 'pydantic', from: '2.8.2', to: '2.10.4', source: 'advisory' },
  { name: '-r base.txt', from: 'directive', to: 'kept', source: 'held' },
];

const SOURCE_LABEL = { registry: 'Registry', advisory: 'Review', held: 'Kept' } as const;

/** A still of the real interface: one manifest, its upgrade candidates and the resulting patch. */
const ProductReadout: React.FC = () => (
  <figure className="readout" aria-label="Example analysis of a requirements.txt file">
    <div className="readout-bar">
      <span className="mono">requirements.txt</span>
      <span className="readout-tag">Example</span>
    </div>

    <table className="readout-table">
      <thead>
        <tr>
          <th scope="col">Package</th>
          <th scope="col">Declared</th>
          <th scope="col">Candidate</th>
          <th scope="col">Basis</th>
        </tr>
      </thead>
      <tbody>
        {READOUT_ROWS.map((row, i) => (
          <tr key={row.name} style={{ '--i': i } as React.CSSProperties}>
            <td className="mono">{row.name}</td>
            <td className="mono dim">{row.from}</td>
            <td className="mono">{row.to}</td>
            <td>
              <span className={`basis basis-${row.source}`}>{SOURCE_LABEL[row.source]}</span>
            </td>
          </tr>
        ))}
      </tbody>
    </table>

    <pre className="readout-diff" aria-label="Resulting patch">
      <span className="d-hunk">@@ -1,3 +1,3 @@</span>
      {'\n'}
      <span className="d-del">-requests==2.28.0</span>
      {'\n'}
      <span className="d-add">+requests==2.32.3</span>
      {'\n'}
      <span className="d-ctx"> fastapi==0.115.0</span>
    </pre>

    <dl className="readout-checks">
      <div>
        <dt>Patch integrity</dt>
        <dd>Applies and reverses cleanly</dd>
      </div>
      <div>
        <dt>Sandbox</dt>
        <dd>No network, non-root</dd>
      </div>
    </dl>
  </figure>
);

const STEPS = [
  {
    title: 'Find the manifests',
    body: 'Paste a public GitHub URL. Every requirements file, pyproject.toml and package.json is found; vendored folders are skipped.',
  },
  {
    title: 'Check the registries',
    body: 'Every package is looked up on PyPI or npm to find the latest release and the latest compatible one.',
  },
  {
    title: 'Recommend upgrades',
    body: 'Candidates are ranked by risk. Model suggestions are only kept if the version exists in the registry.',
  },
  {
    title: 'Review the patch',
    body: 'The plan becomes a unified diff that is checked to apply and reverse cleanly against your file.',
  },
  {
    title: 'Approve explicitly',
    body: 'Nothing is validated or exported until you have read the diff and approved it.',
  },
  {
    title: 'Validate and export',
    body: 'Run checks in an isolated container, then download the patch, report and checksums.',
  },
];

export const Landing: React.FC = () => (
  <div className="site" data-testid="landing">
    <SiteHeader variant="home" />

    <main>
      <section className="hero" aria-labelledby="hero-title">
        <div className="hero-copy">
          <h1 id="hero-title" className="hero-title">
            <span className="line">Dependency</span>{' '}
            <span className="line">upgrades, verified</span>{' '}
            <span className="line">before they merge.</span>
          </h1>
          <p className="hero-lede">
            Point it at a GitHub repository. Dependency Upgrade Assistant finds every Python and npm
            manifest, checks each package against its registry, turns safe upgrades into a patch you can
            read line by line, and tests it in an isolated container.
          </p>
          <div className="hero-actions">
            <a href="#/analyze" className="btn btn-primary btn-lg" data-testid="hero-cta">
              Analyze a repository
            </a>
            <a href="#how-it-works" className="btn btn-ghost btn-lg">
              How it works
            </a>
          </div>
        </div>
        <div className="hero-visual">
          <ProductReadout />
        </div>
      </section>

      <section id="how-it-works" className="band" aria-labelledby="how-title">
        <div className="band-head">
          <h2 id="how-title" className="band-title">From manifest to a patch you can merge</h2>
          <p className="band-lede">
            Six steps, each one visible. You can stop at any point and keep what you have.
          </p>
        </div>
        <ol className="steps">
          {STEPS.map((step, i) => (
            <li key={step.title} className="step">
              <span className="step-index mono">{String(i + 1).padStart(2, '0')}</span>
              <h3 className="step-title">{step.title}</h3>
              <p className="step-body">{step.body}</p>
            </li>
          ))}
        </ol>
      </section>

      <section id="evidence" className="band" aria-labelledby="evidence-title">
        <div className="band-head">
          <h2 id="evidence-title" className="band-title">Facts and suggestions are kept apart</h2>
          <p className="band-lede">
            You always know whether a result was measured or proposed.
          </p>
        </div>
        <div className="ledger">
          <div className="ledger-col">
            <h3 className="ledger-title">
              <span className="basis basis-registry">Verified</span>
            </h3>
            <ul className="ledger-list">
              <li>Versions, release lists and deprecations from PyPI and npm</li>
              <li>Unified diffs generated from your own manifest</li>
              <li>Patch checks: applies, reverses, leaves other lines untouched</li>
              <li>Container exit codes and logs from validation runs</li>
              <li>SHA-256 checksums for every exported file</li>
            </ul>
          </div>
          <div className="ledger-col">
            <h3 className="ledger-title">
              <span className="basis basis-advisory">Advisory</span>
            </h3>
            <ul className="ledger-list">
              <li>Upgrade explanations from a local model, when one is running</li>
              <li>Notes on likely breaking changes</li>
              <li>Rule-based recommendations when no model is available</li>
            </ul>
            <p className="ledger-note">
              A suggested version that does not exist in the registry is rejected, not shown.
            </p>
          </div>
        </div>
      </section>

      <section id="isolation" className="band band-split" aria-labelledby="isolation-title">
        <div className="band-head">
          <h2 id="isolation-title" className="band-title">Validation that cannot reach your machine</h2>
          <p className="band-lede">
            Checks run inside a locked-down Docker container. If Docker is not available, the result
            is reported as not runnable. It is never reported as a pass.
          </p>
        </div>
        <dl className="spec">
          <div><dt>Network</dt><dd className="mono">none</dd></div>
          <div><dt>User</dt><dd className="mono">1000:1000, non-root</dd></div>
          <div><dt>Capabilities</dt><dd className="mono">all dropped</dd></div>
          <div><dt>Root filesystem</dt><dd className="mono">read-only</dd></div>
          <div><dt>Limits</dt><dd className="mono">512 MB, 1 CPU, 64 processes</dd></div>
          <div><dt>Images</dt><dd className="mono">pinned by digest</dd></div>
        </dl>
      </section>

      <section className="closing" aria-labelledby="closing-title">
        <h2 id="closing-title" className="closing-title">Start with a repository you already have.</h2>
        <a href="#/analyze" className="btn btn-primary btn-lg">
          Analyze a repository
        </a>
      </section>
    </main>

    <footer className="site-footer">
      <div className="footer-brand">
        <BrandMark size={18} />
        <span>Dependency Upgrade Assistant</span>
      </div>
      <p className="footer-note">
        Runs on your machine. Code and manifests are not sent to hosted AI services.
      </p>
    </footer>
  </div>
);
