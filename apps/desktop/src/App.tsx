import { useEffect, useMemo, useState } from "react";
import { createClient } from "./api/client";
import { createDemoTransport } from "./api/mock";
import type { ConnectionConfig, Run, Transport } from "./api/types";
import { AppShell, type ViewName } from "./components/AppShell";
import { loadPendingApprovals } from "./hooks/useRunBundle";
import { usePolling } from "./hooks/usePolling";
import { ApprovalsView } from "./views/ApprovalsView";
import { EvidenceView } from "./views/EvidenceView";
import { NewRunView } from "./views/NewRunView";
import { OverviewView } from "./views/OverviewView";
import { RunDetailView } from "./views/RunDetailView";
import { SettingsView } from "./views/SettingsView";
import "./App.css";

const isEnvDemo = import.meta.env.VITE_POLARIS_DEMO === "1";

export default function App({
  transport,
  demo = isEnvDemo,
}: {
  transport?: Transport;
  demo?: boolean;
}) {
  const [connection, setConnection] = useState<ConnectionConfig>({
    daemonUrl: "http://127.0.0.1:8765",
    tokenFile: demo ? "demo-transport" : "",
  });
  const [view, setView] = useState<ViewName>("overview");
  const [selectedRun, setSelectedRun] = useState("run-8f21a9");
  const effectiveTransport = useMemo(
    () => transport ?? (demo ? createDemoTransport() : undefined),
    [demo, transport],
  );
  const client = useMemo(
    () => createClient(connection, effectiveTransport),
    [connection, effectiveTransport],
  );
  const health = usePolling(
    async () => {
      await client.health();
      return true;
    },
    { intervalMs: 8000 },
  );
  const providers = usePolling(() => client.providers(), { intervalMs: 12000 });
  const approvals = usePolling(() => loadPendingApprovals(client), { intervalMs: 5000 });

  const openRun = (id: string) => {
    setSelectedRun(id);
    setView("run");
  };
  const created = (run: Run) => openRun(run.id);

  useEffect(() => {
    const shortcut = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "n") {
        event.preventDefault();
        setView("new-run");
      }
    };
    window.addEventListener("keydown", shortcut);
    return () => window.removeEventListener("keydown", shortcut);
  }, []);

  return (
    <AppShell
      view={view}
      onNavigate={setView}
      daemonOnline={health.data === true}
      providerHealth={providers.data ?? []}
      pendingApprovals={approvals.data?.length ?? 0}
      demo={demo}
    >
      {view === "overview" && (
        <OverviewView
          client={client}
          providers={providers.data ?? []}
          onSelectRun={openRun}
          onNewRun={() => setView("new-run")}
          pendingApprovals={approvals.data ?? []}
        />
      )}
      {view === "new-run" && (
        <NewRunView client={client} providers={providers.data ?? []} onCreated={created} />
      )}
      {view === "run" && (
        <RunDetailView
          client={client}
          runId={selectedRun}
          onEvidence={() => setView("evidence")}
        />
      )}
      {view === "evidence" && <EvidenceView client={client} runId={selectedRun} />}
      {view === "approvals" && <ApprovalsView client={client} onSelectRun={openRun} />}
      {view === "settings" && (
        <SettingsView connection={connection} onSave={setConnection} client={client} />
      )}
    </AppShell>
  );
}
