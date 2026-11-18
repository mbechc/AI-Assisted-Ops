import { useState } from "react";
import { api, fmtDate, qs } from "../api";
import { ErrorLine, Pager, useAsync } from "../components";

interface Entry { id: number; ts: string; actor: string; action: string; object_type: string | null; object_id: string | null; details: Record<string, unknown> }

export default function Audit() {
  const [offset, setOffset] = useState(0);
  const [action, setAction] = useState("");
  const [q, setQ] = useState("");
  const [open, setOpen] = useState<number | null>(null);
  const data = useAsync(() => api.get<{ total: number; items: Entry[] }>(`/audit${qs({ limit: 100, offset, action, q })}`), [offset, action, q]);
  return (
    <>
      <div className="page-head"><div><h1>Audit log</h1><p className="sub">Every decision, autonomy change, execution and configuration change.</p></div></div>
      <ErrorLine error={data.error} />
      <div className="card">
        <div className="row" style={{ marginBottom: 10 }}>
          <select value={action} onChange={(e) => { setAction(e.target.value); setOffset(0); }}>
            <option value="">All actions</option>
            <option value="items.">Item decisions and execution</option>
            <option value="task.">Task configuration and autonomy</option>
            <option value="settings.">Global settings</option>
            <option value="source.">Data sources</option>
            <option value="auth.">Authentication</option>
            <option value="user.">Users</option>
          </select>
          <input placeholder="Actor or object" value={q} onChange={(e) => { setQ(e.target.value); setOffset(0); }} />
        </div>
        <table>
          <thead><tr><th>Time</th><th>Actor</th><th>Action</th><th>Object</th><th>Details</th></tr></thead>
          <tbody>
            {(data.data?.items || []).map((e) => (
              <tr key={e.id} className="click" onClick={() => setOpen(open === e.id ? null : e.id)}>
                <td className="small">{fmtDate(e.ts)}</td><td>{e.actor}</td><td><code>{e.action}</code></td>
                <td className="small">{e.object_type ? `${e.object_type} ${e.object_id ?? ""}` : ""}</td>
                <td className="small">{open === e.id ? <pre>{JSON.stringify(e.details, null, 2)}</pre> : <span className="muted">{JSON.stringify(e.details).slice(0, 90)}</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <Pager total={data.data?.total || 0} limit={100} offset={offset} onChange={setOffset} />
      </div>
    </>
  );
}
