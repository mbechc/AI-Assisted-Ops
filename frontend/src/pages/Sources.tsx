import { useState } from "react";
import { Link } from "react-router-dom";
import { useAuth } from "../App";
import { api, fmtDate, READ_ONLY_URL } from "../api";
import { ErrorLine, Modal, Status, useAsync } from "../components";
import { CapabilityRow, Source } from "../types";

interface Kind { kind: string; label: string; implemented: boolean; description: string }
interface TestResult {
  status: string; version: string | null; version_source: string | null; elapsed_ms: number;
  probes: { probe: string; ok: boolean; result: string; ms: number }[]; capabilities: CapabilityRow[];
}

const EMPTY = { name: "", kind: "catalyst_center", base_url: "https://", username: "", password: "", verify_tls: false, ca_bundle: "", rate_limit_rps: 5, enabled: true };

export default function Sources() {
  const { can } = useAuth();
  const sources = useAsync(() => api.get<Source[]>("/sources"), []);
  const kinds = useAsync(() => api.get<Kind[]>("/connector-kinds"), []);
  const [edit, setEdit] = useState<(typeof EMPTY & { id?: number }) | null>(null);
  const [test, setTest] = useState<{ id: number; result?: TestResult; busy: boolean } | null>(null);
  const [caps, setCaps] = useState<{ name: string; rows: CapabilityRow[] } | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const save = async () => {
    if (!edit) return;
    setErr(null);
    try {
      const body = { ...edit, password: edit.password || undefined, ...(edit.kind === "geoip" ? { base_url: "", username: "" } : {}) };
      edit.id ? await api.put(`/sources/${edit.id}`, body) : await api.post("/sources", body);
      setEdit(null);
      sources.reload();
    } catch (e: any) {
      setErr(e.message);
    }
  };

  const runTest = async (s: Source) => {
    setTest({ id: s.id, busy: true });
    try {
      const result = await api.post<TestResult>(`/sources/${s.id}/test`);
      setTest({ id: s.id, result, busy: false });
      sources.reload();
    } catch (e: any) {
      setTest(null);
      setErr(e.message);
    }
  };

  const remove = async (s: Source) => {
    const what = s.kind === "geoip" ? "its uploaded GeoIP databases" : "its inventory snapshots and history";
    if (!confirm(`Delete data source "${s.name}"?\n\nThis removes ${what}. Open review items and queued runs for this ` +
      "source are removed; finished runs and decisions stay in the history and audit log.")) return;
    setErr(null);
    try {
      await api.del(`/sources/${s.id}`);
      sources.reload();
    } catch (e: any) {
      setErr(e.message);
    }
  };

  const showCaps = async (s: Source) => {
    const r = await api.get<{ capabilities: CapabilityRow[] }>(`/sources/${s.id}/capabilities`);
    setCaps({ name: s.name, rows: r.capabilities });
  };

  return (
    <>
      <div className="page-head">
        <div><h1>Data sources</h1><p className="sub">Catalyst Center 2.3.x and 3.x are detected automatically; the connector picks the right API per release.</p></div>
        {can("admin") && <button className="primary" onClick={() => setEdit({ ...EMPTY })}>Add source</button>}
      </div>
      <ErrorLine error={err || sources.error} />
      <div className="card">
        <table>
          <thead><tr><th>Name</th><th>URL</th><th>Release</th><th>Status</th><th>Last check</th><th /></tr></thead>
          <tbody>
            {(sources.data || []).map((s) => (
              <tr key={s.id}>
                <td><b>{s.name}</b>{!s.enabled && <span className="badge muted" style={{ marginLeft: 6 }}>disabled</span>}</td>
                <td className="small">{s.kind === "geoip" ? <span className="badge info">GeoIP (optional)</span> : s.base_url}</td>
                <td>{s.detected_version || <span className="muted">unknown</span>}<div className="small muted">{s.version_source}</div></td>
                <td>
                  <Status value={s.last_status} />
                  {s.write_refused_at && (
                    <div className="small muted" title={`Last refusal ${fmtDate(s.write_refused_at)}`}>
                      Catalyst Center refused changes (read-only account)
                    </div>
                  )}
                </td>
                <td className="small muted">{fmtDate(s.last_check_at)}</td>
                <td className="num">
                  <div className="row end">
                    {s.kind === "geoip"
                      ? <Link to={`/sources/${s.id}/geoip`}><button>Manage GeoIP</button></Link>
                      : <button onClick={() => showCaps(s)}>Capabilities</button>}
                    {can("admin") && <button onClick={() => runTest(s)} disabled={test?.busy}>{test?.busy && test.id === s.id ? "Testing..." : "Test"}</button>}
                    {can("admin") && <button onClick={() => setEdit({ ...EMPTY, ...s, password: "", ca_bundle: "" })}>Edit</button>}
                    {can("admin") && <button className="danger" onClick={() => remove(s)}>Delete</button>}
                  </div>
                </td>
              </tr>
            ))}
            {!sources.data?.length && <tr><td colSpan={6} className="muted">No data sources yet. Add your Catalyst Center.</td></tr>}
          </tbody>
        </table>
      </div>
      <div className="card">
        <h2>Connector roadmap</h2>
        <table>
          <tbody>
            {(kinds.data || []).map((k) => (
              <tr key={k.kind}><td><b>{k.label}</b></td><td className="small">{k.description}</td><td>{k.implemented ? <span className="badge ok">available</span> : <span className="badge muted">planned</span>}</td></tr>
            ))}
          </tbody>
        </table>
      </div>

      {edit && (
        <Modal onClose={() => setEdit(null)}>
          <h2>{edit.id ? "Edit" : "Add"} data source</h2>
          <ErrorLine error={err} />
          <label className="field">Name<input value={edit.name} onChange={(e) => setEdit({ ...edit, name: e.target.value })} /></label>
          <label className="field">Type
            <select value={edit.kind} onChange={(e) => setEdit({ ...edit, kind: e.target.value })}>
              {(kinds.data || []).filter((k) => k.implemented).map((k) => <option key={k.kind} value={k.kind}>{k.label}</option>)}
            </select>
          </label>
          {edit.kind === "geoip" ? (
            <p className="small muted">After saving, use "Manage GeoIP" to upload a MaxMind-format (.mmdb) database and map locations to buildings.</p>
          ) : (
            <>
              <label className="field">Base URL<input value={edit.base_url} onChange={(e) => setEdit({ ...edit, base_url: e.target.value })} /></label>
              <div className="grid cols-2">
                <label className="field">Username<input autoComplete="off" value={edit.username} onChange={(e) => setEdit({ ...edit, username: e.target.value })} /></label>
                <label className="field">Password{edit.id ? " (leave empty to keep)" : ""}<input type="password" autoComplete="new-password" value={edit.password} onChange={(e) => setEdit({ ...edit, password: e.target.value })} /></label>
              </div>
              <p className="small muted" style={{ marginTop: -4 }}>
                For an assessment, use a Catalyst Center user with the read-only OBSERVER-ROLE: Catalyst Center then refuses
                every change. No Steward setting is a substitute (<a href={READ_ONLY_URL} target="_blank" rel="noreferrer">read-only assessment</a>).
              </p>
              <label className="field">API rate limit (requests/second)<input type="number" value={edit.rate_limit_rps} onChange={(e) => setEdit({ ...edit, rate_limit_rps: Number(e.target.value) })} /></label>
              <label className="check"><input type="checkbox" checked={edit.verify_tls} onChange={(e) => setEdit({ ...edit, verify_tls: e.target.checked })} /> Verify TLS certificate</label>
              {edit.verify_tls && <label className="field" style={{ marginTop: 8 }}>CA bundle (PEM, optional)<textarea value={edit.ca_bundle} onChange={(e) => setEdit({ ...edit, ca_bundle: e.target.value })} /></label>}
            </>
          )}
          <label className="check" style={{ marginTop: 8 }}><input type="checkbox" checked={edit.enabled} onChange={(e) => setEdit({ ...edit, enabled: e.target.checked })} /> Enabled</label>
          <div className="row end" style={{ marginTop: 14 }}>
            <button onClick={() => setEdit(null)}>Cancel</button>
            <button className="primary" onClick={save}>Save</button>
          </div>
        </Modal>
      )}

      {test?.result && (
        <Modal onClose={() => setTest(null)}>
          <h2>Connection test</h2>
          <div className="kv" style={{ marginBottom: 12 }}>
            <div className="k">Status</div><div><Status value={test.result.status} /></div>
            <div className="k">Release</div><div>{test.result.version || "undetected"} <span className="small muted">via {test.result.version_source}</span></div>
            <div className="k">Duration</div><div>{(test.result.elapsed_ms / 1000).toFixed(1)}s</div>
          </div>
          <table>
            <tbody>
              {test.result.probes.map((p) => (
                <tr key={p.probe}><td>{p.probe}</td><td>{p.ok ? <span className="badge ok">ok</span> : <span className="badge warn">unavailable</span>}</td><td className="small">{p.result}</td></tr>
              ))}
            </tbody>
          </table>
          <CapTable rows={test.result.capabilities} />
        </Modal>
      )}
      {caps && (
        <Modal onClose={() => setCaps(null)}>
          <h2>API capabilities: {caps.name}</h2>
          <CapTable rows={caps.rows} />
        </Modal>
      )}
    </>
  );
}

function CapTable({ rows }: { rows: CapabilityRow[] }) {
  return (
    <>
      <h3>Endpoint selection per operation</h3>
      <table>
        <thead><tr><th>Operation</th><th>Candidate</th><th>Status</th></tr></thead>
        <tbody>
          {rows.flatMap((r) => r.candidates.map((c, n) => (
            <tr key={r.operation + c.name}>
              <td>{n === 0 ? <b>{r.operation}</b> : ""}</td>
              <td className="small"><code>{c.path}</code><div className="muted">{c.api}{!c.verified && " - unverified endpoint"}</div></td>
              <td>{r.selected === c.name ? <span className="badge ok">selected</span> : <Status value={c.status === "unsupported" ? "failed" : c.status} />}</td>
            </tr>
          )))}
        </tbody>
      </table>
    </>
  );
}
