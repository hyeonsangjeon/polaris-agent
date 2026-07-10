import { useMemo, useState } from "react";
import type { PolarisClient } from "../api/client";
import type { ClaimStatus } from "../api/types";
import { ErrorState, LoadingState } from "../components/StatePanel";
import { useRunBundle } from "../hooks/useRunBundle";

const groups: Array<{ id: ClaimStatus; label: string }> = [
  { id: "consensus", label: "Consensus" },
  { id: "disputed", label: "Disputed" },
  { id: "unsupported", label: "Unsupported" },
];

export function EvidenceView({ client, runId }: { client: PolarisClient; runId: string }) {
  const { data, loading, error, refresh } = useRunBundle(client, runId);
  const [filter, setFilter] = useState<ClaimStatus>("consensus");
  const claims = data?.replay?.claims ?? [];
  const visible = useMemo(() => claims.filter((item) => item.status === filter), [claims, filter]);
  if (loading && !data) return <LoadingState label="Loading evidence ledger" />;
  if (error && !data) return <ErrorState error={error} onRetry={refresh} />;

  return (
    <div className="view">
      <div className="view-heading">
        <div>
          <p className="context-line">Normalized research ledger</p>
          <h1>Evidence</h1>
          <p>Inspect what workers agree on, where they dissent, and which claims lack support.</p>
        </div>
      </div>
      <div className="evidence-layout">
        <section>
          <div className="claim-tabs" role="tablist" aria-label="Claim status">
            {groups.map((group) => (
              <button
                key={group.id}
                role="tab"
                type="button"
                aria-selected={filter === group.id}
                onClick={() => setFilter(group.id)}
              >
                {group.label}
                <span>{claims.filter((item) => item.status === group.id).length}</span>
              </button>
            ))}
          </div>
          <div className="claim-list">
            {visible.map((claim) => (
              <article className={`claim claim-${claim.status}`} key={claim.id}>
                <div className="claim-meta">
                  <code>{claim.id}</code>
                  <span>{Math.round(claim.confidence * 100)}% confidence</span>
                </div>
                <h2>{claim.statement}</h2>
                <div className="claim-evidence">
                  <span>Evidence</span>
                  {claim.evidence_ids.length ? (
                    claim.evidence_ids.map((id) => <code key={id}>{id}</code>)
                  ) : (
                    <em>No cited evidence</em>
                  )}
                </div>
                <div className="positions">
                  <Position label="Supports" values={claim.supporters} />
                  <Position label="Opposes" values={claim.opponents} opponent />
                </div>
              </article>
            ))}
            {!visible.length && (
              <div className="state-panel empty-panel">
                <strong>No {filter} claims</strong>
                <p>The replay does not contain claims in this classification.</p>
              </div>
            )}
          </div>
        </section>
        <aside className="dissent-panel">
          <span className="dissent-symbol" aria-hidden="true">
            ≠
          </span>
          <h2>Dissent record</h2>
          <p>
            {data?.replay?.disagreements ??
              "No synthesized disagreement record is available for this run."}
          </p>
          <div className="evidence-sources">
            <h3>Source excerpts</h3>
            {(data?.replay?.evidence ?? []).slice(0, 3).map((source) => (
              <blockquote key={source.source_id}>
                <code>{source.source_id}</code>
                <p>“{source.quote}”</p>
                <cite>{source.title ?? "Recorded source"}</cite>
              </blockquote>
            ))}
          </div>
        </aside>
      </div>
    </div>
  );
}

function Position({ label, values, opponent = false }: { label: string; values: string[]; opponent?: boolean }) {
  return (
    <div>
      <span>{label}</span>
      <div>
        {values.length ? (
          values.map((value) => (
            <span className={opponent ? "opponent" : ""} key={value}>
              {value.slice(0, 1).toUpperCase()}
              <small>{value}</small>
            </span>
          ))
        ) : (
          <em>None</em>
        )}
      </div>
    </div>
  );
}
