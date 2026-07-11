import { useEffect, useState, type FormEvent } from "react";
import type { PolarisClient } from "../api/client";
import type { MemoryEntry, MemoryKind, MemoryScope } from "../api/types";
import { ErrorState, LoadingState } from "../components/StatePanel";
import { usePolling } from "../hooks/usePolling";
import { relativeTime } from "../utils/format";

const kinds: MemoryKind[] = ["preference", "fact", "user", "agent"];

export function MemoryView({ client, demo }: { client: PolarisClient; demo: boolean }) {
  const [scope, setScope] = useState<MemoryScope>({ profile_id: "default", subject_key: "local" });
  const [search, setSearch] = useState("");
  const [appliedSearch, setAppliedSearch] = useState("");
  const [content, setContent] = useState("");
  const [kind, setKind] = useState<MemoryKind>("preference");
  const [editing, setEditing] = useState<MemoryEntry | null>(null);
  const [editContent, setEditContent] = useState("");
  const [confirmRemove, setConfirmRemove] = useState<string | null>(null);
  const [busy, setBusy] = useState("");
  const [actionError, setActionError] = useState("");
  const scopeKey = `${scope.profile_id}\u0000${scope.subject_key}\u0000${appliedSearch.trim()}`;
  const poll = usePolling(
    async () => {
      const requestKey = scopeKey;
      const entries = !appliedSearch.trim()
        ? await client.memory(scope)
        : (await client.searchMemory(scope, appliedSearch.trim())).map((hit) => hit.entry);
      return { requestKey, entries };
    },
    { intervalMs: 10_000, cacheKey: scopeKey },
  );

  useEffect(() => {
    setEditing(null);
    setConfirmRemove(null);
    setActionError("");
  }, [scope.profile_id, scope.subject_key, appliedSearch]);

  async function add(event: FormEvent) {
    event.preventDefault();
    setBusy("add");
    setActionError("");
    try {
      await client.addMemory({
        ...scope,
        content,
        kind,
        trust_level: "user_asserted",
        provenance_session_id: "desktop-operator",
      });
      setContent("");
      setAppliedSearch("");
      setSearch("");
      poll.refresh();
    } catch (error) {
      setActionError(message(error));
    } finally {
      setBusy("");
    }
  }

  async function revise(event: FormEvent) {
    event.preventDefault();
    if (!editing) return;
    setBusy(editing.id);
    setActionError("");
    try {
      await client.reviseMemory(editing.id, {
        profile_id: editing.profile_id,
        subject_key: editing.subject_key,
        content: editContent,
        expected_revision: editing.revision,
        expected_hash: editing.content_hash,
      });
      setEditing(null);
      poll.refresh();
    } catch (error) {
      setActionError(message(error));
    } finally {
      setBusy("");
    }
  }

  async function remove(entry: MemoryEntry) {
    setBusy(entry.id);
    setActionError("");
    try {
      await client.removeMemory(entry.id, {
        profile_id: entry.profile_id,
        subject_key: entry.subject_key,
        expected_revision: entry.revision,
        expected_hash: entry.content_hash,
      });
      setConfirmRemove(null);
      poll.refresh();
    } catch (error) {
      setActionError(message(error));
    } finally {
      setBusy("");
    }
  }

  const currentResult = poll.data?.requestKey === scopeKey ? poll.data : null;
  const entries = currentResult?.entries ?? [];
  const currentScopeReady = currentResult !== null && poll.error === null;
  return (
    <div className="view">
      <div className="view-heading">
        <div>
          <p className="context-line">Curated recall{demo ? " / synthetic data" : ""}</p>
          <h1>Memory</h1>
          <p>Manage scoped claims that may be recalled for future single-agent runs.</p>
        </div>
        {demo && <span className="demo-label">Synthetic memory</span>}
      </div>

      <div className="safety-notice" role="note">
        <span aria-hidden="true">!</span>
        <div>
          <strong>Blocked content stays audit-visible but is excluded from prompts.</strong>
          <p>Potential secrets are withheld here. Entry state, reason, provenance, and revision remain visible.</p>
        </div>
      </div>

      <section className="memory-controls" aria-labelledby="memory-scope-heading">
        <div className="section-heading">
          <div>
            <h2 id="memory-scope-heading">Memory scope</h2>
            <p>Profile and subject form an explicit isolation boundary.</p>
          </div>
        </div>
        <div className="control-fields">
          <label>
            Profile
            <input
              value={scope.profile_id}
              onChange={(event) => setScope({ ...scope, profile_id: event.target.value })}
              required
            />
          </label>
          <label>
            Subject
            <input
              value={scope.subject_key}
              onChange={(event) => setScope({ ...scope, subject_key: event.target.value })}
              required
            />
          </label>
          <form
            className="search-form"
            role="search"
            onSubmit={(event) => {
              event.preventDefault();
              setAppliedSearch(search);
            }}
          >
            <label>
              Search current entries
              <span className="input-action">
                <input
                  type="search"
                  value={search}
                  onChange={(event) => setSearch(event.target.value)}
                  placeholder="Search memory…"
                />
                <button className="button secondary" type="submit">
                  Search
                </button>
              </span>
            </label>
          </form>
        </div>
      </section>

      {actionError && <div className="inline-error" role="alert">{actionError}</div>}

      <div className="memory-layout">
        <section className="section-block" aria-labelledby="memory-list-heading">
          <div className="section-heading">
            <div>
              <h2 id="memory-list-heading">{appliedSearch ? "Search results" : "Current entries"}</h2>
              <p>{entries.length} in {scope.profile_id} / {scope.subject_key}</p>
            </div>
            {appliedSearch && (
              <button
                className="text-button"
                type="button"
                onClick={() => {
                  setSearch("");
                  setAppliedSearch("");
                }}
              >
                Clear search
              </button>
            )}
          </div>
          {poll.loading && !currentResult ? (
            <LoadingState label="Loading memory" />
          ) : poll.error && !currentResult ? (
            <ErrorState error={poll.error} onRetry={poll.refresh} />
          ) : entries.length ? (
            <ul className="memory-list">
              {entries.map((entry) => (
                <li key={entry.id} className={entry.blocked_reason ? "blocked" : ""}>
                  <div className="memory-meta">
                    <span className={`memory-kind kind-${entry.kind}`}>{entry.kind}</span>
                    <span>{entry.trust_level.replace("_", " ")}</span>
                    <code>r{entry.revision}</code>
                    {entry.blocked_reason && <strong>Blocked</strong>}
                    <time dateTime={entry.updated_at}>{relativeTime(entry.updated_at)}</time>
                  </div>
                  {entry.blocked_reason ? (
                    <div className="blocked-memory">
                      <strong>Content withheld</strong>
                      <p>{entry.blocked_reason}</p>
                    </div>
                  ) : editing?.id === entry.id ? (
                    <form className="memory-edit" onSubmit={revise}>
                      <label>
                        Revised content
                        <textarea
                          value={editContent}
                          onChange={(event) => setEditContent(event.target.value)}
                          required
                        />
                      </label>
                      <div className="compact-actions">
                        <button className="button primary" type="submit" disabled={busy === entry.id}>
                          Save revision
                        </button>
                        <button className="button secondary" type="button" onClick={() => setEditing(null)}>
                          Cancel
                        </button>
                      </div>
                    </form>
                  ) : (
                    <p className="memory-content">{entry.content}</p>
                  )}
                  <div className="memory-provenance">
                    <span>Provenance</span>
                    <code>
                      {entry.provenance_run_id
                        ? `run:${entry.provenance_run_id}`
                        : entry.provenance_session_id
                          ? `session:${entry.provenance_session_id}`
                          : "manual / unlinked"}
                    </code>
                    <span className="hash-label">hash {entry.content_hash.slice(0, 10)}…</span>
                  </div>
                  {!entry.blocked_reason && editing?.id !== entry.id && (
                    <div className="compact-actions">
                      <button
                        className="text-button"
                        type="button"
                        disabled={!currentScopeReady}
                        onClick={() => {
                          setEditing(entry);
                          setEditContent(entry.content);
                          setConfirmRemove(null);
                        }}
                      >
                        Revise
                      </button>
                      <button
                        className="text-button danger-text"
                        type="button"
                        disabled={!currentScopeReady}
                        onClick={() => setConfirmRemove(entry.id)}
                      >
                        Remove
                      </button>
                    </div>
                  )}
                  {confirmRemove === entry.id && (
                    <div className="inline-confirm" role="group" aria-label="Confirm memory removal">
                      <span>Remove revision {entry.revision}? The audit tombstone remains.</span>
                      <button
                        className="button danger"
                        type="button"
                        disabled={!currentScopeReady || busy === entry.id}
                        onClick={() => void remove(entry)}
                      >
                        Confirm remove
                      </button>
                      <button className="button secondary" type="button" onClick={() => setConfirmRemove(null)}>Keep</button>
                    </div>
                  )}
                </li>
              ))}
            </ul>
          ) : (
            <div className="empty-state">
              <strong>No current memory found</strong>
              <p>{appliedSearch ? "Try a broader search or clear the query." : "Add an operator-asserted entry for this scope."}</p>
            </div>
          )}
        </section>

        <aside className="section-block memory-add">
          <div className="section-heading">
            <div>
              <h2>Add memory</h2>
              <p>New desktop entries are always user asserted.</p>
            </div>
          </div>
          <form onSubmit={add}>
            <label>
              Kind
              <select value={kind} onChange={(event) => setKind(event.target.value as MemoryKind)}>
                {kinds.map((value) => <option key={value}>{value}</option>)}
              </select>
            </label>
            <label>
              Memory content
              <textarea
                value={content}
                onChange={(event) => setContent(event.target.value)}
                placeholder="Record a durable preference or fact…"
                required
              />
            </label>
            <div className="assertion-note">
              <strong>Trust: user asserted</strong>
              <p>Only add information you intend Polaris to recall in this scope. Do not paste credentials.</p>
            </div>
            <button className="button primary" type="submit" disabled={busy === "add"}>
              {busy === "add" ? "Adding…" : "Add memory"}
            </button>
          </form>
        </aside>
      </div>
    </div>
  );
}

function message(error: unknown) {
  return error instanceof Error ? error.message : "The memory operation failed.";
}
