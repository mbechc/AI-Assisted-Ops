import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useAuth } from "../App";
import { api, fmtDate, qs } from "../api";
import { ErrorLine, useAsync } from "../components";
import { Source } from "../types";

interface GeoFile {
  id: number; sha256: string; filename: string; size: number; uploaded_at: string; uploaded_by: string; active: boolean;
  meta: { database_type?: string; build_epoch?: number; sample_networks?: number; sample_with_location?: number; sample_keys?: string[]; description?: string };
}
interface LearnKey {
  location_key: string; devices_with_site: number; devices_without_site: number;
  top_buildings: { site_id: string; site: string; devices: number }[];
  learned_site_id: string | null; learned_site: string | null; purity: number | null; mapped_site_id: string | null;
}
interface GeoState {
  source: { id: number; name: string; enabled: boolean };
  fields: string[]; default_fields: string[]; files: GeoFile[];
  mappings: { id: number; location_key: string; site_id: string; site_hierarchy: string; created_by: string; created_at: string }[];
  learning: { cc_source_id: number | null; available: boolean; devices: number; covered: number; keys: LearnKey[]; reason?: string };
}
interface SiteRow { id: string; hierarchy: string; type: string }

async function uploadFile(sourceId: number, file: File): Promise<GeoFile> {
  const res = await fetch(`/api/sources/${sourceId}/upload${qs({ filename: file.name })}`, {
    method: "POST", credentials: "same-origin", body: file,
    headers: { "Content-Type": "application/octet-stream", "X-AIOps": "1" },
  });
  const data = await res.json().catch(() => null);
  if (!res.ok) throw new Error(data?.detail || res.statusText);
  return data;
}

export default function GeoipSource() {
  const { id } = useParams();
  const sid = Number(id);
  const { can } = useAuth();
  const sources = useAsync(() => api.get<Source[]>("/sources"), []);
  const ccSources = (sources.data || []).filter((s) => s.kind === "catalyst_center");
  const [cc, setCc] = useState<number | null>(null);
  const ccId = cc ?? ccSources[0]?.id ?? null;
  const state = useAsync(() => api.get<GeoState>(`/sources/${sid}/geoip${qs({ cc_source_id: ccId })}`), [sid, ccId]);
  const sites = useAsync(() => (ccId ? api.get<SiteRow[]>(`/sources/${ccId}/sites?type=building`) : Promise.resolve([])), [ccId]);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [fields, setFields] = useState<string | null>(null);
  const [filter, setFilter] = useState("");

  const act = async (fn: () => Promise<unknown>) => {
    setErr(null);
    setBusy(true);
    try {
      await fn();
      state.reload();
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  };

  const s = state.data;
  const keys = (s?.learning.keys || []).filter((k) => !filter || k.location_key.toLowerCase().includes(filter.toLowerCase()));
  const siteName = (sid2: string | null) => (sites.data || []).find((x) => x.id === sid2)?.hierarchy || sid2 || "";

  return (
    <>
      <div className="page-head">
        <div>
          <h1>GeoIP: {s?.source.name || "..."}</h1>
          <p className="sub">Optional. Adds a location vote to the site tasks; all tasks work without it. Proposals supported only by GeoIP are capped at confidence 60 and never executed automatically.</p>
        </div>
        <Link to="/sources"><button>Back to sources</button></Link>
      </div>
      <ErrorLine error={err || state.error} />

      <div className="card">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <h2>Database versions</h2>
          {can("admin") && (
            <label className="button primary">
              {busy ? "Uploading..." : "Upload .mmdb"}
              <input type="file" accept=".mmdb" hidden disabled={busy}
                onChange={(e) => { const f = e.target.files?.[0]; e.target.value = ""; if (f) act(() => uploadFile(sid, f)); }} />
            </label>
          )}
        </div>
        <table>
          <thead><tr><th>File</th><th>Type</th><th>Sampled networks with a location</th><th>Uploaded</th><th>State</th><th /></tr></thead>
          <tbody>
            {(s?.files || []).map((f) => (
              <tr key={f.id}>
                <td><b>{f.filename}</b><div className="small muted"><code>{f.sha256.slice(0, 16)}</code> {(f.size / 1048576).toFixed(1)} MB</div></td>
                <td className="small">{f.meta.database_type}<div className="muted">{f.meta.build_epoch ? new Date(f.meta.build_epoch * 1000).toLocaleDateString() : ""}</div></td>
                <td className="small">{f.meta.sample_with_location} of {f.meta.sample_networks}<div className="muted">{(f.meta.sample_keys || []).slice(0, 4).join(", ")}</div></td>
                <td className="small">{fmtDate(f.uploaded_at)}<div className="muted">{f.uploaded_by}</div></td>
                <td>{f.active ? <span className="badge ok">active</span> : <span className="badge muted">inactive</span>}</td>
                <td className="num">
                  {can("admin") && !f.active && (
                    <div className="row end">
                      <button disabled={busy} onClick={() => act(() => api.post(`/sources/${sid}/geoip/files/${f.id}/activate`))}>Activate (roll back)</button>
                      <button disabled={busy} onClick={() => confirm(`Delete ${f.filename}?`) && act(() => api.del(`/sources/${sid}/geoip/files/${f.id}`))}>Delete</button>
                    </div>
                  )}
                </td>
              </tr>
            ))}
            {!s?.files.length && <tr><td colSpan={6} className="muted">No database uploaded yet.</td></tr>}
          </tbody>
        </table>
      </div>

      <div className="card">
        <h2>Location key fields</h2>
        <p className="small muted">Record fields tried in order to get a location key (dot paths, e.g. <code>city.names.en</code>). The first non-empty value is used.</p>
        <div className="row">
          <input style={{ flex: 1 }} value={fields ?? (s?.fields || []).join(", ")} disabled={!can("admin")}
            onChange={(e) => setFields(e.target.value)} />
          {can("admin") && fields !== null && (
            <button className="primary" onClick={() => act(async () => {
              await api.put(`/sources/${sid}/geoip/fields`, { fields: fields.split(",").map((x) => x.trim()).filter(Boolean) });
              setFields(null);
            })}>Save</button>
          )}
        </div>
      </div>

      <div className="card">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <h2>Location to building</h2>
          <div className="row">
            {ccSources.length > 1 && (
              <select value={ccId ?? ""} onChange={(e) => setCc(Number(e.target.value))}>
                {ccSources.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
              </select>
            )}
            <input placeholder="Filter keys" value={filter} onChange={(e) => setFilter(e.target.value)} />
          </div>
        </div>
        {s && !s.learning.available && <p className="muted">Learning unavailable: {s.learning.reason}.</p>}
        {s?.learning.available && (
          <p className="small muted">{s.learning.covered} of {s.learning.devices} devices have a GeoIP location. Learned mappings come from devices that already have a site; an operator mapping always wins.</p>
        )}
        <table>
          <thead><tr><th>Location key</th><th>Devices (with / without site)</th><th>Top buildings</th><th>Learned</th><th>Operator mapping</th></tr></thead>
          <tbody>
            {keys.slice(0, 500).map((k) => (
              <tr key={k.location_key}>
                <td><b>{k.location_key}</b></td>
                <td className="small">{k.devices_with_site} / {k.devices_without_site}</td>
                <td className="small">{k.top_buildings.map((b) => `${b.site.split("/").slice(-2).join("/")} (${b.devices})`).join(", ") || "-"}</td>
                <td className="small">{k.learned_site ? <>{k.learned_site.split("/").slice(-2).join("/")} <span className="muted">({Math.round((k.purity || 0) * 100)}%)</span></> : <span className="muted">not learned</span>}</td>
                <td>
                  {can("admin") ? (
                    <select value={k.mapped_site_id || ""} onChange={(e) => act(async () => {
                      const m = s?.mappings.find((x) => x.location_key === k.location_key);
                      if (e.target.value) await api.put(`/sources/${sid}/geoip/mappings`, { location_key: k.location_key, site_id: e.target.value, cc_source_id: ccId });
                      else if (m) await api.del(`/sources/${sid}/geoip/mappings/${m.id}`);
                    })}>
                      <option value="">(use learned)</option>
                      {(sites.data || []).map((b) => <option key={b.id} value={b.id}>{b.hierarchy}</option>)}
                    </select>
                  ) : (k.mapped_site_id ? siteName(k.mapped_site_id) : "-")}
                </td>
              </tr>
            ))}
            {s?.learning.available && !keys.length && <tr><td colSpan={5} className="muted">No location keys found for inventory devices.</td></tr>}
          </tbody>
        </table>
      </div>
    </>
  );
}
