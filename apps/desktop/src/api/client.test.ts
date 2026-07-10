import { describe, expect, it, vi } from "vitest";
import { createClient } from "./client";
import type { Transport } from "./types";

describe("secure client", () => {
  it("delegates credentials by file path without exposing a bearer token", async () => {
    const transport: Transport = vi.fn(async () => ({
      status: 200,
      body: { status: "ok" },
    }));
    const client = createClient(
      { daemonUrl: "http://127.0.0.1:8765", tokenFile: "/secure/polaris.token" },
      transport,
    );

    await client.health();

    const request = vi.mocked(transport).mock.calls[0][0];
    expect(request).toEqual({
      daemonUrl: "http://127.0.0.1:8765",
      tokenFile: "/secure/polaris.token",
      method: "GET",
      path: "/health",
    });
    expect(request).not.toHaveProperty("token");
    expect(request).not.toHaveProperty("authorization");
    expect(JSON.stringify(client)).not.toMatch(/bearer|authorization|token-value/i);
  });
});
