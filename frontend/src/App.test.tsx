import { render, screen } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import { App } from './App';
import * as apiClient from './api/client';

describe('App Root Component', () => {
  it('renders application header and branding', () => {
    vi.spyOn(apiClient, 'getHealth').mockReturnValue(new Promise(() => {}));

    render(<App />);

    expect(screen.getByText('DUA 2.0')).toBeInTheDocument();
    expect(screen.getByText('Dependency Upgrade Assistant')).toBeInTheDocument();
    expect(screen.getAllByText(/Milestone M1 Foundation/i).length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText(/System Architecture Status/i)).toBeInTheDocument();
  });
});
