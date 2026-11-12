import React, { useEffect, useRef } from 'react';

export interface RailStep {
  id: string;
  label: string;
  state: 'done' | 'current' | 'locked';
}

interface StepRailProps {
  steps: RailStep[];
  onNavigate: (id: string) => void;
}

const STATE_TEXT = { done: 'complete', current: 'in progress', locked: 'not yet available' } as const;

/** Sticky progress rail: shows where the user is in the journey and jumps to available stages. */
export const StepRail: React.FC<StepRailProps> = ({ steps, onNavigate }) => {
  const currentIndex = steps.findIndex((s) => s.state === 'current');
  const navRef = useRef<HTMLElement>(null);

  // On narrow screens the rail scrolls horizontally: keep the current step in view.
  useEffect(() => {
    const nav = navRef.current;
    if (!nav || nav.scrollWidth <= nav.clientWidth) return;
    const item = nav.querySelectorAll<HTMLElement>('.rail-item')[currentIndex];
    if (item) nav.scrollTo({ left: Math.max(0, item.offsetLeft - 16), behavior: 'smooth' });
  }, [currentIndex]);

  return (
    <nav className="rail" aria-label="Progress" ref={navRef}>
      <ol className="rail-list">
        {steps.map((step, i) => (
          <li key={step.id} className={`rail-item is-${step.state}`}>
            <button
              type="button"
              className="rail-button"
              onClick={() => onNavigate(step.id)}
              disabled={step.state === 'locked'}
              aria-current={i === currentIndex ? 'step' : undefined}
            >
              <span className="rail-marker" aria-hidden="true" />
              <span className="rail-label">{step.label}</span>
              <span className="visually-hidden">, {STATE_TEXT[step.state]}</span>
            </button>
          </li>
        ))}
      </ol>
    </nav>
  );
};
