import type { ReactNode } from "react";

export function LoadingState({ label = "Loading operational data" }: { label?: string }) {
  return (
    <div className="state-panel" role="status">
      <div className="skeleton-lines" aria-hidden="true">
        <i />
        <i />
        <i />
      </div>
      <p>{label}…</p>
    </div>
  );
}

export function ErrorState({
  error,
  onRetry,
}: {
  error: Error;
  onRetry?: () => void;
}) {
  return (
    <div className="state-panel error-panel" role="alert">
      <span className="state-symbol" aria-hidden="true">
        !
      </span>
      <div>
        <strong>Unable to reach Polaris</strong>
        <p>{error.message || "Check the daemon connection and try again."}</p>
      </div>
      {onRetry && (
        <button className="button secondary" type="button" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  );
}

export function EmptyState({
  title,
  children,
  action,
}: {
  title: string;
  children: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="state-panel empty-panel">
      <span className="state-symbol" aria-hidden="true">
        ◇
      </span>
      <div>
        <strong>{title}</strong>
        <p>{children}</p>
      </div>
      {action}
    </div>
  );
}
