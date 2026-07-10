import {
  demoApprovals,
  demoArtifacts,
  demoProviders,
  demoReplay,
  demoRuns,
  demoTimeline,
} from "./fixtures";
import type { Approval, Run, Transport } from "./types";

const copy = <T>(value: T): T => structuredClone(value);

export function createDemoTransport(): Transport {
  const runs = copy(demoRuns);
  const approvals = copy(demoApprovals);

  return async ({ method, path, body }) => {
    await new Promise((resolve) => window.setTimeout(resolve, 90));
    const route = path.split("?")[0];

    if (method === "GET" && route === "/health") {
      return ok({ status: "ok" });
    }
    if (method === "GET" && route === "/v1/runs") return ok(copy(runs));
    if (method === "GET" && route === "/v1/providers/doctor") {
      return ok(Object.fromEntries(demoProviders.map((item) => [item.name, item])));
    }
    if (method === "POST" && route.startsWith("/v1/runs/") && isSubmission(route)) {
      const created = createRun(route, body as Record<string, unknown>);
      runs.unshift(created);
      return accepted(copy(created));
    }

    const runMatch = route.match(/^\/v1\/runs\/([^/]+)(?:\/(.*))?$/);
    if (runMatch) {
      const [, id, resource] = runMatch;
      const run = runs.find((item) => item.id === id);
      if (!run) return missing();
      if (method === "GET" && !resource) return ok(copy(run));
      if (method === "GET" && resource === "timeline") return ok(copy(demoTimeline));
      if (method === "GET" && resource === "artifacts") return ok(copy(demoArtifacts));
      if (method === "GET" && resource === "replay") return ok(copy(demoReplay));
      if (method === "GET" && resource === "approvals") {
        return ok(copy(approvals.filter((item) => item.run_id === id)));
      }
      if (method === "POST" && resource === "resume") {
        run.status = "running";
        return ok(copy(run));
      }
      if (method === "POST" && resource === "cancel") {
        run.status = "cancelled";
        return ok(copy(run));
      }
    }

    const approvalMatch = route.match(/^\/v1\/approvals\/([^/]+)(?:\/decision)?$/);
    if (method === "POST" && approvalMatch) {
      const item = approvals.find((approval) => approval.id === approvalMatch[1]);
      if (!item) return missing();
      const decision = (body as { decision: Approval["status"] }).decision;
      item.status = decision;
      item.decision = decision;
      item.decided_at = new Date().toISOString();
      return ok(copy(item));
    }
    return missing();
  };
}

function isSubmission(path: string) {
  return ["/v1/runs/single", "/v1/runs/fanout", "/v1/runs/foundry-router"].includes(path);
}

function createRun(path: string, body: Record<string, unknown>): Run {
  const mode =
    path === "/v1/runs/fanout"
      ? "fan-out"
      : path === "/v1/runs/foundry-router"
        ? "foundry-router"
        : "single";
  const timestamp = new Date().toISOString();
  return {
    id: `run-${Math.random().toString(16).slice(2, 8)}`,
    mode,
    request: copy(body),
    config: {},
    status: "created",
    budget: (body.budget as Run["budget"]) ?? {},
    parent_run_id: null,
    created_at: timestamp,
    updated_at: timestamp,
  };
}

const ok = (body: unknown) => ({ status: 200, body });
const accepted = (body: unknown) => ({ status: 202, body });
const missing = () => ({ status: 404, body: { error: "not_found", detail: "Resource not found" } });
