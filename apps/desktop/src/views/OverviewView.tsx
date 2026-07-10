import type { PolarisClient } from "../api/client";
import type { Approval, ProviderHealth } from "../api/types";
import { RunTable } from "../components/RunTable";
import { ErrorState, LoadingState } from "../components/StatePanel";
import { usePolling } from "../hooks/usePolling";
import { compact } from "../utils/format";

export function OverviewView({
  client,
  providers,
  onSelectRun,
  onNewRun,
  pendingApprovals,
}: {
  client: PolarisClient;
  providers: ProviderHealth[];
  onSelectRun: (id: string) => void;
  onNewRun: () => void;
  pendingApprovals: Approval[];
}) {
  const { data: runs, loading, error, refresh } = usePolling(() => client.runs(), {
    intervalMs: 4000,
  });
  if (loading && !runs) return <LoadingState label="Loading control plane" />;
  if (error && !runs) return <ErrorState error={error} onRetry={refresh} />;

  const data = runs ?? [];
  const active = data.filter((run) => ["created", "running"].includes(run.status)).length;
  const paused = data.filter((run) => run.status === "paused").length;
  const uncertain = new Set(
    pendingApprovals
      .filter((approval) => approval.kind === "uncertain_outcome")
      .map((approval) => approval.run_id),
  ).size;
  const completed = data.filter((run) => run.status === "completed").length;

  return (
    <div className="view">
      <div className="view-heading overview-heading">
        <div>
          <p className="context-line">Control plane / now</p>
          <h1>Operational overview</h1>
          <p>Durable agent work, provider readiness, and decisions that need an operator.</p>
        </div>
        <button className="button primary" type="button" onClick={onNewRun}>
          Start a run
        </button>
      </div>

      <section className="metric-ribbon" aria-label="Run summary">
        <Metric label="Active" value={active} tone="active" note="executing or queued" />
        <Metric label="Paused" value={paused} tone="paused" note="awaiting intervention" />
        <Metric label="Uncertain" value={uncertain} tone="uncertain" note="side effect unresolved" />
        <Metric label="Completed" value={completed} tone="complete" note="durably recorded" />
      </section>

      <div className="overview-grid">
        <section className="section-block recent-runs">
          <div className="section-heading">
            <div>
              <h2>Recent runs</h2>
              <p>{compact(data.length)} journaled runs visible to this daemon</p>
            </div>
            <button className="text-button" type="button" onClick={refresh}>
              Refresh
            </button>
          </div>
          <RunTable runs={data.slice(0, 6)} onSelect={onSelectRun} />
        </section>
        <aside className="provider-panel">
          <div className="section-heading">
            <div>
              <h2>Model readiness</h2>
              <p>Provider doctor</p>
            </div>
          </div>
          <div className="provider-list">
            {providers.length ? (
              providers.map((provider) => (
                <div className="provider-row" key={provider.name}>
                  <span className={`provider-monogram ${provider.status}`}>
                    {provider.name.slice(0, 2).toUpperCase()}
                  </span>
                  <div>
                    <strong>{provider.name}</strong>
                    <small>{provider.model ?? provider.detail ?? "Configured"}</small>
                  </div>
                  <span className={`health-label ${provider.status}`}>{provider.status}</span>
                </div>
              ))
            ) : (
              <p className="muted-copy">No configured providers reported.</p>
            )}
          </div>
          <div className="provider-note">
            <span aria-hidden="true">i</span>
            Health reflects configuration checks, not a live model completion.
          </div>
        </aside>
      </div>
    </div>
  );
}

function Metric({
  label,
  value,
  tone,
  note,
}: {
  label: string;
  value: number;
  tone: string;
  note: string;
}) {
  return (
    <div className={`metric metric-${tone}`}>
      <div>
        <span>{label}</span>
        <strong>{value}</strong>
      </div>
      <small>{note}</small>
    </div>
  );
}
