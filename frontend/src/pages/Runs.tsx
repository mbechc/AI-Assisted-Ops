import { useState } from "react";
import { useAuth } from "../App";
import { api, duration, fmtDate, qs } from "../api";
import LogView from "../LogView";
import { Drawer, ErrorLine, Pager, Status, useAsync } from "../components";
import { Run } from "../types";
import { summarizeStats } from "./TaskDetail";

export default function Runs() {
  const [offset, setOffset] = useState(0);
  const [open, setOpen] = useState<number | null>(null);
  const runs = useAsync(() => api.get<{ total: number; items: Run[] }>(`/runs${qs({ limit: 50, offset })}`), [offset], 10000);
  return (
    <>
      <div className="page-head"><div><h1>Run history</h1><p className="sub">Every scheduled or manual run of every Expert Task.</p></div></div>
      <ErrorLine error={runs.error} />
      <div className="card">
        <table>
          <thead><tr><th>Run</th><th>Task</th><th>Status</th><th>Trigger</th><th>Started</th><th>Duration</th><th>Result</th></tr></thead>
          <tbody>
            {(runs.data?.items || []).map((r) => (
              <tr key={r.id} className="click" onClick={() => setOpen(r.id)}>
                <td>#{r.id}</td><td>{r.task_key}</td><td><Status value={r.status} /></td>
                <td>{r.trigger}{r.triggered_by ? ` (${r.triggered_by})` : ""}</td>
                <td>{fmtDate(r.started_at)}</td><td>{duration(r.started_at, r.finished_at)}</td>
                <td className="small">{summarizeStats(r.stats)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <Pager total={runs.data?.total || 0} limit={50} offset={offset} onChange={setOffset} />
      </div>
      {open && <RunDrawer id={open} onClose={() => setOpen(null)} />}
    </>
  );
}

export function RunDrawer({ id, onClose }: { id: number; onClose: () => void }) {
  const { can } = useAuth();
  const run = useAsync(() => api.get<Run>(`/runs/${id}`), [id], 5000);
  const [live, setLive] = useState(false);
  const r = run.data;
  return (
    <Drawer onClose={onClose}>
      <div className="row"><h2 style={{ margin: 0 }}>Run #{id}</h2><div className="spacer" /><button onClick={onClose}>Close</button></div>
      {r && (
        <>
          <div className="kv" style={{ margin: "14px 0" }}>
            <div className="k">Task</div><div>{r.task_key}</div>
            <div className="k">Status</div><div><Status value={r.status} /></div>
            <div className="k">Started</div><div>{fmtDate(r.started_at)}</div>
            <div className="k">Duration</div><div>{duration(r.started_at, r.finished_at)}</div>
          </div>
          <h3>Statistics</h3>
          <pre>{JSON.stringify(r.stats, null, 2)}</pre>
          <div className="tabs" style={{ marginTop: 16 }}>
            <button className={!live ? "active" : ""} onClick={() => setLive(false)}>Run log</button>
            {can("admin") && <button className={live ? "active" : ""} onClick={() => setLive(true)}>Live container log</button>}
          </div>
          {live ? (
            <>
              <p className="small muted">Lines from the worker container tagged with run_id={id}, including library warnings and tracebacks that are not in the run log.</p>
              <LogView runId={id} short />
            </>
          ) : <pre>{r.log || "(no log yet)"}</pre>}
          {r.error && <><h3>Error</h3><pre className="err">{r.error}</pre></>}
        </>
      )}
    </Drawer>
  );
}
