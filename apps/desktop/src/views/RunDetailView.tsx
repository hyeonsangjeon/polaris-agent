import { useState } from "react";
import type { PolarisClient } from "../api/client";
import type { Replay, Run } from "../api/types";
import { ErrorState, LoadingState } from "../components/StatePanel";
import { StatusBadge } from "../components/StatusBadge";
import { useRunBundle } from "../hooks/useRunBundle";
import {
  actualModels,
  budgetPercent,
  compact,
  modeLabel,
  relativeTime,
  runTitle,
  shortId,
  usd,
} from "../utils/format";

export function RunDetailView({
  client,
  runId,
  onEvidence,
}: {
  client: PolarisClient;
  runId: string;
  onEvidence: () => void;
}) {
  const { data, loading, error, refresh } = useRunBundle(client, runId);
  const [action, setAction] = useState("");
  if (loading && !data) return <LoadingState label="Materializing durable run" />;
  if (error && !data) return <ErrorState error={error} onRetry={refresh} />;
  if (!data) return null;
  const { run, replay, timeline, artifacts } = data;
  const models = actualModels(replay);

  async function runAction(kind: "resume" | "cancel") {
    setAction(kind);
    try {
      await client[kind](run.id);
      refresh();
    } finally {
      setAction("");
    }
  }

  return (
    <div className="view">
      <div className="run-detail-heading">
        <div>
          <div className="run-heading-meta">
            <StatusBadge status={run.status} />
            <span>{modeLabel(run.mode)}</span>
            <code>{shortId(run.id)}</code>
          </div>
          <h1>{runTitle(run)}</h1>
          <p>Created {relativeTime(run.created_at)} · durable journal updated {relativeTime(run.updated_at)}</p>
        </div>
        <div className="run-actions">
          {run.status === "paused" && (
            <button
              className="button primary"
              type="button"
              disabled={!!action}
              onClick={() => void runAction("resume")}
            >
              {action === "resume" ? "Resuming…" : "Resume"}
            </button>
          )}
          {!["completed", "failed", "cancelled"].includes(run.status) && (
            <button
              className="button danger"
              type="button"
              disabled={!!action}
              onClick={() => void runAction("cancel")}
            >
              Cancel
            </button>
          )}
          <button className="button secondary" type="button" onClick={() => refresh()}>
            Replay record
          </button>
        </div>
      </div>

      <section className="run-facts" aria-label="Run facts">
        <Fact label="Requested model" value={requestedModel(run)} />
        <Fact
          label="Actual model"
          value={models.length ? models.join(", ") : "Pending provider response"}
          highlight={models.length > 0}
        />
        <Fact
          label="Token budget"
          value={`${compact(run.budget.used_tokens)} / ${compact(run.budget.token_limit)}`}
        />
        <Fact label="Recorded cost" value={usd(run.budget.used_micro_usd ?? replay?.cost?.micro_usd)} />
      </section>

      <div className="detail-grid">
        <div className="detail-main">
          <section className="section-block">
            <div className="section-heading">
              <div>
                <h2>Execution timeline</h2>
                <p>{timeline.length} durable events</p>
              </div>
              <span className="live-label">
                <i aria-hidden="true" /> {run.status === "running" ? "Polling" : "Recorded"}
              </span>
            </div>
            <ol className="timeline">
              {timeline.map((event, index) => (
                <li key={event.id} className={index === timeline.length - 1 ? "current" : ""}>
                  <span className="timeline-node" aria-hidden="true" />
                  <div>
                    <strong>{event.type.replace(/\./g, " ")}</strong>
                    <p>{eventSummary(event.payload)}</p>
                  </div>
                  <time>{relativeTime(event.created_at)}</time>
                </li>
              ))}
            </ol>
          </section>

          <WorkerGraph replay={replay} run={run} />
        </div>
        <aside className="detail-aside">
          <section className="budget-panel">
            <h2>Budget envelope</h2>
            <BudgetMeter
              label="Calls"
              value={`${run.budget.used_calls ?? 0} / ${run.budget.call_limit ?? "∞"}`}
              percent={budgetPercent(run.budget, "used_calls", "call_limit")}
            />
            <BudgetMeter
              label="Tokens"
              value={`${compact(run.budget.used_tokens)} / ${compact(run.budget.token_limit)}`}
              percent={budgetPercent(run.budget, "used_tokens", "token_limit")}
            />
            <BudgetMeter
              label="Cost"
              value={`${usd(run.budget.used_micro_usd)} / ${usd(run.budget.micro_usd_limit)}`}
              percent={budgetPercent(run.budget, "used_micro_usd", "micro_usd_limit")}
            />
          </section>
          <section className="artifact-panel">
            <div className="section-heading">
              <div>
                <h2>Artifacts</h2>
                <p>Content-addressed</p>
              </div>
            </div>
            {artifacts.map((artifact) => (
              <div className="artifact-row" key={artifact.id}>
                <span aria-hidden="true">□</span>
                <div>
                  <strong>{artifact.name}</strong>
                  <small>
                    {artifact.size_bytes ? `${compact(artifact.size_bytes)} bytes` : "Recorded"} ·{" "}
                    {artifact.sha256 ?? "hash pending"}
                  </small>
                </div>
              </div>
            ))}
            {!artifacts.length && <p className="muted-copy">Artifacts appear after committed steps.</p>}
          </section>
          {replay?.claims && replay.claims.length > 0 && (
            <button className="evidence-jump" type="button" onClick={onEvidence}>
              <span>
                <strong>Evidence ledger</strong>
                <small>{replay.claims.length} normalized claims</small>
              </span>
              <b aria-hidden="true">→</b>
            </button>
          )}
        </aside>
      </div>
    </div>
  );
}

function Fact({ label, value, highlight = false }: { label: string; value: string; highlight?: boolean }) {
  return (
    <div className={highlight ? "highlight" : ""}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function BudgetMeter({ label, value, percent }: { label: string; value: string; percent: number }) {
  return (
    <div className="budget-meter">
      <div>
        <span>{label}</span>
        <strong>{value}</strong>
      </div>
      <progress max="100" value={percent}>
        {percent}%
      </progress>
    </div>
  );
}

function WorkerGraph({ replay, run }: { replay: Replay | null; run: Run }) {
  const workers = replay?.workers ?? [];
  const configured = Array.isArray(run.request.workers) ? run.request.workers : [];
  if (!workers.length && !configured.length && run.mode !== "foundry-router") return null;
  return (
    <section className="section-block worker-section">
      <div className="section-heading">
        <div>
          <h2>{run.mode === "foundry-router" ? "Routing record" : "Worker graph"}</h2>
          <p>Requested and actual provider execution</p>
        </div>
      </div>
      {run.mode === "foundry-router" ? (
        <div className="route-graph">
          <span>Foundry router</span>
          <i aria-hidden="true">→</i>
          <span className="selected-model">
            {actualModels(replay).join(", ") || "Selection pending"}
          </span>
          <i aria-hidden="true">→</i>
          <span>Durable replay</span>
        </div>
      ) : (
        <div className="worker-cards">
          {workers.map((worker) => (
            <div className="worker-card" key={worker.worker_id}>
              <span className="worker-avatar">{worker.worker_id.slice(0, 2).toUpperCase()}</span>
              <div>
                <strong>{worker.worker_id}</strong>
                <small>
                  {worker.requested_model} → {worker.actual_models.join(", ")}
                </small>
              </div>
              <b>{compact(worker.prompt_tokens + worker.completion_tokens)} tok</b>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function requestedModel(run: Run) {
  return String(run.request.provider ?? run.config.provider ?? (run.mode === "fan-out" ? "Per worker" : "Auto"));
}

function eventSummary(payload: Record<string, unknown>) {
  const entries = Object.entries(payload).slice(0, 3);
  if (!entries.length) return "Event committed to the run journal.";
  return entries.map(([key, value]) => `${key.replace(/_/g, " ")}: ${String(value)}`).join(" · ");
}
