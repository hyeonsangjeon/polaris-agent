import type { Run } from "../api/types";
import { modeLabel, relativeTime, runTitle, shortId } from "../utils/format";
import { EmptyState } from "./StatePanel";
import { StatusBadge } from "./StatusBadge";

export function RunTable({
  runs,
  onSelect,
}: {
  runs: Run[];
  onSelect: (id: string) => void;
}) {
  if (!runs.length) {
    return (
      <EmptyState title="No durable runs yet">
        Start a run to see status, evidence, artifacts, and replay history here.
      </EmptyState>
    );
  }
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Run</th>
            <th>Strategy</th>
            <th>Status</th>
            <th>Updated</th>
            <th>
              <span className="sr-only">Open</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {runs.map((run) => (
            <tr key={run.id}>
              <td>
                <button className="run-link" type="button" onClick={() => onSelect(run.id)}>
                  <span>{runTitle(run)}</span>
                  <code>{shortId(run.id)}</code>
                </button>
              </td>
              <td>{modeLabel(run.mode)}</td>
              <td>
                <StatusBadge status={run.status} />
              </td>
              <td>{relativeTime(run.updated_at)}</td>
              <td>
                <button
                  className="icon-button"
                  type="button"
                  aria-label={`Open run ${shortId(run.id)}`}
                  onClick={() => onSelect(run.id)}
                >
                  →
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
