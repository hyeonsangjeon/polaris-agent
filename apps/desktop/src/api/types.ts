export type RequestMethod = "GET" | "POST";

export interface ConnectionConfig {
  daemonUrl: string;
  tokenFile: string;
}

export interface TransportRequest extends ConnectionConfig {
  method: RequestMethod;
  path: string;
  body?: unknown;
}

export interface TransportResponse {
  status: number;
  body: unknown;
}

export type Transport = (request: TransportRequest) => Promise<TransportResponse>;

export type RunStatus =
  | "created"
  | "running"
  | "paused"
  | "completed"
  | "failed"
  | "cancelled";

export interface Budget {
  call_limit?: number;
  token_limit?: number;
  micro_usd_limit?: number;
  wall_seconds_limit?: number;
  used_calls?: number;
  used_tokens?: number;
  used_micro_usd?: number;
  used_wall_seconds?: number;
}

export interface Run {
  id: string;
  mode: "single" | "fan-out" | "foundry-router" | string;
  request: Record<string, unknown>;
  config: Record<string, unknown>;
  status: RunStatus;
  budget: Budget;
  parent_run_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface TimelineEvent {
  id: number;
  run_id: string;
  step_id: string | null;
  type: string;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface Artifact {
  id: string;
  run_id: string;
  step_id: string | null;
  name: string;
  media_type: string | null;
  uri: string;
  sha256: string | null;
  size_bytes: number | null;
  metadata: Record<string, unknown> | null;
  created_at: string;
}

export interface Approval {
  id: string;
  run_id: string;
  step_id: string | null;
  kind: "tool" | "uncertain_outcome" | string;
  request: Record<string, unknown>;
  status: "pending" | "approved" | "rejected" | string;
  decision: string | null;
  decision_reason: string | null;
  created_at: string;
  decided_at: string | null;
}

export type ClaimStatus = "consensus" | "disputed" | "unsupported";

export interface Claim {
  id: string;
  statement: string;
  evidence_ids: string[];
  supporters: string[];
  opponents: string[];
  status: ClaimStatus;
  confidence: number;
}

export interface EvidenceItem {
  source_id: string;
  url?: string | null;
  title?: string | null;
  quote: string;
  content_hash: string;
}

export interface WorkerResult {
  worker_id: string;
  run_id: string;
  output: string;
  requested_model: string;
  actual_models: string[];
  prompt_tokens: number;
  completion_tokens: number;
  micro_usd: number;
}

export interface Replay {
  final_output?: string | null;
  actual_models?: string[];
  provider_usage?: {
    prompt_tokens: number;
    completion_tokens: number;
    total_tokens: number;
  };
  report?: string;
  claims?: Claim[];
  evidence?: EvidenceItem[];
  disagreements?: string;
  workers?: WorkerResult[];
  cost?: {
    requested_models: Record<string, string>;
    actual_models: Record<string, string[]>;
    input_tokens: number;
    output_tokens: number;
    total_tokens: number;
    micro_usd: number;
    calls: number;
  };
}

export interface ProviderHealth {
  name: string;
  status: "healthy" | "degraded" | "unavailable" | string;
  model?: string;
  detail?: string;
  configured?: boolean;
}

export interface BudgetInput {
  call_limit?: number;
  token_limit?: number;
  micro_usd_limit?: number;
  wall_seconds_limit?: number;
}

export interface WorkerInput {
  id: string;
  provider: string;
  role: string;
  instructions: string;
}

export interface SingleRunInput {
  prompt: string;
  provider?: string;
  budget: BudgetInput;
  schedule: true;
}

export interface FanoutRunInput {
  question: string;
  workers: WorkerInput[];
  verifier: string;
  synthesizer: string;
  max_workers: number;
  budget: BudgetInput;
  schedule: true;
}

export interface FoundryRunInput {
  question: string;
  provider: string;
  budget: BudgetInput;
  schedule: true;
}

export interface RunBundle {
  run: Run;
  timeline: TimelineEvent[];
  artifacts: Artifact[];
  approvals: Approval[];
  replay: Replay | null;
}

export const TERMINAL_STATUSES: ReadonlySet<RunStatus> = new Set([
  "completed",
  "failed",
  "cancelled",
]);
