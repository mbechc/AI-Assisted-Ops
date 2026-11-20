import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../App";
import { api } from "../api";
import { ErrorLine, useAsync } from "../components";
import { Connectivity } from "../types";
import { ConnectivityChip } from "../UpdatesSection";

interface Kpi {
  key: string;
  name: string;
  category: string;
  proposed: number;
  reported: number;
  open: number;
  approved: number;
  new_24h: number;
  executed_24h: number;
  resolved_24h: number;
  fixed_24h: number;
  open_24h_ago: number;
  change_24h: number;
  history: number[];
  history_step_hours: number;
}

function trend(change: number): "better" | "worse" | "flat" {
  return change < 0 ? "better" : change > 0 ? "worse" : "flat";
}

function Backdrop({ series }: { series: number[] }) {
  if (series.length < 2) return null;
  const max = Math.max(1, ...series);
  const pts = series.map((v, i) => `${(i / (series.length - 1)) * 100},${100 - (v / max) * 88}`);
  return (
    <svg className="kpi-graph" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden>
      <polygon points={`0,100 ${pts.join(" ")} 100,100`} />
      <polyline points={pts.join(" ")} vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

export default function Dashboard() {
  const kpis = useAsync(() => api.get<Kpi[]>("/items/kpis"), [], 30000);
  const net = useAsync(() => api.get<Connectivity>("/system/connectivity"), [], 60000);
  const { can } = useAuth();
  const all = kpis.data || [];
  const nav = useNavigate();
  const open = all.reduce((n, k) => n + k.open, 0);
  const change = all.reduce((n, k) => n + k.change_24h, 0);

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Dashboard</h1>
          <p className="sub">
            Inbox: {open} open, {change === 0 ? "unchanged" : change < 0 ? `${-change} fewer` : `${change} more`} than
            24 hours ago. The graph behind each category shows the last 30 days. Click a category to review it.
          </p>
        </div>
        {net.data && <ConnectivityChip c={net.data} onClick={can("admin") ? () => nav("/settings#updates") : undefined} />}
      </div>
      <ErrorLine error={kpis.error} />
      <div className="kpi-grid">
        {all.map((k) => {
          const t = trend(k.change_24h);
          return (
            <div key={k.key} className={`card kpi ${t}`} role="link" tabIndex={0}
              onClick={() => nav(`/inbox?task=${k.key}&status=${k.reported > k.proposed ? "reported" : "proposed"}`)}
              onKeyDown={(e) => e.key === "Enter" && (e.currentTarget as HTMLElement).click()}>
              <Backdrop series={k.history} />
              <div className="kpi-body">
                <div className="kpi-name">{k.name}</div>
                <div className="kpi-value">{k.open}</div>
                <div className="kpi-label">{k.reported > k.proposed ? "open findings" : "awaiting review"}
                  {k.approved > 0 && <> · {k.approved} approved, executing</>}</div>
                <div className="kpi-trend" title={`24 hours ago: ${k.open_24h_ago} open. Since then ${k.new_24h} new, ` +
                  `${k.executed_24h} executed, ${k.resolved_24h} resolved in Catalyst Center.`}>
                  {t === "better" && <><span className="arrow">▲</span>{-k.change_24h} fewer than 24 h ago</>}
                  {t === "worse" && <><span className="arrow">▼</span>{k.change_24h} more than 24 h ago</>}
                  {t === "flat" && <>No change in 24 h</>}
                </div>
              </div>
            </div>
          );
        })}
      </div>
      {kpis.data && !all.length && <p className="muted">No review categories.</p>}
      <ShiftCard />
    </>
  );
}

interface Shift {
  hours: number;
  lines: string[];
  ai_mode: string;
  ai: null | { provider_label: string; narrative: string | null; rejected_reason: string | null; generated_at: string };
}

function ShiftCard() {
  const { can } = useAuth();
  const [hours, setHours] = useState(12);
  const shift = useAsync(() => api.get<Shift>(`/digest/shift?hours=${hours}`), [hours], 60000);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const s = shift.data;
  const write = async () => {
    setBusy(true);
    setErr(null);
    try {
      shift.setData(await api.post<Shift>("/digest/shift/ai", { hours }));
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  };
  const aiOn = s && s.ai_mode !== "none";
  return (
    <div className="card" style={{ marginTop: 16 }}>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h2 style={{ margin: 0 }}>Since last shift
          {aiOn && <span className="badge ai-on" style={{ marginLeft: 8 }}>{s!.ai_mode === "local" ? "Local AI" : "Cloud AI"}</span>}
        </h2>
        <div className="row">
          <select value={hours} onChange={(e) => setHours(Number(e.target.value))}>
            {[8, 12, 24].map((h) => <option key={h} value={h}>Last {h} hours</option>)}
          </select>
          {aiOn && can("operator") && !s!.ai?.narrative && (
            <button disabled={busy} onClick={write}>{busy ? "Writing..." : "Write summary with AI"}</button>
          )}
        </div>
      </div>
      <ErrorLine error={err || shift.error} />
      {s?.ai?.narrative && (
        <>
          <p style={{ marginTop: 10 }}>{s.ai.narrative}</p>
          <p className="small muted">Written by {s.ai.provider_label} from the facts below.</p>
        </>
      )}
      {s?.ai?.rejected_reason && <div className="banner warn">{s.ai.rejected_reason}</div>}
      {s && <ul className={s.ai?.narrative ? "small muted" : ""} style={{ marginTop: 10 }}>{s.lines.map((l, i) => <li key={i}>{l}</li>)}</ul>}
    </div>
  );
}
