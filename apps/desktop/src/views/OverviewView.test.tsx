import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { createClient } from "../api/client";
import { OverviewView } from "./OverviewView";

describe("overview errors", () => {
  it("renders a clear daemon error state", async () => {
    const client = createClient(
      { daemonUrl: "http://127.0.0.1:8765", tokenFile: "/missing" },
      async () => {
        throw new Error("Daemon unavailable");
      },
    );
    render(
      <OverviewView
        client={client}
        providers={[]}
        onSelectRun={() => undefined}
        onNewRun={() => undefined}
        pendingApprovals={[]}
      />,
    );

    expect(await screen.findByRole("alert")).toHaveTextContent("Unable to reach Polaris");
    expect(screen.getByRole("alert")).toHaveTextContent("Daemon unavailable");
  });
});
