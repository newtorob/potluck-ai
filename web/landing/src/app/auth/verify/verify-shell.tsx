import type { ReactNode } from 'react';

// Shared frame for the sign-in bounce page, so the Suspense fallback and every
// client state render identically. Pure presentation — safe in server or client.
export function VerifyShell({
  heading,
  children,
}: {
  heading: string;
  children?: ReactNode;
}) {
  return (
    <main className="flex min-h-screen w-full items-center justify-center bg-background px-6 py-16">
      <div className="flex w-full max-w-sm flex-col items-center gap-6 text-center">
        <div className="relative h-16 w-16">
          <div
            className="absolute inset-0 rounded-2xl bg-primary/15 blur-xl"
            aria-hidden="true"
          />
          <div className="relative flex h-16 w-16 items-center justify-center rounded-2xl bg-primary/10 ring-1 ring-primary/20">
            <span className="font-serif text-2xl text-primary">P</span>
          </div>
        </div>
        <h1 className="font-serif text-2xl tracking-display text-foreground">{heading}</h1>
        {children}
      </div>
    </main>
  );
}
