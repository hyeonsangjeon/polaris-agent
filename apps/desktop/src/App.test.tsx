import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import App from "./App";
import type { Transport } from "./api/types";

describe("operator console", () => {
  it("renders run status, evidence, and approvals from the demo transport", async () => {
    const user = userEvent.setup();
    render(<App demo />);

    expect(screen.getAllByText("Demo data")).toHaveLength(2);
    expect(await screen.findByText("Operational overview")).toBeVisible();
    expect(await screen.findByText("Compare rollback guarantees across the proposed storage backends."))
      .toBeVisible();
    expect(screen.getAllByText("paused").length).toBeGreaterThan(0);

    await user.click(screen.getByRole("button", { name: "Evidence" }));
    expect(await screen.findByText("CLM-014")).toBeVisible();
    await user.click(screen.getByRole("tab", { name: /Disputed/ }));
    expect(screen.getByText("CLM-018")).toBeVisible();
    expect(screen.getByText("Dissent record")).toBeVisible();

    await user.click(screen.getByRole("button", { name: /Approvals/ }));
    expect(await screen.findByText("Allow write_file")).toBeVisible();
    expect(screen.getByText("Retry may duplicate an external side effect")).toBeVisible();
    await waitFor(() => expect(screen.getByText("2 pending")).toBeVisible());
  });

  it("keeps bearer token values out of frontend requests and the DOM", async () => {
    const bearerToken = "frontend-must-never-see-this-secret";
    const transport: Transport = vi.fn(async ({ path }) => {
      if (path === "/health") return { status: 200, body: { status: "ok" } };
      if (path === "/v1/providers/doctor") return { status: 200, body: {} };
      if (path === "/v1/runs") return { status: 200, body: [] };
      return { status: 200, body: [] };
    });

    render(<App transport={transport} />);

    await waitFor(() => expect(transport).toHaveBeenCalled());
    for (const [request] of vi.mocked(transport).mock.calls) {
      expect(request).not.toHaveProperty("token");
      expect(request).not.toHaveProperty("authorization");
      expect(JSON.stringify(request)).not.toContain(bearerToken);
    }
    expect(document.body).not.toHaveTextContent(bearerToken);
    expect(document.body.innerHTML).not.toContain(bearerToken);
  });
});
