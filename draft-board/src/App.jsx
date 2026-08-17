import React from "react";

import DraftBoard from "./DraftBoard";

/**
 * Catches a render crash and shows something recoverable.
 *
 * Without it React unmounts the whole tree and leaves a white page — which
 * during a live draft is indistinguishable from the tool being dead, at the
 * one moment there's no time to debug it. The session lives on the server, so
 * reloading genuinely does recover the board, picks and all.
 */
class ErrorBoundary extends React.Component {
  state = { error: null };

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    // Kept in the console for afterwards; the user gets the sentence above.
    console.error("Draft board crashed:", error, info?.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="flex min-h-screen items-center justify-center p-6">
        <div className="card w-full max-w-md p-7 text-center">
          <h1 className="text-lg font-semibold text-slate-100">Something broke on this screen</h1>
          <p className="mt-2 text-sm leading-relaxed text-slate-400">
            Your draft is safe — picks live on the server, not in this page, so reloading picks up
            exactly where you were.
          </p>
          <button
            onClick={() => window.location.reload()}
            className="mt-5 h-10 w-full rounded-lg bg-emerald-500 text-sm font-semibold text-slate-950
              transition hover:bg-emerald-400"
          >
            Reload the board
          </button>
          <pre className="mt-4 max-h-32 overflow-auto rounded-lg bg-black/30 p-3 text-left text-[11px]
            leading-relaxed text-slate-500 scroll-slim">
            {String(this.state.error?.message || this.state.error)}
          </pre>
        </div>
      </div>
    );
  }
}

export default function App() {
  return (
    <ErrorBoundary>
      <DraftBoard />
    </ErrorBoundary>
  );
}
