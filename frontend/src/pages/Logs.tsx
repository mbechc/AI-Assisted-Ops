import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "../api";
import { ErrorLine, useAsync } from "../components";
import LogView, { serviceColor } from "../LogView";

interface Svc { service: string; state: string; health: string | null; id: string }

export default function Logs() {
  const [sp, setSp] = useSearchParams();
  const svcs = useAsync(() => api.get<Svc[]>("/admin/logs/services"), []);
  const initial = (sp.get("services") || "").split(",").filter(Boolean);
  const [selected, setSelected] = useState<string[]>(initial);
  const [tail, setTail] = useState(Number(sp.get("tail") || 200));

  useEffect(() => {
    if (!sp.has("services") && svcs.data?.length) {
      setSelected(svcs.data.filter((s) => s.service.startsWith("worker-")).map((s) => s.service));
    }
  }, [svcs.data]);

  const update = (services: string[], lines: number) => {
    setSelected(services);
    setTail(lines);
    setSp({ services: services.join(","), tail: String(lines) }, { replace: true });
  };
  const toggle = (s: string) => update(selected.includes(s) ? selected.filter((x) => x !== s) : [...selected, s], tail);
  const order = (svcs.data || []).map((s) => s.service);
  const shown = [...selected].sort((a, b) => order.indexOf(a) - order.indexOf(b));

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Container logs</h1>
          <p className="sub">Live logs from the AIOps containers (read-only, through the log proxy). Admin only; every session is audited.</p>
        </div>
      </div>
      <ErrorLine error={svcs.error} />
      <div className="card">
        <div className="row" style={{ marginBottom: 10 }}>
          <div className="chips" style={{ flex: 1 }}>
            {(svcs.data || []).map((s) => (
              <span key={s.service} className={`chip ${selected.includes(s.service) ? "on" : ""}`} onClick={() => toggle(s.service)}
                title={`${s.state}${s.health ? ` / ${s.health}` : ""}`}>
                <span className={`dot ${s.state === "running" ? (s.health && s.health !== "healthy" ? "warn" : "ok") : "bad"}`} />
                <span style={{ color: serviceColor(s.service) }}>{s.service}</span>
              </span>
            ))}
          </div>
          <label className="small muted">Last
            <select value={tail} onChange={(e) => update(selected, Number(e.target.value))} style={{ marginLeft: 6 }}>
              {[50, 200, 500, 2000].map((n) => <option key={n} value={n}>{n} lines</option>)}
            </select>
          </label>
          <button onClick={() => update(order, tail)}>All</button>
          <button onClick={() => update([], tail)}>None</button>
        </div>
        <p className="small muted" style={{ marginTop: 0 }}>Click a container to show or hide its logs.</p>
        {shown.length > 0 ? <LogView key={shown.join(",") + tail} services={shown} tail={tail} />
          : <p className="muted">No containers selected.</p>}
      </div>
    </>
  );
}
