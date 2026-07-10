import { useEffect, useState } from "react";
import type { PolarisClient } from "../api/client";
import { TERMINAL_STATUSES, type RunBundle } from "../api/types";
import { usePolling } from "./usePolling";

export function useRunBundle(client: PolarisClient, runId: string) {
  const [terminal, setTerminal] = useState(false);
  useEffect(() => setTerminal(false), [runId]);

  const state = usePolling<RunBundle>(
    async () => {
      const run = await client.run(runId);
      const [timeline, artifacts, approvals, replay] = await Promise.all([
        client.timeline(runId),
        client.artifacts(runId),
        client.approvals(runId),
        client.replay(runId).catch(() => null),
      ]);
      if (TERMINAL_STATUSES.has(run.status)) setTerminal(true);
      return { run, timeline, artifacts, approvals, replay };
    },
    { enabled: !terminal, intervalMs: 3000 },
  );

  return state;
}

export async function loadPendingApprovals(client: PolarisClient) {
  const runs = await client.runs();
  const groups = await Promise.all(runs.map((run) => client.approvals(run.id, true)));
  return groups.flat().filter((item) => item.status === "pending");
}
