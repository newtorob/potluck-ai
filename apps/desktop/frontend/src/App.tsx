import { Navigate, Route, Routes } from 'react-router-dom';
import { TitleBar } from '@/components/layout/TitleBar';
import { Sidebar } from '@/components/layout/Sidebar';
import { AuthGate } from '@/components/auth/AuthGate';
import { ChatView } from '@/views/ChatView';
import { ModelLibrary } from '@/views/ModelLibrary';
import { SettingsView } from '@/views/SettingsView';
import { MembershipView } from '@/views/MembershipView';

/**
 * Root application shell.
 *
 * Layout: TitleBar (Mac drag region) + Sidebar (fixed left) + main content area.
 * AuthGate is a pass-through for now; real auth enforcement comes later.
 *
 * Routes:
 *   /chat        — streaming chat interface
 *   /models      — model library: browse, install, delete
 *   /settings    — hardware, privacy, account, advanced tabs
 *   /membership  — founding member status + billing
 */
export default function App() {
  return (
    <div className="flex h-screen w-screen flex-col overflow-hidden bg-background text-foreground">
      {/* Mac-only drag region — zero height on other platforms */}
      <TitleBar />

      <div className="flex flex-1 overflow-hidden">
        <AuthGate>
          <Sidebar />

          <main className="flex-1 overflow-auto">
            <Routes>
              <Route path="/" element={<Navigate to="/chat" replace />} />
              <Route path="/chat" element={<ChatView />} />
              <Route path="/models" element={<ModelLibrary />} />
              <Route path="/settings" element={<SettingsView />} />
              <Route path="/membership" element={<MembershipView />} />
              {/* Catch-all: redirect unknown paths to chat */}
              <Route path="*" element={<Navigate to="/chat" replace />} />
            </Routes>
          </main>
        </AuthGate>
      </div>
    </div>
  );
}
