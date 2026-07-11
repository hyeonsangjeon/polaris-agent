import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { createClient } from "../api/client";
import { createDemoTransport } from "../api/mock";
import type { Transport, TransportResponse } from "../api/types";
import { MemoryView } from "./MemoryView";

describe("memory view", () => {
  it("renders blocked audit metadata without raw content and supports add, search, revise, and remove", async () => {
    const user = userEvent.setup();
    const base = createDemoTransport();
    const transport: Transport = vi.fn(base);
    const client = createClient({ daemonUrl: "http://demo", tokenFile: "demo" }, transport);
    render(<MemoryView client={client} demo />);

    expect(await screen.findByText("Content withheld")).toBeVisible();
    expect(screen.getByText("Potential secret material")).toBeVisible();
    expect(screen.queryByText("[sensitive value withheld]")).not.toBeInTheDocument();
    expect(screen.queryByText("configured-secret-must-not-render")).not.toBeInTheDocument();

    await user.type(screen.getByLabelText("Memory content"), "Use a ten-minute rollback window");
    await user.click(screen.getByRole("button", { name: "Add memory" }));
    expect(await screen.findByText("Use a ten-minute rollback window")).toBeVisible();

    const search = screen.getByRole("search");
    await user.type(within(search).getByRole("searchbox"), "rollback");
    await user.click(within(search).getByRole("button", { name: "Search" }));
    expect(await screen.findByText("Use a ten-minute rollback window")).toBeVisible();

    await waitFor(() =>
      expect(screen.getByText("Use a ten-minute rollback window").closest("li")).not.toBeNull(),
    );
    const addedRow = screen.getByText("Use a ten-minute rollback window").closest("li");
    expect(addedRow).not.toBeNull();
    await user.click(within(addedRow!).getByRole("button", { name: "Revise" }));
    const edit = within(addedRow!).getByLabelText("Revised content");
    await user.clear(edit);
    await user.type(edit, "Use a fifteen-minute rollback window");
    await user.click(screen.getByRole("button", { name: "Save revision" }));
    await waitFor(() => expect(screen.queryByLabelText("Revised content")).not.toBeInTheDocument());
    expect(await screen.findByText("Use a fifteen-minute rollback window")).toBeVisible();

    const revisedRow = screen.getByText("Use a fifteen-minute rollback window").closest("li");
    expect(revisedRow).not.toBeNull();
    await user.click(within(revisedRow!).getByRole("button", { name: "Remove" }));
    expect(screen.getByText(/audit tombstone remains/i)).toBeVisible();
    await user.click(within(revisedRow!).getByRole("button", { name: "Confirm remove" }));
    await waitFor(() => expect(screen.queryByText("Use a fifteen-minute rollback window")).not.toBeInTheDocument());
  });

  it("hides prior scope data and destructive actions while the new scope fails", async () => {
    const base = createDemoTransport();
    let resolveScope: (response: TransportResponse) => void = () => undefined;
    const pendingScope = new Promise<TransportResponse>((resolve) => {
      resolveScope = resolve;
    });
    const transport: Transport = vi.fn((request) =>
      request.path.includes("profile_id=other") ? pendingScope : base(request),
    );
    const client = createClient({ daemonUrl: "http://demo", tokenFile: "demo" }, transport);
    render(<MemoryView client={client} demo={false} />);

    const oldContent = "Prefer concise operational summaries with explicit next actions.";
    expect(await screen.findByText(oldContent)).toBeVisible();
    expect(screen.getAllByRole("button", { name: "Revise" }).length).toBeGreaterThan(0);

    fireEvent.change(screen.getByLabelText("Profile"), { target: { value: "other" } });

    expect(screen.queryByText(oldContent)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Revise" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Remove" })).not.toBeInTheDocument();
    expect(screen.getByText(/Loading memory/)).toBeVisible();

    await act(async () => {
      resolveScope({ status: 503, body: { detail: "scope unavailable" } });
    });
    expect(await screen.findByText("scope unavailable")).toBeVisible();
    expect(screen.queryByText(oldContent)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Revise" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Remove" })).not.toBeInTheDocument();
  });
});
