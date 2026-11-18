import { useState } from "react";
import { Link } from "react-router-dom";
import { useAuth } from "../App";
import { api, fmtDate } from "../api";
import { ErrorLine, Status, useAsync } from "../components";
import { ContainerInfo, RunnersOverview, RunnerWorker } from "../types";
import { RunDrawer } from "./Runs";

function mb(b: number | null | undefined): string {
  if (b === null || b === undefined) return "-";
  return b >= 1 << 30 ? `${(b / (1 << 30)).toFixed(1)} GB` : `${Math.round(b / (1 << 20))} MB`;
}

function ago(ts: number | string | null | undefined): string {
  if (!ts) return "-";
  const t = typeof ts === "number" ? ts * 1000 : new Date(ts).getTime();
  const s = Math.max(0, Math.round((Date.now() - t) / 1000));
  return s < 60 ? `${s}s` : s < 3600 ? `${Math.floor(s / 60)}m` : s < 86400 ? `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m` : `${Math.floor(s / 86400)}d`;
}

function Health({ c, alive }: { c: ContainerInfo | null; alive?: boolean }) {
  const state = c ? (c.state !== "running" ? "bad" : c.health && c.health !== "healthy" ? "warn" : "ok") : alive ? "ok" : "bad";
  const text = c ? `${c.state}${c.health ? ` / ${c.health}` : ""}` : alive ? "heartbeat ok" : "no heartbeat";
  return <span><span className={`dot ${state}`} />{text}</span>;
}

function Usage({ c }: { c: ContainerInfo | null }) {
  if (!c || c.cpu_percent === null) return <span className="muted small">no metrics</span>;
  const memPct = c.mem_limit && c.mem_bytes ? Math.min(100, (c.mem_bytes / c.mem_limit) * 100) : 0;
  return (
    <div className="grid cols-2" style={{ gap: 10 }}>
      <div className="small">CPU {c.cpu_percent}%<div className="meter"><span style={{ width: `${Math.min(100, c.cpu_percent)}%` }} /></div></div>
      <div className="small">Memory {mb(c.mem_bytes)}{c.mem_limit ? ` / ${mb(c.mem_limit)}` : ""}
        <div className="meter"><span style={{ width: `${memPct}%`, background: memPct > 85 ? "var(--bad)" : undefined }} /></div></div>
    </div>
  );
}

export default function Runners() {
  const { can } = useAuth();
  const data = useAsync(() => api.get<RunnersOverview>("/runners"), [], 10000);
  const [run, setRun] = useState<number | null>(null);
  const d = data.data;
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Runners</h1>
          <p className="sub">One container per specialist domain, so a failure in one cannot stall the others. Refreshes every 10 seconds.</p>
        </div>
        {can("admin") && <Link to="/logs"><button>Container logs</button></Link>}
      </div>
      <ErrorLine error={data.error} />
      {d && (
        <>
          {d.docker_error && <div className="banner warn">{d.docker_error}. Worker heartbeats and queues are still shown.</div>}
          {!d.redis_ok && <div className="banner bad">Redis is not reachable: queues and heartbeats are unknown.</div>}
          {!d.scheduler_alive && <div className="banner bad">The scheduler has no recent heartbeat: scheduled runs will not start.</div>}
          <div className="grid two-by-two">
            {d.workers.map((w) => <WorkerCard key={w.name} w={w} onRun={setRun} canLogs={can("admin")} />)}
          </div>
          <div className="card">
            <h2>Next 24 hours</h2>
            <Timeline d={d} onRun={setRun} />
          </div>
          <div className="card">
            <h2>Platform services</h2>
            <table>
              <thead><tr><th>Service</th><th>State</th><th>Restarts</th><th>Up</th><th style={{ width: "40%" }}>Usage</th></tr></thead>
              <tbody>
                {d.services.map((c) => (
                  <tr key={c.id}>
                    <td><b>{c.service}</b>{can("admin") && <Link className="small" style={{ marginLeft: 8 }} to={`/logs?services=${c.service}`}>logs</Link>}</td>
                    <td className="small"><Health c={c} />{c.oom_killed && <span className="badge bad" style={{ marginLeft: 6 }}>OOM</span>}</td>
                    <td>{c.restarts}</td>
                    <td className="small">{ago(c.started_at)}</td>
                    <td><Usage c={c} /></td>
                  </tr>
                ))}
                {!d.services.length && <tr><td colSpan={5} className="muted">Container metrics unavailable.</td></tr>}
              </tbody>
            </table>
            {Object.values(d.legacy_queues).some((n) => n > 0) && (
              <p className="small muted">Legacy queues still draining: {Object.entries(d.legacy_queues).map(([q, n]) => `${q}=${n}`).join(", ")}</p>
            )}
          </div>
        </>
      )}
      {run && <RunDrawer id={run} onClose={() => setRun(null)} />}
    </>
  );
}

function WorkerCard({ w, onRun, canLogs }: { w: RunnerWorker; onRun: (id: number) => void; canLogs: boolean }) {
  const failed = w.runs_24h.failed || 0;
  const ok = w.runs_24h.success || 0;
  return (
    <div className="card">
      <div className="row">
        <h2 style={{ margin: 0 }}>worker-{w.name}</h2>
        <span className="small muted">{w.label}</span>
        <div className="spacer" />
        <span className="small"><Health c={w.container} alive={w.alive} /></span>
      </div>
      <div className="grid cols-4" style={{ margin: "12px 0", gap: 8 }}>
        <div className="stat"><div className="v">{w.queue_depth ?? "?"}</div><div className="l">queued ({w.queue})</div></div>
        <div className="stat"><div className="v">{w.active.length}</div><div className="l">running now</div></div>
        <div className="stat"><div className="v">{ok}</div><div className="l">succeeded 24h</div></div>
        <div className="stat"><div className="v" style={{ color: failed ? "var(--bad)" : undefined }}>{failed}</div><div className="l">failed 24h</div></div>
      </div>
      <Usage c={w.container} />
      <div className="kv small" style={{ marginTop: 10 }}>
        <div className="k">Heartbeat</div><div>{w.heartbeat ? `${ago(w.heartbeat.ts)} ago, ${w.heartbeat.hostname}, concurrency ${w.heartbeat.concurrency ?? "?"}` : "none"}</div>
        <div className="k">Up / restarts</div><div>{w.container ? `${ago(w.container.started_at)}, ${w.container.restarts} restart(s)` : w.heartbeat ? ago(w.heartbeat.started_at) : "-"}{w.restarts_seen ? ` (${w.restarts_seen} seen by heartbeat)` : ""}</div>
        <div className="k">Tasks</div><div>{w.tasks.map((t) => <Link key={t} to={`/tasks/${t}`} style={{ marginRight: 8 }}>{t}</Link>)}</div>
      </div>
      {(w.active.length > 0 || w.queued_runs.length > 0) && (
        <>
          <h3>Active</h3>
          {w.active.map((a, n) => (
            <div key={n} className="small">
              <span className="dot warn" />{a.task === "aiops.run_task" ? "analysis" : "execution"} {a.task_key || ""}
              {a.run_id && <button className="link small" style={{ marginLeft: 6 }} onClick={() => onRun(a.run_id!)}>run #{a.run_id}</button>}
              <span className="muted"> for {ago(a.started)}</span>
            </div>
          ))}
          {w.queued_runs.map((q) => (
            <div key={q.run_id} className="small muted"><span className="dot muted" />queued {q.task_key} <button className="link small" onClick={() => onRun(q.run_id)}>run #{q.run_id}</button></div>
          ))}
        </>
      )}
      {w.failures_24h.length > 0 && (
        <>
          <h3>Recent failures</h3>
          {w.failures_24h.map((f) => (
            <div key={f.run_id} className="small">
              <button className="link small" onClick={() => onRun(f.run_id)}>#{f.run_id}</button> {f.task_key} <span className="muted">{fmtDate(f.finished_at)}</span>
              <div className="err" style={{ marginLeft: 16 }}>{f.error[0]}</div>
            </div>
          ))}
        </>
      )}
      {canLogs && <div className="row end" style={{ marginTop: 8 }}><Link to={`/logs?services=worker-${w.name}`}><button>Follow logs</button></Link></div>}
    </div>
  );
}

function Timeline({ d, onRun }: { d: RunnersOverview; onRun: (id: number) => void }) {
  const start = new Date(d.now).getTime();
  const span = 24 * 3600 * 1000;
  const hours = Array.from({ length: 12 }, (_, i) => new Date(start + i * 2 * 3600 * 1000));
  return (
    <>
      <div className="timeline">
        {d.schedule.map((s) => (
          <div className="lane" key={s.task_key} style={{ opacity: s.enabled ? 1 : 0.4 }}>
            <div className="name" title={`${s.name} (${s.cron} UTC)`}>
              <Link to={`/tasks/${s.task_key}`}>{s.name}</Link> <span className="muted">{s.worker}</span>
            </div>
            <div style={{ position: "absolute", left: 200, right: 8, top: 0, bottom: 0 }}>
              {s.next.map((t) => {
                const x = ((new Date(t).getTime() - start) / span) * 100;
                return <div key={t} className="tick" style={{ left: `${x}%` }} title={new Date(t).toLocaleString()} />;
              })}
            </div>
          </div>
        ))}
      </div>
      <div className="timeline" style={{ border: "none", background: "none" }}>
        <div className="hours">{hours.map((h) => <div key={h.getTime()} style={{ flex: 1 }}>{h.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</div>)}</div>
      </div>
      <table style={{ marginTop: 12 }}>
        <thead><tr><th>Task</th><th>Worker</th><th>Cron (UTC)</th><th>Next</th><th>Last run</th></tr></thead>
        <tbody>
          {d.schedule.map((s) => (
            <tr key={s.task_key}>
              <td><Link to={`/tasks/${s.task_key}`}>{s.name}</Link>{!s.enabled && <span className="badge muted" style={{ marginLeft: 6 }}>disabled</span>}</td>
              <td className="small">{s.worker}</td>
              <td><code>{s.cron}</code></td>
              <td className="small">{s.next[0] ? fmtDate(s.next[0]) : "-"}</td>
              <td className="small">{s.last_run ? <><button className="link small" onClick={() => onRun(s.last_run!.id)}>#{s.last_run.id}</button> <Status value={s.last_run.status} /> {fmtDate(s.last_run.created_at)}</> : <span className="muted">none in 24h</span>}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}
