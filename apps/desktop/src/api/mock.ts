import {
  demoApprovals,
  demoArtifacts,
  demoChannelStatus,
  demoJobRuns,
  demoJobs,
  demoMemory,
  demoProviders,
  demoReplay,
  demoRuns,
  demoTimeline,
  demoUnknownOutbox,
} from "./fixtures";
import type {
  Approval,
  Job,
  JobCreateInput,
  JobRun,
  MemoryAddInput,
  MemoryEntry,
  MemoryRemoveInput,
  MemoryReviseInput,
  Run,
  SchedulePreviewInput,
  Transport,
} from "./types";

const copy = <T>(value: T): T => structuredClone(value);

export function createDemoTransport(): Transport {
  const runs = copy(demoRuns);
  const approvals = copy(demoApprovals);
  const memory = copy(demoMemory);
  const jobs = copy(demoJobs);
  const jobRuns = copy(demoJobRuns);
  const outbox = copy(demoUnknownOutbox);

  return async ({ method, path, body }) => {
    await new Promise((resolve) => window.setTimeout(resolve, 90));
    const route = path.split("?")[0];

    if (method === "GET" && route === "/health") {
      return ok({ status: "ok" });
    }
    if (method === "GET" && route === "/v1/runs") return ok(copy(runs));
    if (method === "GET" && route === "/v1/providers/doctor") {
      return ok(Object.fromEntries(demoProviders.map((item) => [item.name, item])));
    }
    if (method === "GET" && route === "/v1/memory") {
      const params = searchParams(path);
      return ok(
        copy(
          memory.filter(
            (entry) =>
              entry.profile_id === (params.get("profile_id") ?? "default") &&
              entry.subject_key === (params.get("subject_key") ?? "local") &&
              (params.get("include_tombstones") === "true" || !entry.tombstoned),
          ),
        ),
      );
    }
    if (method === "GET" && route === "/v1/memory/search") {
      const params = searchParams(path);
      const term = (params.get("query") ?? "").toLowerCase();
      const found = memory
        .filter(
          (entry) =>
            !entry.tombstoned &&
            entry.profile_id === (params.get("profile_id") ?? "default") &&
            entry.subject_key === (params.get("subject_key") ?? "local") &&
            entry.content.toLowerCase().includes(term),
        )
        .map((entry) => ({ entry, score: 1, search_backend: "demo" }));
      return ok(copy(found));
    }
    if (method === "POST" && route === "/v1/memory") {
      const input = body as MemoryAddInput;
      const created = memoryEntry(input);
      memory.unshift(created);
      return createdResponse(copy(created));
    }
    const memoryMatch = route.match(/^\/v1\/memory\/([^/]+)$/);
    if (memoryMatch) {
      const entry = memory.find((item) => item.id === decodeURIComponent(memoryMatch[1]));
      if (!entry) return missing();
      if (method === "PUT") {
        const input = body as MemoryReviseInput;
        if (input.expected_revision !== entry.revision || input.expected_hash !== entry.content_hash) {
          return conflict();
        }
        Object.assign(entry, input, {
          revision: entry.revision + 1,
          content_hash: demoHash(input.content),
          updated_at: new Date().toISOString(),
        });
        return ok(copy(entry));
      }
      if (method === "DELETE") {
        const input = body as MemoryRemoveInput;
        if (input.expected_revision !== entry.revision || input.expected_hash !== entry.content_hash) {
          return conflict();
        }
        entry.tombstoned = true;
        entry.revision += 1;
        entry.updated_at = new Date().toISOString();
        return ok(copy(entry));
      }
    }
    if (method === "GET" && route === "/v1/jobs") return ok(copy(jobs));
    if (method === "POST" && route === "/v1/jobs/preview") {
      return ok(preview(body as SchedulePreviewInput));
    }
    if (method === "POST" && route === "/v1/jobs") {
      const created = createJob(body as JobCreateInput);
      jobs.unshift(created);
      return createdResponse(copy(created));
    }
    if (method === "GET" && route === "/v1/jobs/runs") return ok(copy(jobRuns));
    const retryMatch = route.match(/^\/v1\/jobs\/runs\/([^/]+)\/retry$/);
    if (method === "POST" && retryMatch) {
      const original = jobRuns.find((item) => item.id === decodeURIComponent(retryMatch[1]));
      if (!original) return missing();
      const retried: JobRun = {
        ...copy(original),
        id: `job-run-${Math.random().toString(16).slice(2, 8)}`,
        status: "claimed",
        attempt: original.attempt + 1,
        execution_error: null,
        updated_at: new Date().toISOString(),
      };
      jobRuns.unshift(retried);
      return accepted(copy(retried));
    }
    const jobMatch = route.match(/^\/v1\/jobs\/([^/]+)(?:\/(pause|resume|cancel|runs))?$/);
    if (jobMatch) {
      const job = jobs.find((item) => item.id === decodeURIComponent(jobMatch[1]));
      if (!job) return missing();
      const action = jobMatch[2];
      if (method === "GET" && !action) return ok(copy(job));
      if (method === "GET" && action === "runs") {
        return ok(copy(jobRuns.filter((item) => item.job_id === job.id)));
      }
      if (method === "POST" && action) {
        job.state =
          action === "pause" ? "paused" : action === "resume" ? "scheduled" : "cancelled";
        job.next_run_at =
          action === "resume" ? new Date(Date.now() + 60 * 60_000).toISOString() : null;
        job.updated_at = new Date().toISOString();
        return ok(copy(job));
      }
    }
    if (method === "GET" && route === "/v1/channels/status") {
      return ok({ ...copy(demoChannelStatus), unknown_outbox: outbox.length });
    }
    if (method === "GET" && route === "/v1/channels/outbox/unknown") {
      return ok(copy(outbox.filter((item) => item.status === "unknown")));
    }
    const outboxMatch = route.match(
      /^\/v1\/channels\/outbox\/([^/]+)\/(mark-sent|retry)$/,
    );
    if (method === "POST" && outboxMatch) {
      const item = outbox.find(
        (candidate) => candidate.message.idempotency_key === decodeURIComponent(outboxMatch[1]),
      );
      if (!item) return missing();
      item.status = outboxMatch[2] === "mark-sent" ? "sent" : "pending";
      item.error = null;
      item.updated_at = new Date().toISOString();
      return ok(copy(item));
    }
    if (method === "POST" && route.startsWith("/v1/runs/") && isSubmission(route)) {
      const created = createRun(route, body as Record<string, unknown>);
      runs.unshift(created);
      return accepted(copy(created));
    }

    function searchParams(path: string) {
      return new URL(path, "http://demo.local").searchParams;
    }

    function demoHash(content: string) {
      return Array.from(content)
        .reduce((value, character) => ((value * 31 + character.charCodeAt(0)) >>> 0), 2166136261)
        .toString(16)
        .padStart(64, "0");
    }

    function memoryEntry(input: MemoryAddInput): MemoryEntry {
      const timestamp = new Date().toISOString();
      return {
        id: `mem-${Math.random().toString(16).slice(2, 8)}`,
        ...input,
        provenance_run_id: input.provenance_run_id ?? null,
        provenance_session_id: input.provenance_session_id ?? null,
        provenance_message_id: input.provenance_message_id ?? null,
        created_at: timestamp,
        updated_at: timestamp,
        revision: 1,
        content_hash: demoHash(input.content),
        blocked_reason: null,
        tombstoned: false,
      };
    }

    function preview(input: SchedulePreviewInput) {
      const start = new Date(input.after).getTime();
      const step =
        input.schedule.kind === "interval"
          ? (input.schedule.interval_seconds ?? 3600) * 1000
          : input.schedule.kind === "once"
            ? 0
            : 60 * 60_000;
      if (input.schedule.kind === "once" && input.schedule.once_at) {
        return [new Date(input.schedule.once_at).toISOString()];
      }
      return Array.from({ length: input.count }, (_, index) =>
        new Date(start + step * (index + 1)).toISOString(),
      );
    }

    function createJob(input: JobCreateInput): Job {
      const timestamp = new Date().toISOString();
      return {
        id: `job-${Math.random().toString(16).slice(2, 8)}`,
        ...copy(input),
        grace_seconds: input.grace_seconds ?? 0,
        state: "scheduled",
        next_run_at: preview({ schedule: input.schedule, after: timestamp, count: 1 })[0] ?? null,
        created_at: timestamp,
        updated_at: timestamp,
        version: 1,
      };
    }

    const runMatch = route.match(/^\/v1\/runs\/([^/]+)(?:\/(.*))?$/);
    if (runMatch) {
      const [, id, resource] = runMatch;
      const run = runs.find((item) => item.id === id);
      if (!run) return missing();
      if (method === "GET" && !resource) return ok(copy(run));
      if (method === "GET" && resource === "timeline") return ok(copy(demoTimeline));
      if (method === "GET" && resource === "artifacts") return ok(copy(demoArtifacts));
      if (method === "GET" && resource === "replay") return ok(copy(demoReplay));
      if (method === "GET" && resource === "approvals") {
        return ok(copy(approvals.filter((item) => item.run_id === id)));
      }
      if (method === "POST" && resource === "resume") {
        run.status = "running";
        return ok(copy(run));
      }
      if (method === "POST" && resource === "cancel") {
        run.status = "cancelled";
        return ok(copy(run));
      }
    }

    const approvalMatch = route.match(/^\/v1\/approvals\/([^/]+)(?:\/decision)?$/);
    if (method === "POST" && approvalMatch) {
      const item = approvals.find((approval) => approval.id === approvalMatch[1]);
      if (!item) return missing();
      const decision = (body as { decision: Approval["status"] }).decision;
      item.status = decision;
      item.decision = decision;
      item.decided_at = new Date().toISOString();
      return ok(copy(item));
    }
    return missing();
  };
}

function isSubmission(path: string) {
  return ["/v1/runs/single", "/v1/runs/fanout", "/v1/runs/foundry-router"].includes(path);
}

function createRun(path: string, body: Record<string, unknown>): Run {
  const mode =
    path === "/v1/runs/fanout"
      ? "fan-out"
      : path === "/v1/runs/foundry-router"
        ? "foundry-router"
        : "single";
  const timestamp = new Date().toISOString();
  return {
    id: `run-${Math.random().toString(16).slice(2, 8)}`,
    mode,
    request: copy(body),
    config: {},
    status: "created",
    budget: (body.budget as Run["budget"]) ?? {},
    parent_run_id: null,
    created_at: timestamp,
    updated_at: timestamp,
  };
}

const ok = (body: unknown) => ({ status: 200, body });
const accepted = (body: unknown) => ({ status: 202, body });
const createdResponse = (body: unknown) => ({ status: 201, body });
const conflict = () => ({
  status: 409,
  body: { error: "memory_conflict", detail: "Memory changed; refresh before retrying." },
});
const missing = () => ({ status: 404, body: { error: "not_found", detail: "Resource not found" } });
