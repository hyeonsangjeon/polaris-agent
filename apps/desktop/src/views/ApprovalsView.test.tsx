import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { createClient } from "../api/client";
import type { Approval, Run, Transport } from "../api/types";
import { ApprovalsView } from "./ApprovalsView";

const run: Run = {
  id: "run-uncertain",
  mode: "single",
  request: {},
  config: {},
  status: "paused",
  budget: {},
  parent_run_id: null,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

const approval: Approval = {
  id: "approval-uncertain",
  run_id: run.id,
  step_id: "step-deploy",
  kind: "uncertain_outcome",
  request: {
    tool: "deploy",
    target: "production/api",
    parameters: { region: "eastus", checks: ["health", "traffic"] },
  },
  status: "pending",
  decision: null,
  decision_reason: null,
  created_at: "2026-01-01T00:00:00Z",
  decided_at: null,
};

describe("uncertain approvals", () => {
  it("shows complete nested JSON and makes approval an explicit retry", async () => {
    const user = userEvent.setup();
    const transport: Transport = vi.fn(async ({ method, path }) => {
      if (method === "GET" && path === "/v1/runs") return { status: 200, body: [run] };
      if (method === "GET" && path === `/v1/runs/${run.id}/approvals?pending=true`) {
        return { status: 200, body: [approval] };
      }
      return { status: 200, body: approval };
    });
    const client = createClient(
      { daemonUrl: "http://127.0.0.1:8765", tokenFile: "/token-file" },
      transport,
    );

    render(<ApprovalsView client={client} onSelectRun={vi.fn()} />);

    expect(await screen.findByText("Retry may duplicate an external side effect")).toBeVisible();
    expect(screen.getByText(/Inspect the target system before deciding/)).toBeVisible();
    const request = screen.getByText((_, element) => {
      return (
        element?.tagName === "PRE" &&
        (element.textContent?.includes('"parameters": {') ?? false) &&
        (element.textContent?.includes('"checks": [') ?? false)
      );
    });
    expect(request).toHaveTextContent('"region": "eastus"');
    expect(screen.getByRole("button", { name: "Deny retry" })).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Retry operation" }));
    await waitFor(() =>
      expect(transport).toHaveBeenCalledWith(
        expect.objectContaining({
          method: "POST",
          path: "/v1/approvals/approval-uncertain/decision",
          body: { decision: "approved", decided_by: "desktop-operator" },
        }),
      ),
    );
  });
});
