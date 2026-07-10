import { useState } from "react";
import type { PolarisClient } from "../api/client";
import type { Approval } from "../api/types";
import { ErrorState, LoadingState } from "../components/StatePanel";
import { loadPendingApprovals } from "../hooks/useRunBundle";
import { usePolling } from "../hooks/usePolling";
import { relativeTime, shortId } from "../utils/format";

export function ApprovalsView({
  client,
  onSelectRun,
}: {
  client: PolarisClient;
  onSelectRun: (id: string) => void;
}) {
  const { data, loading, error, refresh } = usePolling(() => loadPendingApprovals(client), {
    intervalMs: 3500,
  });
  const [deciding, setDeciding] = useState("");
  if (loading && !data) return <LoadingState label="Loading decision queue" />;
  if (error && !data) return <ErrorState error={error} onRetry={refresh} />;
  const approvals = data ?? [];

  async function decide(approval: Approval, approved: boolean) {
    setDeciding(approval.id);
    try {
      await client.decide(approval.id, approved);
      refresh();
    } finally {
      setDeciding("");
    }
  }

  return (
    <div className="view narrow-view approvals-view">
      <div className="view-heading">
        <div>
          <p className="context-line">Human decision boundary</p>
          <h1>Approvals</h1>
          <p>Review tool intent and uncertain outcomes before durable execution continues.</p>
        </div>
        <span className="queue-count">{approvals.length} pending</span>
      </div>
      <div className="approval-list">
        {approvals.map((approval) => {
          const uncertain = approval.kind === "uncertain_outcome";
          return (
            <article className={`approval ${uncertain ? "uncertain" : ""}`} key={approval.id}>
              <div className="approval-icon" aria-hidden="true">
                {uncertain ? "?" : "⌁"}
              </div>
              <div className="approval-content">
                <div className="approval-meta">
                  <span>{uncertain ? "Uncertain side effect" : "Tool permission"}</span>
                  <button type="button" onClick={() => onSelectRun(approval.run_id)}>
                    {shortId(approval.run_id)}
                  </button>
                  <time>{relativeTime(approval.created_at)}</time>
                </div>
                <h2>{approvalTitle(approval)}</h2>
                <p>{approvalDescription(approval)}</p>
                <div className="approval-request">
                  <h3>Complete request</h3>
                  <pre>{JSON.stringify(approval.request, null, 2)}</pre>
                </div>
              </div>
              <div className="approval-actions">
                <button
                  className="button secondary"
                  type="button"
                  disabled={!!deciding}
                  onClick={() => void decide(approval, false)}
                >
                  {uncertain ? "Deny retry" : "Deny"}
                </button>
                <button
                  className="button primary"
                  type="button"
                  disabled={!!deciding}
                  onClick={() => void decide(approval, true)}
                >
                  {deciding === approval.id ? "Recording…" : uncertain ? "Retry operation" : "Approve"}
                </button>
              </div>
            </article>
          );
        })}
        {!approvals.length && (
          <div className="state-panel empty-panel">
            <span className="state-symbol" aria-hidden="true">
              ✓
            </span>
            <div>
              <strong>Decision queue clear</strong>
              <p>Polaris will place tool approvals and unresolved side effects here.</p>
            </div>
          </div>
        )}
      </div>
      <p className="approval-footnote">
        Decisions are attributed to <code>desktop-operator</code> and appended to the durable journal.
      </p>
    </div>
  );
}

function approvalTitle(approval: Approval) {
  if (approval.kind === "uncertain_outcome") return "Retry may duplicate an external side effect";
  return `Allow ${String(approval.request.tool ?? "tool execution")}`;
}

function approvalDescription(approval: Approval) {
  if (approval.kind === "uncertain_outcome") {
    const reason = String(
      approval.request.uncertainty_reason ??
        "The connection ended before Polaris could confirm the external side effect.",
    );
    return `${reason} Inspect the target system before deciding. Approval explicitly retries the operation and may duplicate the external side effect.`;
  }
  return "This step crosses the configured tool approval boundary and will resume only after a decision.";
}
