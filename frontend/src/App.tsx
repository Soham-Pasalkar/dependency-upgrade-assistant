import React, { useEffect, useState } from 'react';
import { Landing } from './components/Landing';
import { SiteHeader } from './components/SiteHeader';
import { ManifestAnalyzer } from './components/ManifestAnalyzer';
import './App.css';

type View = 'home' | 'analyze';

const readView = (): View =>
  typeof window !== 'undefined' && window.location.hash.startsWith('#/analyze') ? 'analyze' : 'home';

export const App: React.FC = () => {
  const [view, setView] = useState<View>(readView);

  useEffect(() => {
    const onHashChange = () => {
      const next = readView();
      setView((prev) => {
        if (prev !== next) window.scrollTo({ top: 0 });
        return next;
      });
    };
    window.addEventListener('hashchange', onHashChange);
    return () => window.removeEventListener('hashchange', onHashChange);
  }, []);

  useEffect(() => {
    document.title =
      view === 'analyze' ? 'Analyze dependencies | Dependency Upgrade Assistant' : 'Dependency Upgrade Assistant';
  }, [view]);

  return (
    <div className="view" key={view}>
      {view === 'home' ? (
        <Landing />
      ) : (
        <div className="app" data-testid="app-view">
          <SiteHeader variant="app" />
          <ManifestAnalyzer />
        </div>
      )}
    </div>
  );
};

export default App;
