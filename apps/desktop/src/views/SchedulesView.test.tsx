import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { createClient } from "../api/client";
import { createDemoTransport } from "../api/mock";
import { demoProviders } from "../api/fixtures";
import type { Transport } from "../api/types";
import { SchedulesView } from "./SchedulesView";

describe("schedules view", () => {
  it("previews and creates cron jobs with default catchup and controls lifecycle and retry", async () => {
    const user = userEvent.setup();
    const transport: Transport = vi.fn(createDemoTransport());
    const client = createClient({ daemonUrl: "http://demo", tokenFile: "demo" }, transport);
    render(<SchedulesView client={client} providers={demoProviders} demo />);

    expect(await screen.findByText("Hourly provider health")).toBeVisible();
    expect(screen.getByText(/Default fire-once catchup/)).toBeVisible();
    await user.type(screen.getByLabelText("Name"), "Cron smoke");
    await user.type(screen.getByLabelText("Prompt"), "Check the queue");
    await user.click(screen.getByRole("button", { name: "Preview next times" }));
    expect(await screen.findByRole("list", { name: "Next scheduled times" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Create job" }));
    expect(await screen.findByText("Cron smoke")).toBeVisible();

    await user.click(screen.getAllByRole("button", { name: "Pause" })[0]);
    await waitFor(() => expect(transport).toHaveBeenCalledWith(expect.objectContaining({ path: expect.stringMatching(/^\/v1\/jobs\/.+\/pause$/) })));
    expect(await screen.findByRole("button", { name: "Resume" })).toBeVisible();
    await user.click(screen.getAllByRole("button", { name: "Resume" })[0]);
    await user.click(screen.getAllByRole("button", { name: "Cancel" })[0]);
    await user.click(screen.getByRole("button", { name: "Retry interrupted" }));

    expect(transport).toHaveBeenCalledWith(expect.objectContaining({
      path: "/v1/jobs",
      body: expect.objectContaining({
        schedule: expect.objectContaining({ kind: "cron", cron: "0 * * * *" }),
        catchup_policy: "fire_once",
        payload: expect.objectContaining({ mode: "single" }),
      }),
    }));
    expect(transport).toHaveBeenCalledWith(expect.objectContaining({ path: "/v1/jobs/runs/job-run-interrupted/retry" }));
  });

  it("sends a once wall time unchanged with its selected IANA timezone", async () => {
    const user = userEvent.setup();
    const transport: Transport = vi.fn(createDemoTransport());
    const client = createClient({ daemonUrl: "http://demo", tokenFile: "demo" }, transport);
    render(<SchedulesView client={client} providers={demoProviders} demo />);

    await screen.findByText("Hourly provider health");
    await user.type(screen.getByLabelText("Name"), "DST once");
    await user.type(screen.getByLabelText("Prompt"), "Check the transition");
    await user.click(screen.getByRole("radio", { name: "once" }));
    fireEvent.change(screen.getByLabelText("Run once at"), {
      target: { value: "2026-11-01T01:30" },
    });
    await user.clear(screen.getByLabelText("IANA timezone"));
    await user.type(screen.getByLabelText("IANA timezone"), "America/New_York");
    await user.click(screen.getByRole("button", { name: "Create job" }));

    await waitFor(() =>
      expect(transport).toHaveBeenCalledWith(
        expect.objectContaining({
          path: "/v1/jobs",
          body: expect.objectContaining({
            schedule: {
              kind: "once",
              once_at: "2026-11-01T01:30",
              timezone: "America/New_York",
            },
          }),
        }),
      ),
    );
  });
});
