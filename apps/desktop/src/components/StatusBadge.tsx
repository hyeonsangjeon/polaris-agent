import type { RunStatus } from "../api/types";

export function StatusBadge({ status }: { status: RunStatus | string }) {
  return (
    <span className={`status-badge status-${status}`} aria-label={`Status: ${status}`}>
      <span aria-hidden="true" />
      {status}
    </span>
  );
}
