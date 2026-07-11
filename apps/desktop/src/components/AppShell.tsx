import type { ReactNode } from "react";
import type { ProviderHealth } from "../api/types";

export type ViewName =
  | "overview"
  | "new-run"
  | "run"
  | "evidence"
  | "approvals"
  | "memory"
  | "schedules"
  | "channels"
  | "settings";

const nav: Array<{ id: ViewName; label: string; key: string }> = [
  { id: "overview", label: "Overview", key: "O" },
  { id: "new-run", label: "New run", key: "N" },
  { id: "evidence", label: "Evidence", key: "E" },
  { id: "approvals", label: "Approvals", key: "A" },
  { id: "memory", label: "Memory", key: "M" },
  { id: "schedules", label: "Schedules", key: "J" },
  { id: "channels", label: "Channels", key: "C" },
];

export function AppShell({
  children,
  view,
  onNavigate,
  daemonOnline,
  providerHealth,
  pendingApprovals,
  demo,
}: {
  children: ReactNode;
  view: ViewName;
  onNavigate: (view: ViewName) => void;
  daemonOnline: boolean;
  providerHealth: ProviderHealth[];
  pendingApprovals: number;
  demo: boolean;
}) {
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <button className="wordmark" type="button" onClick={() => onNavigate("overview")}>
          <span className="brand-mark" aria-hidden="true">
            <i />
          </span>
          <span>
            <strong>Polaris</strong>
            <small>Agent control</small>
          </span>
        </button>
        <nav aria-label="Primary">
          {nav.map((item) => (
            <button
              key={item.id}
              type="button"
              className={view === item.id ? "active" : ""}
              aria-current={view === item.id ? "page" : undefined}
              onClick={() => onNavigate(item.id)}
            >
              <span className="nav-glyph" aria-hidden="true">
                {item.key}
              </span>
              {item.label}
              {item.id === "approvals" && pendingApprovals > 0 && (
                <span className="nav-count">{pendingApprovals}</span>
              )}
            </button>
          ))}
        </nav>
        <div className="sidebar-footer">
          {demo && <span className="demo-badge">Demo data</span>}
          <button
            type="button"
            className={view === "settings" ? "active" : ""}
            onClick={() => onNavigate("settings")}
          >
            <span className="nav-glyph" aria-hidden="true">
              S
            </span>
            Connection
          </button>
          <div className="build-id">
            <span>Local console</span>
            <code>v0.2.0</code>
          </div>
        </div>
      </aside>
      <div className="workspace">
        <header className="topbar">
          <div className="top-status">
            <span className={`pulse ${daemonOnline ? "online" : "offline"}`} aria-hidden="true" />
            <span>
              Daemon <strong>{daemonOnline ? "connected" : "offline"}</strong>
            </span>
          </div>
          {demo && <span className="top-demo">Demo data</span>}
          <div className="provider-strip" aria-label="Provider health">
            {providerHealth.slice(0, 3).map((provider) => (
              <span key={provider.name}>
                <i className={provider.status} aria-hidden="true" />
                {provider.name}
              </span>
            ))}
          </div>
          <button className="new-run-shortcut" type="button" onClick={() => onNavigate("new-run")}>
            New run <kbd>⌘ N</kbd>
          </button>
        </header>
        <main className="main-content" id="main-content">
          {children}
        </main>
      </div>
    </div>
  );
}
