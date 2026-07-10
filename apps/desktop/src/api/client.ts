import { invoke } from "@tauri-apps/api/core";
import type {
  Approval,
  ConnectionConfig,
  FanoutRunInput,
  FoundryRunInput,
  ProviderHealth,
  Replay,
  Run,
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

  async request<T>(method: "GET" | "POST", path: string, body?: unknown): Promise<T> {
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
