import { Component, type ErrorInfo, type ReactNode } from 'react';

interface Props {
  children: ReactNode;
  /**
   * When this value changes, a tripped boundary resets itself. Pass the current
   * route (e.g. location.pathname) so navigating away from a crashed view
   * recovers automatically instead of leaving the user stuck on the fallback.
   */
  resetKey?: string | number;
  /** Optional short label describing the scope ("this view", "the app"). */
  scopeLabel?: string;
}

interface State {
  error: Error | null;
}

/**
 * Catches render/lifecycle errors in its subtree and shows a recoverable
 * fallback instead of unmounting the whole app (a single bad date or a
 * malformed sidecar payload should never white-screen the window).
 *
 * Privacy: errors are logged to the local console only — never sent anywhere.
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidUpdate(prev: Props): void {
    if (this.state.error && prev.resetKey !== this.props.resetKey) {
      this.setState({ error: null });
    }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Local-only. No telemetry, ever.
    console.error('[ErrorBoundary]', error, info.componentStack);
  }

  private handleReset = (): void => this.setState({ error: null });

  render(): ReactNode {
    const { error } = this.state;
    if (!error) return this.props.children;

    const scope = this.props.scopeLabel ?? 'this view';
    return (
      <div
        role="alert"
        className="flex h-full w-full flex-col items-center justify-center gap-5 bg-background p-8 text-center"
      >
        <div className="max-w-md space-y-2">
          <p className="font-serif text-lg font-light text-foreground">
            Something went wrong in {scope}.
          </p>
          <p className="font-sans text-sm leading-relaxed text-muted-foreground">
            The rest of the app is still running. Try again, or switch to another
            view from the sidebar.
          </p>
        </div>

        <button
          type="button"
          onClick={this.handleReset}
          className="rounded-md border border-border px-4 py-2 font-sans text-sm text-foreground transition-colors hover:bg-surface-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
        >
          Try again
        </button>

        <details className="max-w-md text-left">
          <summary className="cursor-pointer font-sans text-xs text-muted-foreground/70">
            Technical details
          </summary>
          <pre className="mt-2 overflow-auto whitespace-pre-wrap break-words rounded-md bg-surface-2 p-3 font-mono text-[11px] text-muted-foreground">
            {error.message}
          </pre>
        </details>
      </div>
    );
  }
}
