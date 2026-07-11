import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { createClient } from "../api/client";
import type { ProviderHealth, Run, Transport } from "../api/types";
import { NewRunView } from "./NewRunView";

const run: Run = {
  id: "created-run",
  mode: "single",
  request: {},
  config: {},
  status: "created",
  budget: {},
  parent_run_id: null,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

const providers: ProviderHealth[] = [
  { name: "openai-prod", status: "healthy", model: "gpt-4.1", configured: true },
  { name: "ollama", status: "healthy", model: "qwen3", configured: true },
  {
    name: "foundry-router",
    status: "healthy",
    model: "model-router",
    configured: true,
  },
];

function setup() {
  const transport: Transport = vi.fn(async () => ({ status: 202, body: run }));
  const client = createClient(
    { daemonUrl: "http://127.0.0.1:8765", tokenFile: "/token-file" },
    transport,
  );
  const onCreated = vi.fn();
  render(<NewRunView client={client} providers={providers} onCreated={onCreated} />);
  return { transport, onCreated };
}

describe("new run strategies", () => {
  it("defaults to configured Ollama and submits its exact backend key", async () => {
    const user = userEvent.setup();
    const { transport } = setup();
    await user.type(screen.getByLabelText("Prompt"), "Inspect the incident timeline");
    await user.click(screen.getByRole("button", { name: "Start single run" }));

    await waitFor(() =>
      expect(transport).toHaveBeenCalledWith(
        expect.objectContaining({
          method: "POST",
          path: "/v1/runs/single",
          body: expect.objectContaining({
            prompt: "Inspect the incident timeline",
            provider: "ollama",
            schedule: true,
          }),
        }),
      ),
    );
    const calls = vi.mocked(transport).mock.calls;
    const body = calls[calls.length - 1]?.[0].body;
    expect(body).not.toHaveProperty("profile_id");
    expect(body).not.toHaveProperty("subject_key");
  });

  it("submits optional profile and subject memory context for single runs", async () => {
    const user = userEvent.setup();
    const { transport } = setup();
    await user.click(screen.getByText("Optional memory context"));
    await user.type(screen.getByLabelText("Profile"), "operations");
    await user.type(screen.getByLabelText("Subject"), "incident-42");
    await user.type(screen.getByLabelText("Prompt"), "Summarize known constraints");
    await user.click(screen.getByRole("button", { name: "Start single run" }));

    await waitFor(() =>
      expect(transport).toHaveBeenCalledWith(
        expect.objectContaining({
          path: "/v1/runs/single",
          body: expect.objectContaining({
            profile_id: "operations",
            subject_key: "incident-42",
          }),
        }),
      ),
    );
  });

  it("submits workers, verifier, synthesizer, and concurrency to fan-out", async () => {
    const user = userEvent.setup();
    const { transport } = setup();
    await user.click(screen.getByRole("radio", { name: /Local Fan-out/ }));
    await user.type(screen.getByLabelText("Research question"), "Which rollback is safest?");
    await user.click(screen.getByRole("button", { name: "Start 2-worker fan-out" }));

    await waitFor(() =>
      expect(transport).toHaveBeenCalledWith(
        expect.objectContaining({
          path: "/v1/runs/fanout",
          body: expect.objectContaining({
            question: "Which rollback is safest?",
            max_workers: 2,
            verifier: "openai-prod",
            synthesizer: "ollama",
            workers: expect.arrayContaining([
              expect.objectContaining({ provider: "openai-prod", role: "Evidence analyst" }),
            ]),
          }),
        }),
      ),
    );
  });

  it("keeps model selection out of Foundry Router and submits router payload", async () => {
    const user = userEvent.setup();
    const { transport } = setup();
    await user.click(screen.getByRole("radio", { name: /Foundry Router/ }));

    expect(screen.queryByRole("group", { name: "Workers" })).not.toBeInTheDocument();
    expect(screen.getByText("Selection is observable, not configured here")).toBeVisible();
    await user.type(screen.getByLabelText("Question"), "Route this research question");
    await user.click(screen.getByRole("button", { name: "Start routed run" }));

    await waitFor(() =>
      expect(transport).toHaveBeenCalledWith(
        expect.objectContaining({
          path: "/v1/runs/foundry-router",
          body: expect.objectContaining({
            question: "Route this research question",
            provider: "foundry-router",
            schedule: true,
          }),
        }),
      ),
    );
  });
});
