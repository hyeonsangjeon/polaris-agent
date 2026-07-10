import { useEffect, useMemo, useState, type FormEvent } from "react";
import type { PolarisClient } from "../api/client";
import type { BudgetInput, ProviderHealth, Run, WorkerInput } from "../api/types";
import { BudgetFields } from "../components/BudgetFields";

type Strategy = "single" | "fanout" | "foundry";

const strategies: Array<{ id: Strategy; title: string; description: string; signal: string }> = [
  {
    id: "single",
    title: "Single",
    description: "One provider, one durable agent loop. Best for focused operational work.",
    signal: "Direct",
  },
  {
    id: "fanout",
    title: "Local Fan-out",
    description: "Up to eight named perspectives, then verify and synthesize locally.",
    signal: "Parallel",
  },
  {
    id: "foundry",
    title: "Foundry Router",
    description: "Delegate model selection to Azure Foundry while Polaris records the result.",
    signal: "Routed",
  },
];

export function NewRunView({
  client,
  providers,
  onCreated,
}: {
  client: PolarisClient;
  providers: ProviderHealth[];
  onCreated: (run: Run) => void;
}) {
  const [strategy, setStrategy] = useState<Strategy>("single");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const configuredProviders = useMemo(
    () => uniqueProviders(providers.filter((provider) => provider.configured !== false)),
    [providers],
  );
  const routerProviders = useMemo(
    () => configuredProviders.filter(isModelRouter),
    [configuredProviders],
  );

  async function submit(action: () => Promise<Run>) {
    setSubmitting(true);
    setError("");
    try {
      onCreated(await action());
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The run could not be created.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="view">
      <div className="view-heading">
        <div>
          <p className="context-line">Create durable work</p>
          <h1>New run</h1>
          <p>Choose an execution contract first. Strategy changes who selects models and verifies evidence.</p>
        </div>
      </div>
      <fieldset className="strategy-picker">
        <legend className="sr-only">Run strategy</legend>
        {strategies.map((item) => (
          <label key={item.id} className={strategy === item.id ? "selected" : ""}>
            <input
              type="radio"
              name="strategy"
              value={item.id}
              checked={strategy === item.id}
              onChange={() => setStrategy(item.id)}
            />
            <span className="strategy-signal">{item.signal}</span>
            <strong>{item.title}</strong>
            <small>{item.description}</small>
            <i aria-hidden="true">{strategy === item.id ? "●" : "○"}</i>
          </label>
        ))}
      </fieldset>
      {error && (
        <div className="inline-error" role="alert">
          <strong>Run not created.</strong> {error}
        </div>
      )}
      <div className="run-form-frame">
        {strategy === "single" && (
          <SingleForm
            client={client}
            providers={configuredProviders}
            submit={submit}
            submitting={submitting}
          />
        )}
        {strategy === "fanout" && (
          <FanoutForm
            client={client}
            providers={configuredProviders}
            submit={submit}
            submitting={submitting}
          />
        )}
        {strategy === "foundry" && (
          <FoundryForm
            client={client}
            providers={routerProviders}
            submit={submit}
            submitting={submitting}
          />
        )}
      </div>
    </div>
  );
}

interface FormProps {
  client: PolarisClient;
  providers: ProviderHealth[];
  submit: (action: () => Promise<Run>) => Promise<void>;
  submitting: boolean;
}

function SingleForm({ client, providers, submit, submitting }: FormProps) {
  const [prompt, setPrompt] = useState("");
  const [provider, setProvider] = useProviderChoice(providers, 0, true);
  const [budget, setBudget] = useState<BudgetInput>({
    call_limit: 8,
    token_limit: 24000,
    micro_usd_limit: 150000,
    wall_seconds_limit: 600,
  });
  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    void submit(() => client.submitSingle({ prompt, provider, budget, schedule: true }));
  };
  return (
    <form onSubmit={onSubmit}>
      <FormIntro
        title="Single agent"
        text="A direct model loop with durable tool receipts, replay, and budget enforcement."
      />
      <label>
        Prompt
        <textarea
          value={prompt}
          onChange={(event) => setPrompt(event.target.value)}
          rows={5}
          placeholder="Describe the task, constraints, and required output…"
          required
        />
      </label>
      <label>
        Provider
        <select
          value={provider}
          onChange={(event) => setProvider(event.target.value)}
          disabled={!providers.length}
          required
        >
          <ProviderOptions providers={providers} />
        </select>
        {!providers.length && <small>No configured providers were reported by the daemon.</small>}
      </label>
      <BudgetFields value={budget} onChange={setBudget} />
      <SubmitButton
        submitting={submitting}
        disabled={!provider}
        label="Start single run"
      />
    </form>
  );
}

function FanoutForm({ client, providers, submit, submitting }: FormProps) {
  const [question, setQuestion] = useState("");
  const providerNames = useMemo(() => providers.map((provider) => provider.name), [providers]);
  const [workers, setWorkers] = useState<WorkerInput[]>([
    worker(1, preferredProvider(providers, 0), "Evidence analyst"),
    worker(2, preferredProvider(providers, 1), "Failure-mode skeptic"),
  ]);
  const [verifier, setVerifier] = useProviderChoice(providers);
  const [synthesizer, setSynthesizer] = useProviderChoice(providers, 1);
  const [concurrency, setConcurrency] = useState(2);
  const [budget, setBudget] = useState<BudgetInput>({
    call_limit: 18,
    token_limit: 72000,
    micro_usd_limit: 400000,
    wall_seconds_limit: 1200,
  });
  const update = (index: number, patch: Partial<WorkerInput>) =>
    setWorkers((current) =>
      current.map((item, itemIndex) => (itemIndex === index ? { ...item, ...patch } : item)),
    );
  useEffect(() => {
    setWorkers((current) =>
      current.map((item, index) =>
        providerNames.includes(item.provider)
          ? item
          : { ...item, provider: preferredProvider(providers, index) },
      ),
    );
  }, [providerNames, providers]);
  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    void submit(() =>
      client.submitFanout({
        question,
        workers,
        verifier,
        synthesizer,
        max_workers: concurrency,
        budget,
        schedule: true,
      }),
    );
  };
  return (
    <form onSubmit={onSubmit}>
      <FormIntro
        title="Local ensemble"
        text="Polaris runs each role independently, normalizes evidence, verifies claims, and synthesizes dissent."
      />
      <label>
        Research question
        <textarea
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
          rows={4}
          placeholder="State one decision-ready question…"
          required
        />
      </label>
      <fieldset className="worker-builder">
        <div className="field-heading">
          <legend>Workers</legend>
          <button
            className="text-button"
            type="button"
            disabled={workers.length >= 8}
            onClick={() =>
              setWorkers([
                ...workers,
                worker(
                  workers.length + 1,
                  preferredProvider(providers, workers.length),
                  "Researcher",
                ),
              ])
            }
          >
            + Add worker
          </button>
        </div>
        {workers.map((item, index) => (
          <div className="worker-row" key={item.id}>
            <span className="worker-index">{index + 1}</span>
            <label>
              <span className="sr-only">Worker {index + 1} provider</span>
              <select
                aria-label={`Worker ${index + 1} provider`}
                value={item.provider}
                onChange={(event) => update(index, { provider: event.target.value })}
                disabled={!providers.length}
                required
              >
                <ProviderOptions providers={providers} />
              </select>
            </label>
            <label>
              <span className="sr-only">Worker {index + 1} role</span>
              <input
                aria-label={`Worker ${index + 1} role`}
                value={item.role}
                onChange={(event) => update(index, { role: event.target.value })}
                required
              />
            </label>
            <button
              className="icon-button"
              type="button"
              aria-label={`Remove worker ${index + 1}`}
              disabled={workers.length === 1}
              onClick={() => setWorkers(workers.filter((_, workerIndex) => workerIndex !== index))}
            >
              ×
            </button>
          </div>
        ))}
      </fieldset>
      <div className="form-grid three">
        <label>
          Verifier
          <select
            value={verifier}
            onChange={(event) => setVerifier(event.target.value)}
            disabled={!providers.length}
            required
          >
            <ProviderOptions providers={providers} />
          </select>
        </label>
        <label>
          Synthesizer
          <select
            value={synthesizer}
            onChange={(event) => setSynthesizer(event.target.value)}
            disabled={!providers.length}
            required
          >
            <ProviderOptions providers={providers} />
          </select>
        </label>
        <label>
          Concurrency
          <input
            type="number"
            min="1"
            max="8"
            value={concurrency}
            onChange={(event) => setConcurrency(Number(event.target.value))}
          />
        </label>
      </div>
      {!providers.length && (
        <p className="provider-empty" role="status">
          No configured providers were reported by the daemon.
        </p>
      )}
      <BudgetFields value={budget} onChange={setBudget} />
      <SubmitButton
        submitting={submitting}
        disabled={!providers.length}
        label={`Start ${workers.length}-worker fan-out`}
      />
    </form>
  );
}

function FoundryForm({ client, providers, submit, submitting }: FormProps) {
  const [question, setQuestion] = useState("");
  const [provider, setProvider] = useProviderChoice(providers);
  const [budget, setBudget] = useState<BudgetInput>({
    call_limit: 10,
    token_limit: 48000,
    micro_usd_limit: 300000,
    wall_seconds_limit: 900,
  });
  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    void submit(() => client.submitFoundry({ question, provider, budget, schedule: true }));
  };
  return (
    <form onSubmit={onSubmit}>
      <FormIntro
        title="Foundry-managed routing"
        text="Azure Foundry owns model selection. Polaris remains the durable record for the request, response, evidence, usage, and replay."
      />
      <div className="foundry-contract">
        <span className="foundry-symbol" aria-hidden="true">
          ⟡
        </span>
        <div>
          <strong>Selection is observable, not configured here</strong>
          <p>
            The selected <code>response.model</code> is recorded alongside evidence and cost. Replay
            surfaces the actual model even when it differs between calls.
          </p>
        </div>
      </div>
      <label>
        Question
        <textarea
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
          rows={5}
          placeholder="Ask the router a decision-ready question…"
          required
        />
      </label>
      <label>
        Router provider
        <select
          value={provider}
          onChange={(event) => setProvider(event.target.value)}
          disabled={!providers.length}
          required
        >
          <ProviderOptions providers={providers} emptyLabel="No configured model-router provider" />
        </select>
        {!providers.length && (
          <small>Configure a provider whose name or model identifies a model router.</small>
        )}
      </label>
      <BudgetFields value={budget} onChange={setBudget} />
      <SubmitButton submitting={submitting} disabled={!provider} label="Start routed run" />
    </form>
  );
}

function worker(index: number, provider: string, role: string): WorkerInput {
  return {
    id: `worker-${index}`,
    provider,
    role,
    instructions: "Research the question and cite evidence.",
  };
}

function FormIntro({ title, text }: { title: string; text: string }) {
  return (
    <div className="form-intro">
      <h2>{title}</h2>
      <p>{text}</p>
    </div>
  );
}

function SubmitButton({
  submitting,
  disabled = false,
  label,
}: {
  submitting: boolean;
  disabled?: boolean;
  label: string;
}) {
  return (
    <div className="form-actions">
      <button className="button primary" type="submit" disabled={submitting || disabled}>
        {submitting ? "Creating durable run…" : label}
      </button>
      <span className="durability-note">Journaled before execution</span>
    </div>
  );
}

function uniqueProviders(providers: ProviderHealth[]) {
  return providers.filter(
    (provider, index) =>
      provider.name.length > 0 &&
      providers.findIndex((candidate) => candidate.name === provider.name) === index,
  );
}

function preferredProvider(providers: ProviderHealth[], fallbackIndex = 0, preferOllama = false) {
  if (preferOllama) {
    const ollama = providers.find((provider) => provider.name.toLowerCase() === "ollama");
    if (ollama) return ollama.name;
  }
  return providers[fallbackIndex % Math.max(providers.length, 1)]?.name ?? "";
}

function useProviderChoice(
  providers: ProviderHealth[],
  fallbackIndex = 0,
  preferOllama = false,
) {
  const providerNames = useMemo(() => providers.map((provider) => provider.name), [providers]);
  const fallback = preferredProvider(providers, fallbackIndex, preferOllama);
  const [provider, setProvider] = useState(fallback);

  useEffect(() => {
    setProvider((current) => (providerNames.includes(current) ? current : fallback));
  }, [fallback, providerNames]);

  return [provider, setProvider] as const;
}

function isModelRouter(provider: ProviderHealth) {
  return /(?:foundry|model)[\s_-]*router/i.test(`${provider.name} ${provider.model ?? ""}`);
}

function ProviderOptions({
  providers,
  emptyLabel = "No configured providers",
}: {
  providers: ProviderHealth[];
  emptyLabel?: string;
}) {
  if (!providers.length) return <option value="">{emptyLabel}</option>;
  return providers.map((provider) => (
    <option value={provider.name} key={provider.name}>
      {provider.model ? `${provider.name} — ${provider.model}` : provider.name}
    </option>
  ));
}
