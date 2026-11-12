import React from 'react';
import { BackendStatus } from './BackendStatus';

export const BrandMark: React.FC<{ size?: number }> = ({ size = 22 }) => (
  <svg width={size} height={size} viewBox="0 0 32 32" aria-hidden="true" className="brand-mark">
    <path d="M5 25h8v-8h8V9h6" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="square" />
    <circle cx="26" cy="9" r="2.4" className="brand-mark-dot" />
  </svg>
);

interface SiteHeaderProps {
  variant: 'home' | 'app';
}

export const SiteHeader: React.FC<SiteHeaderProps> = ({ variant }) => (
  <header className="site-header">
    <div className="site-header-frame">
      <a href="#/" className="brand" aria-label="Dependency Upgrade Assistant home">
        <BrandMark />
        <span className="brand-name">
          Dependency Upgrade <span className="brand-name-tail">Assistant</span>
        </span>
      </a>

      <div className="header-ruler" aria-hidden="true" />

      {variant === 'home' ? (
        <>
          <nav className="site-nav" aria-label="Sections">
            <a href="#how-it-works">How it works</a>
            <a href="#evidence">Evidence</a>
            <a href="#isolation">Isolation</a>
          </nav>
          <a href="#/analyze" className="btn btn-bracket" data-testid="header-cta">
            Open analyzer
          </a>
        </>
      ) : (
        <>
          <BackendStatus />
          <a href="#/" className="btn btn-quiet header-back">
            Overview
          </a>
        </>
      )}
    </div>
  </header>
);
