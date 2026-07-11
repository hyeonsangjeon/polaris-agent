import { useState } from "react";
import type { PolarisClient } from "../api/client";
import type { OutboxRecord } from "../api/types";
import { ErrorState, LoadingState } from "../components/StatePanel";
import { usePolling } from "../hooks/usePolling";
import { relativeTime } from "../utils/format";

type Resolution = { key: string; action: "mark" | "retry" } | null;

export function ChannelsView({ client, demo }: { client: PolarisClient; demo: boolean }) {
  const status = usePolling(() => client.channelStatus(), { intervalMs: 6000 });
  const outbox = usePolling(() => client.unknownOutbox(), { intervalMs: 6000 });
  const [resolution, setResolution] = useState<Resolution>(null);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function resolve(record: OutboxRecord) {
    if (!resolution) return;
    setBusy(true);
    setError("");
    try {
      if (resolution.action === "mark") {
        await client.markOutboxSent(record.message.idempotency_key, note);
      } else {
        await client.retryOutbox(record.message.idempotency_key, note);
      }
      setResolution(null);
      setNote("");
      outbox.refresh();
      status.refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The outbox action failed.");
    } finally {
      setBusy(false);
    }
  }

  const data = status.data;
  return (
    <div className="view">
      <div className="view-heading">
        <div>
          <p className="context-line">Message adapters{demo ? " / synthetic data" : ""}</p>
          <h1>Channels</h1>
          <p>Inspect adapter health and explicitly resolve deliveries whose remote outcome is unknown.</p>
        </div>
        {demo && <span className="demo-label">Synthetic channels</span>}
      </div>

      {error && <div className="inline-error" role="alert">{error}</div>}
      {status.loading && !data ? (
        <LoadingState label="Loading channel status" />
      ) : status.error && !data ? (
        <ErrorState error={status.error} onRetry={status.refresh} />
      ) : (
        <section className="channel-summary" aria-label="Channel status">
          <article>
            <div className="channel-heading">
              <span className={`channel-dot ${data?.telegram_enabled && data.started ? "healthy" : ""}`} aria-hidden="true" />
              <div><h2>Telegram</h2><p>Long polling</p></div>
              <strong>{data?.telegram_enabled ? (data.started ? "Running" : "Stopped") : "Disabled"}</strong>
            </div>
            <p>Authorized user and chat allowlists are checked before commands enter the durable inbox.</p>
          </article>
          <article>
            <div className="channel-heading">
              <span className={`channel-dot ${data?.slack_enabled && data.started ? "healthy" : ""}`} aria-hidden="true" />
              <div><h2>Slack</h2><p>Socket Mode</p></div>
              <strong>{data?.slack_enabled ? (data.started ? "Running" : "Stopped") : "Disabled"}</strong>
            </div>
            <p>Socket events are acknowledged, authorized, and journaled before agent work begins.</p>
          </article>
          <dl className="channel-counts">
            <div><dt>Running tasks</dt><dd>{data?.running_tasks ?? 0}</dd></div>
            <div><dt>Failures</dt><dd>{data?.failures.length ?? 0}</dd></div>
            <div><dt>Unknown deliveries</dt><dd>{data?.unknown_outbox ?? 0}</dd></div>
          </dl>
        </section>
      )}

      {data && data.failures.length > 0 && (
        <section className="channel-failures" aria-labelledby="channel-failures-heading">
          <h2 id="channel-failures-heading">Recent adapter failures</h2>
          <p>{data.failures.length} failure{data.failures.length === 1 ? "" : "s"} recorded. Review daemon logs for redacted diagnostics.</p>
        </section>
      )}

      <section className="section-block outbox-section">
        <div className="section-heading">
          <div>
            <h2>Unknown outbox</h2>
            <p>Never retried automatically: delivery may already have reached the recipient.</p>
          </div>
          <button className="text-button" type="button" onClick={outbox.refresh}>Refresh</button>
        </div>
        {outbox.loading && !outbox.data ? (
          <LoadingState label="Loading unknown deliveries" />
        ) : outbox.error && !outbox.data ? (
          <ErrorState error={outbox.error} onRetry={outbox.refresh} />
        ) : outbox.data?.length ? (
          <ul className="outbox-list">
            {outbox.data.map((record) => {
              const key = record.message.idempotency_key;
              const active = resolution?.key === key;
              return (
                <li key={key}>
                  <div className="outbox-main">
                    <div className="outbox-meta">
                      <span>{record.message.platform}</span>
                      <code>{key}</code>
                      <time dateTime={record.updated_at}>{relativeTime(record.updated_at)}</time>
                    </div>
                    <p>Outbound message content withheld. Resolve using the channel receipt and idempotency key.</p>
                    <small>Remote receipt unavailable · attempt {record.attempt_count}</small>
                  </div>
                  <div className="compact-actions">
                    <button className="button secondary" type="button" onClick={() => { setResolution({ key, action: "mark" }); setNote(""); }}>
                      Mark sent
                    </button>
                    <button className="button danger" type="button" onClick={() => { setResolution({ key, action: "retry" }); setNote(""); }}>
                      Retry
                    </button>
                  </div>
                  {active && (
                    <div className="outbox-confirm" role="group" aria-label={`Confirm ${resolution.action === "mark" ? "mark sent" : "retry"}`}>
                      <strong>{resolution.action === "mark" ? "Confirm delivery was sent" : "Confirm manual retry"}</strong>
                      <p>{resolution.action === "retry" ? "Retry may send a duplicate message. Verify the channel first." : "This records operator confirmation without sending again."}</p>
                      <label>Audit note
                        <input value={note} onChange={(event) => setNote(event.target.value)} placeholder="Why is this resolution safe?" required />
                      </label>
                      <div className="compact-actions">
                        <button className={resolution.action === "retry" ? "button danger" : "button primary"} type="button" disabled={busy || !note.trim()} onClick={() => void resolve(record)}>
                          Confirm {resolution.action === "mark" ? "mark sent" : "retry"}
                        </button>
                        <button className="button secondary" type="button" onClick={() => setResolution(null)}>Cancel</button>
                      </div>
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        ) : (
          <div className="empty-state"><strong>No unknown deliveries</strong><p>Every outbound message has a confirmed state.</p></div>
        )}
      </section>
      <p className="secret-boundary-note">Credentials, token values, and environment contents remain behind the daemon boundary and are never shown here.</p>
    </div>
  );
}
