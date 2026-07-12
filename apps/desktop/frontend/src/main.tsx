import React from 'react';
import ReactDOM from 'react-dom/client';
// HashRouter (not BrowserRouter): the packaged app loads from file:// via
// loadFile(). BrowserRouter puts the route in the URL PATH (file:///…/chat), so
// a reload (Cmd+R) makes Chromium request that path as a file, it doesn't exist,
// and the window goes blank/white. HashRouter keeps the route in the fragment
// (file:///…/index.html#/chat), so a reload always re-loads the real index.html
// and the router restores the view. useLocation().pathname is unchanged.
import { HashRouter } from 'react-router-dom';
import App from './App';
import { ErrorBoundary } from '@/components/ErrorBoundary';
import './styles/globals.css';

const rootElement = document.getElementById('root');
if (!rootElement) {
  throw new Error('Root element #root not found. Check index.html.');
}

ReactDOM.createRoot(rootElement).render(
  <React.StrictMode>
    <HashRouter>
      <ErrorBoundary scopeLabel="the app">
        <App />
      </ErrorBoundary>
    </HashRouter>
  </React.StrictMode>,
);
