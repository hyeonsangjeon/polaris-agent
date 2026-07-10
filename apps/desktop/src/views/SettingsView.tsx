import { useState, type FormEvent } from "react";
import type { PolarisClient } from "../api/client";
import type { ConnectionConfig } from "../api/types";

export function SettingsView({
  connection,
  onSave,
  client,
}: {
  connection: ConnectionConfig;
  onSave: (connection: ConnectionConfig) => void;
  client: PolarisClient;
}) {
  const [draft, setDraft] = useState(connection);
  const [status, setStatus] = useState<"idle" | "checking" | "healthy" | "error">("idle");
  const [message, setMessage] = useState("");

  async function check(event: FormEvent) {
    event.preventDefault();
    onSave(draft);
    setStatus("checking");
    try {
      const probeClient = client.withConfig(draft);
      const health = await probeClient.health();
      setStatus(health.status === "ok" ? "healthy" : "error");
      setMessage(health.status === "ok" ? "Daemon responded normally." : "Unexpected health response.");
    } catch {
      setStatus("error");
      setMessage("Health check failed. Verify the daemon URL and token-file access.");
    }
  }

  return (
    <div className="view narrow-view">
      <div className="view-heading">
        <div>
          <p className="context-line">Local security boundary</p>
          <h1>Connection</h1>
          <p>Polaris reads the bearer credential in Rust and never returns it to this interface.</p>
        </div>
      </div>
      <form className="settings-form" onSubmit={check}>
        <section className="form-section">
          <div className="form-section-title">
            <h2>Daemon endpoint</h2>
            <p>Only loopback and private-network HTTP(S) targets are accepted.</p>
          </div>
          <label>
            Daemon URL
            <input
              type="url"
              value={draft.daemonUrl}
              spellCheck={false}
              onChange={(event) => setDraft({ ...draft, daemonUrl: event.target.value })}
              required
            />
          </label>
          <label>
            Bearer token file
            <input
              type="text"
              value={draft.tokenFile}
              spellCheck={false}
              autoComplete="off"
              onChange={(event) => setDraft({ ...draft, tokenFile: event.target.value })}
              placeholder="/absolute/path/to/polaris.token"
              required
            />
            <small>Enter a file path, never the token value. The file is read only for each request.</small>
          </label>
        </section>
        <section className="security-boundary">
          <div className="boundary-diagram" aria-hidden="true">
            <span>React</span>
            <i>invoke</i>
            <span>Rust proxy</span>
            <i>Bearer</i>
            <span>Daemon</span>
          </div>
          <div>
            <strong>Credential boundary</strong>
            <p>
              Requests are limited to GET/POST and <code>/health</code> or <code>/v1/*</code>.
              Tokens are not stored in browser storage, the DOM, or logs.
            </p>
          </div>
        </section>
        <div className="form-actions">
          <button className="button primary" type="submit" disabled={status === "checking"}>
            {status === "checking" ? "Checking…" : "Save & check health"}
          </button>
          {status !== "idle" && (
            <p className={`connection-result ${status}`} role="status">
              <span aria-hidden="true">{status === "healthy" ? "✓" : status === "error" ? "!" : "·"}</span>
              {message || "Contacting daemon…"}
            </p>
          )}
        </div>
      </form>
    </div>
  );
}
