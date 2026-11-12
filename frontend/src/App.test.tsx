import { act, render, screen } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { App } from './App';
import * as apiClient from './api/client';

const INTERNAL_TERMS = /milestone|AI-DLC|\bgate\b|\bM\d\b|dua_local\.db|phase \d/i;

describe('App Root Component', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    window.location.hash = '';
    vi.spyOn(apiClient, 'getHealth').mockReturnValue(new Promise(() => {}));
    vi.spyOn(apiClient, 'getAiStatus').mockReturnValue(new Promise(() => {}));
  });

  it('renders the product landing page with working entry points', () => {
    render(<App />);

    expect(screen.getByTestId('landing')).toBeInTheDocument();
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent(/Dependency upgrades, verified before they merge/i);
    expect(screen.getByTestId('hero-cta')).toHaveAttribute('href', '#/analyze');
    for (const link of screen.getAllByRole('link', { name: 'How it works' })) {
      expect(link).toHaveAttribute('href', '#how-it-works');
    }
    expect(document.getElementById('how-it-works')).not.toBeNull();
  });

  it('opens the analyzer when the route changes', async () => {
    render(<App />);
    await act(async () => {
      window.location.hash = '#/analyze';
      window.dispatchEvent(new HashChangeEvent('hashchange'));
    });

    expect(screen.getByTestId('app-view')).toBeInTheDocument();
    expect(screen.getByRole('heading', { level: 1, name: 'Analyze dependencies' })).toBeInTheDocument();
  });

  it('shows no internal development information to users', async () => {
    const { container } = render(<App />);
    expect(container.textContent).not.toMatch(INTERNAL_TERMS);

    await act(async () => {
      window.location.hash = '#/analyze';
      window.dispatchEvent(new HashChangeEvent('hashchange'));
    });
    expect(container.textContent).not.toMatch(INTERNAL_TERMS);
  });
});
