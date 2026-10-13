// Three-layer architecture diagram — SVG-based, pure CSS, no JS dependency.
// Layers from bottom to top: Compute mesh → Memory layer → Your AI.
// Designed to work in both light and dark themes via CSS custom properties.

export function LayerDiagram() {
  return (
    <div className="relative w-full overflow-hidden rounded-xl border border-border bg-surface-2 px-6 py-8" aria-hidden="true">
      {/* Stack of three layers */}
      <div className="flex flex-col gap-0 max-w-lg mx-auto">
        {/* Layer 3 — top: Your AI (the interface) */}
        <div className="relative rounded-t-lg border border-border bg-background px-5 py-4 shadow-sm">
          <div className="flex items-center gap-3">
            <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-primary/15">
              <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
                <rect x="2" y="2" width="12" height="9" rx="1.5" stroke="hsl(var(--primary))" strokeWidth="1.25"/>
                <path d="M5 14h6M8 11v3" stroke="hsl(var(--primary))" strokeWidth="1.25" strokeLinecap="round"/>
              </svg>
            </div>
            <div>
              <p className="text-sm font-medium text-foreground">Your AI — on your device</p>
              <p className="text-xs text-muted-foreground">Local models. No prompt logging. Stays on your machine.</p>
            </div>
          </div>
        </div>

        {/* Connector arrows */}
        <div className="flex justify-center py-0.5">
          <svg width="2" height="20" viewBox="0 0 2 20">
            <line x1="1" y1="0" x2="1" y2="20" stroke="hsl(var(--border))" strokeWidth="1.5" strokeDasharray="3 2"/>
          </svg>
        </div>

        {/* Layer 2 — middle: Memory layer (MCP) */}
        <div className="relative border border-border bg-card px-5 py-4 shadow-sm">
          <div className="flex items-center gap-3">
            <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-sage/15">
              <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
                <path d="M3 4h10M3 8h7M3 12h5" stroke="hsl(var(--sage))" strokeWidth="1.25" strokeLinecap="round"/>
                <circle cx="13" cy="11" r="2.5" stroke="hsl(var(--sage))" strokeWidth="1.25"/>
              </svg>
            </div>
            <div>
              <p className="text-sm font-medium text-foreground">Persistent memory — MCP layer</p>
              <p className="text-xs text-muted-foreground">Facts, decisions, tasks. Follows you across every agent and session.</p>
            </div>
          </div>
        </div>

        <div className="flex justify-center py-0.5">
          <svg width="2" height="20" viewBox="0 0 2 20">
            <line x1="1" y1="0" x2="1" y2="20" stroke="hsl(var(--border))" strokeWidth="1.5" strokeDasharray="3 2"/>
          </svg>
        </div>

        {/* Layer 1 — bottom: Shared compute mesh */}
        <div className="relative rounded-b-lg border border-border bg-surface-2 px-5 py-4 shadow-sm">
          <div className="flex items-center gap-3">
            <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-primary/10">
              {/* Simple mesh / network icon */}
              <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
                <circle cx="8" cy="8" r="2" stroke="hsl(var(--primary))" strokeWidth="1.25"/>
                <circle cx="2.5" cy="4" r="1.5" stroke="hsl(var(--primary))" strokeWidth="1.25"/>
                <circle cx="13.5" cy="4" r="1.5" stroke="hsl(var(--primary))" strokeWidth="1.25"/>
                <circle cx="2.5" cy="12" r="1.5" stroke="hsl(var(--primary))" strokeWidth="1.25"/>
                <circle cx="13.5" cy="12" r="1.5" stroke="hsl(var(--primary))" strokeWidth="1.25"/>
                <path d="M4 4.5L6.2 6.5M9.8 9.5L12 11.5M4 11.5L6.2 9.5M9.8 6.5L12 4.5" stroke="hsl(var(--primary))" strokeWidth="1.25" strokeLinecap="round" opacity="0.6"/>
              </svg>
            </div>
            <div>
              <p className="text-sm font-medium text-foreground">Shared compute — WireGuard mesh</p>
              <p className="text-xs text-muted-foreground">Members share idle compute. Blinded routing. No identity leaks between layers.</p>
            </div>
          </div>
        </div>
      </div>

      {/* Privacy annotation */}
      <p className="mt-6 text-center text-xs text-muted-foreground/70">
        Each layer is independently auditable. The mesh encrypts traffic. The inference layer strips your identity before dispatch.
      </p>
    </div>
  );
}
