import { useMemo, useState, type FormEvent } from "react";
import type { PolarisClient } from "../api/client";
import type {
  CatchupPolicy,
  Job,
  JobCreateInput,
  JobRun,
  ProviderHealth,
  ScheduleKind,
  ScheduleSpec,
} from "../api/types";
import { ErrorState, LoadingState } from "../components/StatePanel";
import { StatusBadge } from "../components/StatusBadge";
import { usePolling } from "../hooks/usePolling";
import { relativeTime } from "../utils/format";

export function SchedulesView({
  client,
  providers,
  demo,
}: {
  client: PolarisClient;
  providers: ProviderHealth[];
  demo: boolean;
}) {
  const jobs = usePolling(() => client.jobs(), { intervalMs: 7000 });
  const runs = usePolling(() => client.jobRuns(), { intervalMs: 7000 });
  const [kind, setKind] = useState<ScheduleKind>("cron");
  const [name, setName] = useState("");
  const [prompt, setPrompt] = useState("");
  const [provider, setProvider] = useState("");
  const [timezone, setTimezone] = useState(defaultTimezone);
  const [onceAt, setOnceAt] = useState("");
  const [intervalSeconds, setIntervalSeconds] = useState(3600);
  const [cron, setCron] = useState("0 * * * *");
  const [catchup, setCatchup] = useState<CatchupPolicy>("fire_once");
  const [maxCatchup, setMaxCatchup] = useState(1);
  const [deliveryPlatform, setDeliveryPlatform] = useState("");
  const [deliveryChannel, setDeliveryChannel] = useState("");
  const [profileId, setProfileId] = useState("");
  const [subjectKey, setSubjectKey] = useState("");
  const [preview, setPreview] = useState<string[]>([]);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");

  const configured = useMemo(
    () => providers.filter((item) => item.configured !== false),
    [providers],
  );
  const selectedProvider = provider || configured[0]?.name || "";

  function schedule(): ScheduleSpec {
    if (kind === "once") {
      return { kind, once_at: onceAt, timezone };
    }
    if (kind === "interval") {
      return { kind, interval_seconds: intervalSeconds, timezone };
    }
    return { kind, cron, timezone };
  }

  function payload(): JobCreateInput {
    const request: JobCreateInput["payload"]["request"] = {
      prompt,
      provider: selectedProvider,
    };
    if (profileId.trim()) request.profile_id = profileId.trim();
    if (subjectKey.trim()) request.subject_key = subjectKey.trim();
    return {
      name,
      schedule: schedule(),
      payload: {
        mode: "single",
        request,
        delivery:
          deliveryPlatform && deliveryChannel
            ? {
                platform: deliveryPlatform as "telegram" | "slack",
                channel_id: deliveryChannel,
              }
            : null,
      },
      catchup_policy: catchup,
      max_catchup: catchup === "bounded" ? maxCatchup : 1,
      grace_seconds: 0,
    };
  }

  async function showPreview() {
    setBusy("preview");
    setError("");
    try {
      setPreview(await client.previewJob({ schedule: schedule(), after: new Date().toISOString(), count: 5 }));
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy("");
    }
  }

  async function create(event: FormEvent) {
    event.preventDefault();
    setBusy("create");
    setError("");
    try {
      await client.createJob(payload());
      setName("");
      setPrompt("");
      setPreview([]);
      jobs.refresh();
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy("");
    }
  }

  async function transition(job: Job, action: "pause" | "resume" | "cancel") {
    setBusy(job.id);
    setError("");
    try {
      await (action === "pause"
        ? client.pauseJob(job.id)
        : action === "resume"
          ? client.resumeJob(job.id)
          : client.cancelJob(job.id));
      jobs.refresh();
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy("");
    }
  }

  async function retry(run: JobRun) {
    setBusy(run.id);
    setError("");
    try {
      await client.retryJobRun(run.id);
      runs.refresh();
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy("");
    }
  }

  return (
    <div className="view">
      <div className="view-heading">
        <div>
          <p className="context-line">Durable scheduler{demo ? " / synthetic data" : ""}</p>
          <h1>Schedules</h1>
          <p>Create and supervise single-agent jobs across local timezones and delivery channels.</p>
        </div>
        {demo && <span className="demo-label">Synthetic jobs</span>}
      </div>

      {error && <div className="inline-error" role="alert">{error}</div>}

      <div className="schedule-layout">
        <section className="section-block">
          <div className="section-heading">
            <div>
              <h2>Scheduled jobs</h2>
              <p>{jobs.data?.length ?? 0} durable definitions</p>
            </div>
            <button className="text-button" type="button" onClick={jobs.refresh}>Refresh</button>
          </div>
          {jobs.loading && !jobs.data ? (
            <LoadingState label="Loading schedules" />
          ) : jobs.error && !jobs.data ? (
            <ErrorState error={jobs.error} onRetry={jobs.refresh} />
          ) : jobs.data?.length ? (
            <div className="job-list">
              {jobs.data.map((job) => (
                <article className="job-row" key={job.id}>
                  <div className="job-title">
                    <div>
                      <strong>{job.name || "Untitled job"}</strong>
                      <code>{describeSchedule(job.schedule)}</code>
                    </div>
                    <StatusBadge status={job.state} />
                  </div>
                  <dl className="job-facts">
                    <div><dt>Next run</dt><dd>{job.next_run_at ? scheduleTime(job.next_run_at) : "—"}</dd></div>
                    <div><dt>Timezone</dt><dd>{job.schedule.timezone}</dd></div>
                    <div><dt>Catchup</dt><dd>{job.catchup_policy.replace("_", " ")}</dd></div>
                    <div><dt>Delivery</dt><dd>{job.payload.delivery?.platform ?? "none"}</dd></div>
                  </dl>
                  <div className="compact-actions">
                    {job.state === "scheduled" && (
                      <button className="button secondary" type="button" disabled={busy === job.id} onClick={() => void transition(job, "pause")}>Pause</button>
                    )}
                    {job.state === "paused" && (
                      <button className="button secondary" type="button" disabled={busy === job.id} onClick={() => void transition(job, "resume")}>Resume</button>
                    )}
                    {!["completed", "cancelled"].includes(job.state) && (
                      <button className="button danger" type="button" disabled={busy === job.id} onClick={() => void transition(job, "cancel")}>Cancel</button>
                    )}
                  </div>
                </article>
              ))}
            </div>
          ) : (
            <div className="empty-state"><strong>No jobs scheduled</strong><p>Create a once, interval, or cron job.</p></div>
          )}
        </section>

        <section className="section-block schedule-create">
          <div className="section-heading">
            <div>
              <h2>Create schedule</h2>
              <p>Single-agent payload</p>
            </div>
          </div>
          <form onSubmit={create}>
            <div className="form-grid two">
              <label>Name<input value={name} onChange={(event) => setName(event.target.value)} placeholder="Hourly health summary" /></label>
              <label>Provider
                <select value={selectedProvider} onChange={(event) => setProvider(event.target.value)} required>
                  {!configured.length && <option value="">No configured providers</option>}
                  {configured.map((item) => <option key={item.name} value={item.name}>{item.name}</option>)}
                </select>
              </label>
            </div>
            <label>Prompt
              <textarea value={prompt} onChange={(event) => setPrompt(event.target.value)} placeholder="Describe the scheduled task…" required />
            </label>
            <fieldset className="segmented-field">
              <legend>Schedule type</legend>
              <div>
                {(["once", "interval", "cron"] as ScheduleKind[]).map((value) => (
                  <label key={value}>
                    <input type="radio" name="schedule-kind" value={value} checked={kind === value} onChange={() => { setKind(value); setPreview([]); }} />
                    <span>{value}</span>
                  </label>
                ))}
              </div>
            </fieldset>
            <div className="form-grid two">
              {kind === "once" && (
                <label>Run once at<input type="datetime-local" value={onceAt} onChange={(event) => setOnceAt(event.target.value)} required /></label>
              )}
              {kind === "interval" && (
                <label>Interval (seconds)<input type="number" min="1" value={intervalSeconds} onChange={(event) => setIntervalSeconds(Number(event.target.value))} required /></label>
              )}
              {kind === "cron" && (
                <label>5-field cron
                  <input value={cron} onChange={(event) => setCron(event.target.value)} pattern="\S+\s+\S+\s+\S+\s+\S+\s+\S+" title="Enter five space-separated cron fields" required />
                </label>
              )}
              <label>IANA timezone<input value={timezone} onChange={(event) => setTimezone(event.target.value)} placeholder="UTC" required /></label>
            </div>
            <div className="form-grid two">
              <label>Catchup policy
                <select value={catchup} onChange={(event) => setCatchup(event.target.value as CatchupPolicy)}>
                  <option value="fire_once">Fire once (default)</option>
                  <option value="skip">Skip missed runs</option>
                  <option value="bounded">Run a bounded backlog</option>
                </select>
              </label>
              {catchup === "bounded" && (
                <label>Maximum catchup<input type="number" min="1" max="10" value={maxCatchup} onChange={(event) => setMaxCatchup(Number(event.target.value))} /></label>
              )}
            </div>
            <p className="field-explainer"><strong>Default fire-once catchup:</strong> after downtime, Polaris runs one missed occurrence instead of replaying the full backlog.</p>
            <details>
              <summary>Optional memory and delivery</summary>
              <div className="optional-fields">
                <div className="form-grid two">
                  <label>Memory profile<input value={profileId} onChange={(event) => setProfileId(event.target.value)} placeholder="Use daemon default" /></label>
                  <label>Memory subject<input value={subjectKey} onChange={(event) => setSubjectKey(event.target.value)} placeholder="Use daemon default" /></label>
                </div>
                <div className="form-grid two">
                  <label>Delivery channel
                    <select value={deliveryPlatform} onChange={(event) => setDeliveryPlatform(event.target.value)}>
                      <option value="">No delivery</option>
                      <option value="telegram">Telegram</option>
                      <option value="slack">Slack</option>
                    </select>
                  </label>
                  <label>Channel target<input value={deliveryChannel} onChange={(event) => setDeliveryChannel(event.target.value)} disabled={!deliveryPlatform} required={Boolean(deliveryPlatform)} placeholder="Chat or channel ID" /></label>
                </div>
              </div>
            </details>
            <div className="preview-actions">
              <button className="button secondary" type="button" onClick={() => void showPreview()} disabled={busy === "preview"}>
                {busy === "preview" ? "Previewing…" : "Preview next times"}
              </button>
              <button className="button primary" type="submit" disabled={busy === "create" || !selectedProvider}>
                {busy === "create" ? "Creating…" : "Create job"}
              </button>
            </div>
            {preview.length > 0 && (
              <ol className="preview-list" aria-label="Next scheduled times">
                {preview.map((value) => <li key={value}><time dateTime={value}>{new Date(value).toLocaleString()}</time></li>)}
              </ol>
            )}
          </form>
        </section>
      </div>

      <section className="section-block job-history">
        <div className="section-heading">
          <div><h2>Job-run history</h2><p>Interrupted runs can be retried explicitly.</p></div>
          <button className="text-button" type="button" onClick={runs.refresh}>Refresh</button>
        </div>
        {runs.loading && !runs.data ? (
          <LoadingState label="Loading job history" />
        ) : runs.error && !runs.data ? (
          <ErrorState error={runs.error} onRetry={runs.refresh} />
        ) : (
          <div className="table-wrap">
            <table>
              <thead><tr><th>Scheduled</th><th>Job</th><th>Status</th><th>Attempt</th><th>Delivery</th><th><span className="sr-only">Actions</span></th></tr></thead>
              <tbody>
                {(runs.data ?? []).map((run) => (
                  <tr key={run.id}>
                    <td><time dateTime={run.scheduled_for}>{relativeTime(run.scheduled_for)}</time></td>
                    <td><code>{run.job_id}</code></td>
                    <td><StatusBadge status={run.status} /></td>
                    <td>{run.attempt}</td>
                    <td>{run.delivery_status.replace("_", " ")}</td>
                    <td>{run.status === "interrupted" && <button className="button secondary" type="button" disabled={busy === run.id} onClick={() => void retry(run)}>Retry interrupted</button>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}

const defaultTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";

function describeSchedule(schedule: ScheduleSpec) {
  if (schedule.kind === "once") return `once · ${schedule.once_at}`;
  if (schedule.kind === "interval") return `every ${schedule.interval_seconds}s`;
  return `cron · ${schedule.cron}`;
}

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "The scheduler operation failed.";
}

function scheduleTime(value: string) {
  const seconds = Math.round((new Date(value).getTime() - Date.now()) / 1000);
  if (seconds <= 0) return "due now";
  if (seconds < 3600) return `in ${Math.ceil(seconds / 60)}m`;
  if (seconds < 86400) return `in ${Math.ceil(seconds / 3600)}h`;
  return `in ${Math.ceil(seconds / 86400)}d`;
}
