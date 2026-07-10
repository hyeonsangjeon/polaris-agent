import type {
  Approval,
  Artifact,
  Claim,
  ProviderHealth,
  Replay,
  Run,
  TimelineEvent,
} from "./types";

const now = Date.now();
const stamp = (minutesAgo: number) => new Date(now - minutesAgo * 60_000).toISOString();

export const demoRuns: Run[] = [
  {
    id: "run-8f21a9",
    mode: "foundry-router",
    request: {
      question: "Assess the operational risks of moving the event pipeline to active-active.",
      provider: "foundry-router",
    },
    config: {},
    status: "running",
    budget: {
      call_limit: 12,
      token_limit: 48000,
      micro_usd_limit: 240000,
      used_calls: 7,
      used_tokens: 26840,
      used_micro_usd: 119400,
    },
    parent_run_id: null,
    created_at: stamp(18),
    updated_at: stamp(1),
  },
  {
    id: "run-771c42",
    mode: "fan-out",
    request: {
      question: "Compare rollback guarantees across the proposed storage backends.",
      workers: [
        { id: "durability", provider: "anthropic", role: "Durability analyst" },
        { id: "failure", provider: "openai", role: "Failure-mode skeptic" },
        { id: "cost", provider: "azure-openai", role: "Cost reviewer" },
      ],
    },
    config: { verifier: "anthropic", synthesizer: "openai" },
    status: "paused",
    budget: { call_limit: 18, token_limit: 72000, used_calls: 11, used_tokens: 41120 },
    parent_run_id: null,
    created_at: stamp(71),
    updated_at: stamp(8),
  },
  {
    id: "run-23d02e",
    mode: "single",
    request: {
      prompt: "Summarize the latest provider health report for the on-call handoff.",
      provider: "ollama",
    },
    config: {},
    status: "completed",
    budget: { call_limit: 4, token_limit: 12000, used_calls: 2, used_tokens: 4890 },
    parent_run_id: null,
    created_at: stamp(142),
    updated_at: stamp(137),
  },
  {
    id: "run-c94b11",
    mode: "fan-out",
    request: {
      question: "Produce an evidence-backed migration readiness recommendation.",
      workers: [],
    },
    config: { verifier: "anthropic", synthesizer: "openai" },
    status: "completed",
    budget: {
      call_limit: 20,
      token_limit: 90000,
      used_calls: 16,
      used_tokens: 78220,
      used_micro_usd: 304500,
    },
    parent_run_id: null,
    created_at: stamp(280),
    updated_at: stamp(240),
  },
];

export const demoTimeline: TimelineEvent[] = [
  {
    id: 1,
    run_id: "run-8f21a9",
    step_id: null,
    type: "run.created",
    payload: { mode: "foundry-router" },
    created_at: stamp(18),
  },
  {
    id: 2,
    run_id: "run-8f21a9",
    step_id: "route",
    type: "router.selection",
    payload: { requested: "auto", actual_model: "gpt-4.1" },
    created_at: stamp(17),
  },
  {
    id: 3,
    run_id: "run-8f21a9",
    step_id: "analysis",
    type: "provider.completed",
    payload: { model: "gpt-4.1", tokens: 12440 },
    created_at: stamp(11),
  },
  {
    id: 4,
    run_id: "run-8f21a9",
    step_id: "evidence",
    type: "evidence.verifying",
    payload: { sources: 9 },
    created_at: stamp(2),
  },
];

export const demoClaims: Claim[] = [
  {
    id: "CLM-014",
    statement:
      "Active-active lowers regional recovery time but introduces reconciliation risk for non-commutative events.",
    evidence_ids: ["EV-021", "EV-034"],
    supporters: ["failure", "durability"],
    opponents: [],
    status: "consensus",
    confidence: 0.94,
  },
  {
    id: "CLM-018",
    statement: "Operating cost will remain within ten percent of the current regional topology.",
    evidence_ids: ["EV-041"],
    supporters: ["cost"],
    opponents: ["failure"],
    status: "disputed",
    confidence: 0.58,
  },
  {
    id: "CLM-022",
    statement: "The cutover can be completed without a dual-write observation window.",
    evidence_ids: [],
    supporters: [],
    opponents: ["durability"],
    status: "unsupported",
    confidence: 0.21,
  },
];

export const demoReplay: Replay = {
  report:
    "Proceed with a staged active-active trial. Require deterministic event reconciliation and a fourteen-day dual-write observation window before production cutover.",
  final_output:
    "Proceed with a staged trial after the reconciliation and observability gates are met.",
  actual_models: ["gpt-4.1"],
  claims: demoClaims,
  evidence: [
    {
      source_id: "EV-021",
      title: "Regional failover exercise",
      quote: "Median recovery time fell from 23 minutes to 4 minutes in the dual-region drill.",
      content_hash: "a".repeat(64),
    },
    {
      source_id: "EV-034",
      title: "Event ordering review",
      quote: "Three write paths rely on order-sensitive updates and require explicit reconciliation.",
      content_hash: "b".repeat(64),
    },
    {
      source_id: "EV-041",
      title: "Capacity estimate",
      quote: "Projected steady-state infrastructure spend increases by 8–17 percent.",
      content_hash: "c".repeat(64),
    },
  ],
  disagreements:
    "Cost reviewer models steady-state traffic; failure-mode reviewer includes replay amplification and cross-region egress during incidents.",
  workers: [
    {
      worker_id: "durability",
      run_id: "child-1",
      output: "Reconciliation gate required.",
      requested_model: "auto",
      actual_models: ["gpt-4.1"],
      prompt_tokens: 6140,
      completion_tokens: 2240,
      micro_usd: 48300,
    },
    {
      worker_id: "failure",
      run_id: "child-2",
      output: "Rollback is unsafe without dual-write observation.",
      requested_model: "claude-sonnet",
      actual_models: ["claude-3-7-sonnet"],
      prompt_tokens: 5880,
      completion_tokens: 1980,
      micro_usd: 52100,
    },
    {
      worker_id: "cost",
      run_id: "child-3",
      output: "Expected uplift is 8–17 percent.",
      requested_model: "gpt-4.1-mini",
      actual_models: ["gpt-4.1-mini"],
      prompt_tokens: 4010,
      completion_tokens: 1470,
      micro_usd: 19000,
    },
  ],
  cost: {
    requested_models: {
      durability: "auto",
      failure: "claude-sonnet",
      cost: "gpt-4.1-mini",
    },
    actual_models: {
      durability: ["gpt-4.1"],
      failure: ["claude-3-7-sonnet"],
      cost: ["gpt-4.1-mini"],
    },
    input_tokens: 16030,
    output_tokens: 5690,
    total_tokens: 21720,
    micro_usd: 119400,
    calls: 7,
  },
};

export const demoArtifacts: Artifact[] = [
  {
    id: "artifact-report",
    run_id: "run-8f21a9",
    step_id: null,
    name: "report.md",
    media_type: "text/markdown",
    uri: "journal://run-8f21a9/report.md",
    sha256: "1b8e…d90f",
    size_bytes: 18420,
    metadata: null,
    created_at: stamp(2),
  },
  {
    id: "artifact-evidence",
    run_id: "run-8f21a9",
    step_id: null,
    name: "evidence.jsonl",
    media_type: "application/jsonl",
    uri: "journal://run-8f21a9/evidence.jsonl",
    sha256: "8ac1…443b",
    size_bytes: 42118,
    metadata: null,
    created_at: stamp(2),
  },
];

export const demoApprovals: Approval[] = [
  {
    id: "apr-02f4",
    run_id: "run-771c42",
    step_id: "step-write-plan",
    kind: "tool",
    request: {
      tool: "write_file",
      target: "artifacts/migration-plan.md",
      safety: "reconcilable",
    },
    status: "pending",
    decision: null,
    decision_reason: null,
    created_at: stamp(8),
    decided_at: null,
  },
  {
    id: "apr-1d92",
    run_id: "run-771c42",
    step_id: "step-deploy-check",
    kind: "uncertain_outcome",
    request: {
      tool: "deployment_probe",
      target: "staging/event-gateway",
      uncertainty_reason: "Connection closed after dispatch; side effect could not be confirmed.",
      parameters: {
        region: "eastus",
        checks: ["deployment", "traffic"],
      },
    },
    status: "pending",
    decision: null,
    decision_reason: null,
    created_at: stamp(6),
    decided_at: null,
  },
];

export const demoProviders: ProviderHealth[] = [
  { name: "ollama", status: "healthy", model: "qwen3", configured: true },
  { name: "openai", status: "healthy", model: "gpt-4.1", configured: true },
  { name: "anthropic", status: "healthy", model: "claude-3-7-sonnet", configured: true },
  {
    name: "foundry-router",
    status: "degraded",
    model: "model-router",
    detail: "Elevated routing latency",
    configured: true,
  },
];
