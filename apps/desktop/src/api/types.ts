export type RequestMethod = "GET" | "POST" | "PUT" | "DELETE";

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
  profile_id?: string;
  subject_key?: string;
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

export interface MemoryScope {
  profile_id: string;
  subject_key: string;
}

export type MemoryKind = "user" | "agent" | "fact" | "preference";
export type MemoryTrust = "user_asserted" | "model_inferred" | "verified";

export interface MemoryEntry extends MemoryScope {
  id: string;
  content: string;
  kind: MemoryKind;
  trust_level: MemoryTrust;
  provenance_run_id: string | null;
  provenance_session_id: string | null;
  provenance_message_id: string | null;
  created_at: string;
  updated_at: string;
  revision: number;
  content_hash: string;
  blocked_reason: string | null;
  tombstoned: boolean;
}

export interface MemoryHit {
  entry: MemoryEntry;
  score: number;
  search_backend: string;
}

export interface MemoryAddInput extends MemoryScope {
  content: string;
  kind: MemoryKind;
  trust_level: "user_asserted";
  provenance_run_id?: string | null;
  provenance_session_id?: string | null;
  provenance_message_id?: string | null;
  idempotency_key?: string | null;
}

export interface MemoryReviseInput extends MemoryScope {
  content: string;
  kind?: MemoryKind;
  trust_level?: MemoryTrust;
  provenance_run_id?: string | null;
  provenance_session_id?: string | null;
  provenance_message_id?: string | null;
  expected_revision: number;
  expected_hash?: string | null;
}

export interface MemoryRemoveInput extends MemoryScope {
  expected_revision: number;
  expected_hash?: string | null;
}

export type ScheduleKind = "once" | "interval" | "cron";
export type CatchupPolicy = "skip" | "fire_once" | "bounded";
export type JobState = "scheduled" | "paused" | "completed" | "cancelled";
export type JobRunStatus =
  | "claimed"
  | "running"
  | "succeeded"
  | "failed"
  | "interrupted"
  | "cancelled";

export interface ScheduleSpec {
  kind: ScheduleKind;
  once_at?: string | null;
  interval_seconds?: number | null;
  cron?: string | null;
  timezone: string;
  start_at?: string | null;
}

export interface DeliveryTarget {
  platform: "telegram" | "slack";
  channel_id: string;
  thread_key?: string;
}

export interface JobPayload {
  mode: "single";
  request: {
    prompt: string;
    provider?: string;
    profile_id?: string;
    subject_key?: string;
  };
  delivery?: DeliveryTarget | null;
}

export interface JobCreateInput {
  name: string;
  schedule: ScheduleSpec;
  payload: JobPayload;
  catchup_policy: CatchupPolicy;
  max_catchup: number;
  grace_seconds?: number;
}

export interface SchedulePreviewInput {
  schedule: ScheduleSpec;
  after: string;
  count: number;
}

export interface Job {
  id: string;
  name: string;
  schedule: ScheduleSpec;
  payload: JobPayload;
  catchup_policy: CatchupPolicy;
  max_catchup: number;
  state: JobState;
  next_run_at: string | null;
  grace_seconds: number;
  created_at: string;
  updated_at: string;
  version: number;
}

export interface JobRun {
  id: string;
  job_id: string;
  scheduled_for: string;
  status: JobRunStatus;
  attempt: number;
  owner: string | null;
  lease_expires_at: string | null;
  polaris_run_id: string | null;
  cancel_requested: boolean;
  execution_error: string | null;
  delivery_status: "not_requested" | "pending" | "succeeded" | "failed" | "suppressed";
  delivery_error: string | null;
  claimed_at: string;
  started_at: string | null;
  completed_at: string | null;
  updated_at: string;
  payload?: JobPayload | null;
}

export interface ChannelStatus {
  started: boolean;
  telegram_enabled: boolean;
  slack_enabled: boolean;
  running_tasks: number;
  failures: string[];
  unknown_outbox: number;
  background_failures?: Record<string, string>;
}

export interface OutboundMessage {
  platform: "telegram" | "slack";
  idempotency_key: string;
  channel_id: string;
  thread_key: string;
  text: string;
  operation: "send_message" | "edit_message" | "answer_callback";
  parse_mode: "plain" | "html";
  message_id: string | null;
  callback_query_id: string | null;
  disable_notification: boolean;
  chunk_index: number;
  chunk_count: number;
  metadata: Record<string, unknown>;
}

export interface OutboxRecord {
  message: OutboundMessage;
  status: "pending" | "sending" | "sent" | "unknown" | "failed";
  content_hash: string;
  lease_owner: string | null;
  lease_expires_at: string | null;
  attempt_count: number;
  remote_receipt: Record<string, unknown> | null;
  error: string | null;
  created_at: string;
  updated_at: string;
}

export const TERMINAL_STATUSES: ReadonlySet<RunStatus> = new Set([
  "completed",
  "failed",
  "cancelled",
]);
