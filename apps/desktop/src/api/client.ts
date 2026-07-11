import { invoke } from "@tauri-apps/api/core";
import type {
  Approval,
  ConnectionConfig,
  FanoutRunInput,
  FoundryRunInput,
  ChannelStatus,
  Job,
  JobCreateInput,
  JobRun,
  MemoryAddInput,
  MemoryEntry,
  MemoryHit,
  MemoryRemoveInput,
  MemoryReviseInput,
  MemoryScope,
  OutboxRecord,
  ProviderHealth,
  Replay,
  Run,
  SchedulePreviewInput,
  SingleRunInput,
  Transport,
  TransportRequest,
  TransportResponse,
} from "./types";

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

const invokeTransport: Transport = (request) =>
  invoke<TransportResponse>("daemon_request", { request });

function errorMessage(body: unknown): string {
  if (body && typeof body === "object") {
    const value = body as Record<string, unknown>;
    if (typeof value.detail === "string") return value.detail;
    if (typeof value.error === "string") return value.error;
  }
  return "The daemon could not complete this request.";
}

export class PolarisClient {
  readonly #config: ConnectionConfig;
  readonly #transport: Transport;

  constructor(config: ConnectionConfig, transport: Transport = invokeTransport) {
    this.#config = { ...config };
    this.#transport = transport;
  }

  withConfig(config: ConnectionConfig): PolarisClient {
    return new PolarisClient(config, this.#transport);
  }

  async request<T>(
    method: "GET" | "POST" | "PUT" | "DELETE",
    path: string,
    body?: unknown,
  ): Promise<T> {
    const request: TransportRequest = { ...this.#config, method, path };
    if (body !== undefined) request.body = body;
    const response = await this.#transport(request);
    if (response.status < 200 || response.status >= 300) {
      throw new ApiError(response.status, errorMessage(response.body));
    }
    return response.body as T;
  }

  health = () => this.request<{ status: string }>("GET", "/health");
  runs = () => this.request<Run[]>("GET", "/v1/runs");
  run = (id: string) => this.request<Run>("GET", `/v1/runs/${id}`);
  timeline = (id: string) =>
    this.request<import("./types").TimelineEvent[]>("GET", `/v1/runs/${id}/timeline`);
  artifacts = (id: string) =>
    this.request<import("./types").Artifact[]>("GET", `/v1/runs/${id}/artifacts`);
  replay = (id: string) => this.request<Replay>("GET", `/v1/runs/${id}/replay`);
  approvals = (id: string, pending = false) =>
    this.request<Approval[]>(
      "GET",
      `/v1/runs/${id}/approvals${pending ? "?pending=true" : ""}`,
    );
  providers = async (): Promise<ProviderHealth[]> => {
    const value = await this.request<Record<string, unknown>>("GET", "/v1/providers/doctor");
    return normalizeProviders(value);
  };
  submitSingle = (body: SingleRunInput) =>
    this.request<Run>("POST", "/v1/runs/single", body);
  submitFanout = (body: FanoutRunInput) =>
    this.request<Run>("POST", "/v1/runs/fanout", body);
  submitFoundry = (body: FoundryRunInput) =>
    this.request<Run>("POST", "/v1/runs/foundry-router", body);
  decide = (id: string, approved: boolean) =>
    this.request<Approval>("POST", `/v1/approvals/${id}/decision`, {
      decision: approved ? "approved" : "rejected",
      decided_by: "desktop-operator",
    });
  resume = (id: string) => this.request<Run>("POST", `/v1/runs/${id}/resume`);
  cancel = (id: string) => this.request<Run>("POST", `/v1/runs/${id}/cancel`);
  memory = (scope: MemoryScope, includeTombstones = false) =>
    this.request<MemoryEntry[]>(
      "GET",
      query("/v1/memory", {
        profile_id: scope.profile_id,
        subject_key: scope.subject_key,
        include_tombstones: includeTombstones,
      }),
    );
  searchMemory = (scope: MemoryScope, search: string, limit = 50) =>
    this.request<MemoryHit[]>(
      "GET",
      query("/v1/memory/search", {
        query: search,
        profile_id: scope.profile_id,
        subject_key: scope.subject_key,
        limit,
      }),
    );
  addMemory = (body: MemoryAddInput) =>
    this.request<MemoryEntry>("POST", "/v1/memory", body);
  reviseMemory = (id: string, body: MemoryReviseInput) =>
    this.request<MemoryEntry>("PUT", `/v1/memory/${encodeURIComponent(id)}`, body);
  removeMemory = (id: string, body: MemoryRemoveInput) =>
    this.request<MemoryEntry>("DELETE", `/v1/memory/${encodeURIComponent(id)}`, body);
  previewJob = (body: SchedulePreviewInput) =>
    this.request<string[]>("POST", "/v1/jobs/preview", body);
  createJob = (body: JobCreateInput) => this.request<Job>("POST", "/v1/jobs", body);
  jobs = () => this.request<Job[]>("GET", "/v1/jobs");
  job = (id: string) => this.request<Job>("GET", `/v1/jobs/${encodeURIComponent(id)}`);
  jobRuns = (id?: string) =>
    this.request<JobRun[]>(
      "GET",
      id ? `/v1/jobs/${encodeURIComponent(id)}/runs` : "/v1/jobs/runs",
    );
  retryJobRun = (id: string) =>
    this.request<JobRun>("POST", `/v1/jobs/runs/${encodeURIComponent(id)}/retry`);
  pauseJob = (id: string) =>
    this.request<Job>("POST", `/v1/jobs/${encodeURIComponent(id)}/pause`);
  resumeJob = (id: string) =>
    this.request<Job>("POST", `/v1/jobs/${encodeURIComponent(id)}/resume`);
  cancelJob = (id: string) =>
    this.request<Job>("POST", `/v1/jobs/${encodeURIComponent(id)}/cancel`);
  channelStatus = () => this.request<ChannelStatus>("GET", "/v1/channels/status");
  unknownOutbox = () =>
    this.request<OutboxRecord[]>("GET", "/v1/channels/outbox/unknown");
  markOutboxSent = (idempotencyKey: string, note: string) =>
    this.request<OutboxRecord>(
      "POST",
      `/v1/channels/outbox/${encodeURIComponent(idempotencyKey)}/mark-sent`,
      { note },
    );
  retryOutbox = (idempotencyKey: string, note: string) =>
    this.request<OutboxRecord>(
      "POST",
      `/v1/channels/outbox/${encodeURIComponent(idempotencyKey)}/retry`,
      { note },
    );
}

function query(path: string, values: Record<string, string | number | boolean>) {
  const params = new URLSearchParams();
  Object.entries(values).forEach(([key, value]) => params.set(key, String(value)));
  return `${path}?${params.toString()}`;
}

function normalizeProviders(value: Record<string, unknown>): ProviderHealth[] {
  const source =
    value.providers && typeof value.providers === "object"
      ? (value.providers as Record<string, unknown>)
      : value;
  return Object.entries(source).map(([name, raw]) => {
    if (typeof raw === "string") {
      return { name, status: raw === "ok" ? "healthy" : raw };
    }
    const detail = (raw ?? {}) as Record<string, unknown>;
    const rawStatus = String(detail.status ?? (detail.ok === false ? "unavailable" : "healthy"));
    return {
      name,
      status: rawStatus === "ok" ? "healthy" : rawStatus,
      model: typeof detail.model === "string" ? detail.model : undefined,
      detail: typeof detail.detail === "string" ? detail.detail : undefined,
      configured: typeof detail.configured === "boolean" ? detail.configured : undefined,
    };
  });
}

export function createClient(config: ConnectionConfig, transport?: Transport): PolarisClient {
  return new PolarisClient(config, transport);
}
