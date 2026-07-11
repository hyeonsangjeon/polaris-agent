import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { createClient } from "../api/client";
import type { Transport } from "../api/types";
import { ChannelsView } from "./ChannelsView";

describe("channels view", () => {
  it("requires confirmation for unknown delivery actions and keeps token-like values out of the DOM", async () => {
    const user = userEvent.setup();
    const hidden = "xoxb-never-render-this";
    const record = {
      message: {
        platform: "slack",
        idempotency_key: "out-1",
        channel_id: "C1",
        thread_key: "slack:C1",
        text: `secret ${hidden}`,
        operation: "send_message",
        parse_mode: "plain",
        message_id: null,
        callback_query_id: null,
        disable_notification: false,
        chunk_index: 0,
        chunk_count: 1,
        metadata: {},
      },
      status: "unknown",
      content_hash: "hash",
      lease_owner: null,
      lease_expires_at: null,
      attempt_count: 1,
      remote_receipt: null,
      error: `token=${hidden}`,
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
    };
    const transport: Transport = vi.fn(async ({ path }) => {
      if (path === "/v1/channels/status") {
        return { status: 200, body: { started: true, telegram_enabled: true, slack_enabled: true, running_tasks: 4, failures: [`env=${hidden}`], unknown_outbox: 1 } };
      }
      if (path === "/v1/channels/outbox/unknown") return { status: 200, body: [record] };
      return { status: 200, body: { ...record, status: "sent" } };
    });
    const client = createClient({ daemonUrl: "http://daemon", tokenFile: "/token" }, transport);
    render(<ChannelsView client={client} demo={false} />);

    expect(await screen.findByText("Telegram")).toBeVisible();
    expect(document.body.innerHTML).not.toContain(hidden);
    await user.click(screen.getByRole("button", { name: "Mark sent" }));
    expect(transport).not.toHaveBeenCalledWith(expect.objectContaining({ path: expect.stringContaining("mark-sent") }));
    await user.type(screen.getByLabelText("Audit note"), "Confirmed in Slack history");
    await user.click(screen.getByRole("button", { name: "Confirm mark sent" }));
    await waitFor(() => expect(transport).toHaveBeenCalledWith(expect.objectContaining({
      path: "/v1/channels/outbox/out-1/mark-sent",
      body: { note: "Confirmed in Slack history" },
    })));

    await user.click(await screen.findByRole("button", { name: "Retry" }));
    expect(transport).not.toHaveBeenCalledWith(expect.objectContaining({ path: "/v1/channels/outbox/out-1/retry" }));
    await user.type(screen.getByLabelText("Audit note"), "Recipient confirmed the message is missing");
    await user.click(screen.getByRole("button", { name: "Confirm retry" }));
    await waitFor(() => expect(transport).toHaveBeenCalledWith(expect.objectContaining({
      path: "/v1/channels/outbox/out-1/retry",
      body: { note: "Recipient confirmed the message is missing" },
    })));
  });
});
