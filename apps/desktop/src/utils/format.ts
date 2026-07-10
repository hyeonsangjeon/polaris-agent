import type { Budget, Replay, Run } from "../api/types";

export function relativeTime(value: string): string {
  const seconds = Math.max(0, Math.round((Date.now() - new Date(value).getTime()) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

export function modeLabel(mode: string): string {
  if (mode === "fan-out") return "Local Fan-out";
  if (mode === "foundry-router") return "Foundry Router";
  return "Single";
}

export function runTitle(run: Run): string {
  const request = run.request;
  return String(request.question ?? request.prompt ?? "Untitled run");
}

export function shortId(id: string): string {
  return id.length > 12 ? `${id.slice(0, 12)}…` : id;
}

export function compact(value?: number): string {
  if (value === undefined) return "—";
  return Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 }).format(value);
}

export function usd(microUsd?: number): string {
  if (microUsd === undefined) return "—";
  return Intl.NumberFormat("en", { style: "currency", currency: "USD" }).format(
    microUsd / 1_000_000,
  );
}

export function budgetPercent(budget: Budget, used: keyof Budget, limit: keyof Budget): number {
  const usedValue = Number(budget[used] ?? 0);
  const limitValue = Number(budget[limit] ?? 0);
  return limitValue > 0 ? Math.min(100, (usedValue / limitValue) * 100) : 0;
}

export function actualModels(replay: Replay | null): string[] {
  if (!replay) return [];
  const direct = replay.actual_models ?? [];
  if (direct.length) return [...new Set(direct)];
  const cost = replay.cost ? Object.values(replay.cost.actual_models).flat() : [];
  return [...new Set(cost)];
}
