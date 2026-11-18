import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useAuth } from "../App";
import { api, duration, fmtDate } from "../api";
import { ErrorLine, Level, Status, useAsync } from "../components";
import { Run, Source, Task } from "../types";
import { RunDrawer } from "./Runs";

interface Bucket {
  range: string; lower: number; approved: number; overridden: number; rejected: number; external_match: number;
  external_mismatch: number; auto: number; verify_failed: number; open: number; samples: number; accuracy: number | null;
}
interface Calibration {
  buckets: Bucket[];
  recommendation: { level: number; threshold: number; samples: number; accuracy: number; blocked_by_cap: boolean; current_level: number } | null;
  target_accuracy: number;
  min_samples: number;
}

const LEVEL_TEXT: Record<number, [string, string]> = {
  0: ["Report only", "Findings are recorded; no actions."],
  1: ["Human review", "Every item needs an operator decision."],
  2: ["Auto above threshold", "Items at or above the threshold execute automatically."],
  3: ["Full autonomy", "All items execute automatically."],
};

export default function TaskDetail() {
  const { key = "" } = useParams();
  const { can } = useAuth();
  const task = useAsync(() => api.get<Task>(`/tasks/${key}`), [key], 10000);
  const [tab, setTab] = useState<"config" | "runs" | "calibration">("config");
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const t = task.data;

  const runNow = async () => {
    setErr(null);
    try {
      const r = await api.post<Run>(`/tasks/${key}/run`);
      setMsg(`Run ${r.id} queued.`);
      task.reload();
    } catch (e: any) {
      setErr(e.message);
    }
  };

  if (!t) return <ErrorLine error={task.error} />;
  return (
    <>
      <div className="page-head">
        <div>
          <p className="sub"><Link to="/tasks">Recurring tasks</Link> / {t.category}</p>
          <h1>{t.name}</h1>
          <p className="sub" style={{ maxWidth: 820 }}>{t.description}</p>
          <p className="small muted" style={{ marginTop: 6 }}>
            Runs on <Link to="/runners">worker-{t.worker}</Link>
            {t.optional_sources?.length ? <> - optional sources: {t.optional_sources.join(", ")} (used when configured)</> : null}
          </p>
        </div>
        <div className="row">
          <Link to={`/review?task=${t.key}`}><button>Review queue ({t.counts.proposed || 0})</button></Link>
          {can("operator") && <button className="primary" onClick={runNow}>Run now</button>}
        </div>
      </div>
      {msg && <div className="banner info">{msg}</div>}
      <ErrorLine error={err} />
      {t.key === "site_hierarchy_drift" && <BaselinePanel task={t} onDone={(m) => setMsg(m)} />}
      <div className="grid cols-4">
        <div className="card stat"><div className="v"><Level level={t.autonomy.level} /></div><div className="l">Effective autonomy{t.autonomy.limited_by ? ` (limited by ${t.autonomy.limited_by})` : ""}</div></div>
        <div className="card stat"><div className="v">{t.counts.proposed || 0}</div><div className="l">Awaiting review</div></div>
        <div className="card stat"><div className="v">{t.counts.done || 0}</div><div className="l">Executed</div></div>
        <div className="card stat"><div className="v">{t.counts.failed || 0}</div><div className="l">Failed</div></div>
      </div>
      <div className="tabs">
        {(["config", "runs", "calibration"] as const).map((x) => (
          <button key={x} className={tab === x ? "active" : ""} onClick={() => setTab(x)}>
            {x === "config" ? "Configuration and autonomy" : x === "runs" ? "Runs" : "Confidence calibration"}
          </button>
        ))}
      </div>
      {tab === "config" && <Config task={t} onSaved={(nt) => { task.setData(nt); setMsg("Saved."); }} />}
      {tab === "runs" && <TaskRuns taskKey={t.key} />}
      {tab === "calibration" && <CalibrationView task={t} />}
    </>
  );
}

interface BaselineState {
  baseline: { id: number; approved_at: string; approved_by: string; sites: number } | null;
  current: { taken_at: string; sites: number } | null;
}

function BaselinePanel({ task, onDone }: { task: Task; onDone: (m: string) => void }) {
  const { can } = useAuth();
  const sources = useAsync(() => api.get<Source[]>("/sources"), []);
  const sid = task.config.source_id ?? (sources.data || []).find((s) => s.kind === "catalyst_center")?.id;
  const state = useAsync(() => (sid ? api.get<BaselineState>(`/inventory/${sid}/baseline`) : Promise.resolve(null)), [sid]);
  const [err, setErr] = useState<string | null>(null);
  const s = state.data;
  const approve = async () => {
    if (!sid || !confirm("Approve the current Catalyst Center site hierarchy as the baseline? Later changes are reported against it.")) return;
    setErr(null);
    try {
      const r = await api.post<{ sites: number }>(`/inventory/${sid}/baseline`);
      onDone(`Baseline approved with ${r.sites} sites. The next run reports drift against it.`);
      state.reload();
    } catch (e: any) {
      setErr(e.message);
    }
  };
  return (
    <div className="card">
      <div className="row">
        <h2 style={{ margin: 0 }}>Approved baseline</h2>
        <div className="spacer" />
        {can("admin") && <button className="primary" disabled={!s?.current} onClick={approve}>Approve current hierarchy as baseline</button>}
      </div>
      <ErrorLine error={err || state.error} />
      <p className="small" style={{ marginBottom: 0 }}>
        {s?.baseline ? <>Baseline from {fmtDate(s.baseline.approved_at)} by {s.baseline.approved_by}: {s.baseline.sites} sites.</>
          : <span className="muted">No baseline approved yet.</span>}
        {s?.current && <> Current inventory snapshot ({fmtDate(s.current.taken_at)}): {s.current.sites} sites.</>}
      </p>
    </div>
  );
}

function Config({ task, onSaved }: { task: Task; onSaved: (t: Task) => void }) {
  const { can } = useAuth();
  const admin = can("admin");
  const sources = useAsync(() => api.get<Source[]>("/sources"), []);
  const [cfg, setCfg] = useState(task.config);
  const [params, setParams] = useState<Record<string, unknown>>(task.config.params);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => { setCfg(task.config); setParams(task.config.params); }, [task.config.updated_at]);
  const maxCap = Math.min(task.max_autonomy, 3);
  const allowed = Math.min(cfg.autonomy_cap, task.autonomy.global_cap, task.max_autonomy);

  const save = async (patch: Partial<Task["config"]> & { params?: Record<string, unknown>; clear_source?: boolean }) => {
    setErr(null);
    try {
      onSaved(await api.put<Task>(`/tasks/${task.key}`, patch));
    } catch (e: any) {
      setErr(e.message);
    }
  };

  return (
    <div className="grid cols-2">
      <div className="card">
        <h2>Autonomy</h2>
        <ErrorLine error={err} />
        <h3>Autonomy cap for this task</h3>
        <p className="small muted">The cap is the highest level this task may ever run at. It applies together with the global cap (L{task.autonomy.global_cap}) and the task's built-in maximum (L{task.max_autonomy}).</p>
        <div className="levels">
          {[0, 1, 2, 3].map((l) => (
            <div key={l} className={`level ${cfg.autonomy_cap === l ? "active" : ""} ${l > maxCap || !admin ? "capped" : ""}`}
              onClick={() => admin && l <= maxCap && save({ autonomy_cap: l })}>
              <b>L{l}</b>{LEVEL_TEXT[l][0]}
            </div>
          ))}
        </div>
        <h3>Selected level</h3>
        <div className="levels">
          {[0, 1, 2, 3].map((l) => (
            <div key={l} title={LEVEL_TEXT[l][1]} className={`level ${cfg.autonomy_level === l ? "active" : ""} ${l > cfg.autonomy_cap || !admin ? "capped" : ""}`}
              onClick={() => admin && l <= cfg.autonomy_cap && save({ autonomy_level: l })}>
              <b>L{l}</b>{LEVEL_TEXT[l][1]}
            </div>
          ))}
        </div>
        <p className="small muted" style={{ marginTop: 8 }}>
          Effective level: <Level level={task.autonomy.level} /> (highest allowed now: L{allowed}).
          {task.autonomy.kill_switch && <span className="err"> Kill switch is on: nothing executes.</span>}
        </p>
        <div className="grid cols-2" style={{ marginTop: 10 }}>
          <label className="field">Auto-execute threshold (confidence)
            <input type="number" min={50} max={100} disabled={!admin} value={cfg.auto_threshold}
              onChange={(e) => setCfg({ ...cfg, auto_threshold: Number(e.target.value) })} />
          </label>
          <label className="field">Max automatic actions per run
            <input type="number" min={0} disabled={!admin} value={cfg.max_auto_per_run}
              onChange={(e) => setCfg({ ...cfg, max_auto_per_run: Number(e.target.value) })} />
          </label>
        </div>
        <h3>Schedule and source</h3>
        <div className="grid cols-2">
          <label className="field">Cron (UTC)
            <input disabled={!admin} value={cfg.cron} onChange={(e) => setCfg({ ...cfg, cron: e.target.value })} />
          </label>
          <label className="field">Data source
            <select disabled={!admin} value={cfg.source_id ?? ""} onChange={(e) => setCfg({ ...cfg, source_id: e.target.value ? Number(e.target.value) : null })}>
              <option value="">First enabled {task.source_kind}</option>
              {(sources.data || []).filter((s) => s.kind === task.source_kind).map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
            </select>
          </label>
        </div>
        <label className="check"><input type="checkbox" disabled={!admin} checked={cfg.enabled} onChange={(e) => setCfg({ ...cfg, enabled: e.target.checked })} /> Scheduled runs enabled</label>
        {admin && (
          <div className="row end" style={{ marginTop: 12 }}>
            <button className="primary" onClick={() => save({
              auto_threshold: cfg.auto_threshold, max_auto_per_run: cfg.max_auto_per_run, cron: cfg.cron, enabled: cfg.enabled,
              ...(cfg.source_id ? { source_id: cfg.source_id } : { clear_source: true }),
            })}>Save</button>
          </div>
        )}
        <p className="small muted">Last changed {fmtDate(task.config.updated_at)}{task.config.updated_by ? ` by ${task.config.updated_by}` : ""}. All changes are audited.</p>
      </div>
      <div className="card">
        <h2>Parameters</h2>
        {task.param_specs.map((p) => (
          p.type === "bool" ? (
            <label key={p.key} className="check" style={{ marginBottom: 10 }} title={p.description}>
              <input type="checkbox" disabled={!admin} checked={Boolean(params[p.key])} onChange={(e) => setParams({ ...params, [p.key]: e.target.checked })} />
              {p.label}
            </label>
          ) : (
            <label key={p.key} className="field" title={p.description}>
              {p.label}
              <input disabled={!admin} value={String(params[p.key] ?? "")} onChange={(e) => setParams({ ...params, [p.key]: e.target.value })} />
            </label>
          )
        ))}
        {admin && <div className="row end"><button className="primary" onClick={() => save({ params })}>Save parameters</button></div>}
        <h3>Actions</h3>
        {Object.entries(task.action_types).map(([k, v]) => <div key={k} className="small"><code>{k}</code> {v}</div>)}
      </div>
    </div>
  );
}

function TaskRuns({ taskKey }: { taskKey: string }) {
  const runs = useAsync(() => api.get<{ items: Run[] }>(`/runs?task_key=${taskKey}&limit=50`), [taskKey], 10000);
  const [open, setOpen] = useState<number | null>(null);
  return (
    <div className="card">
      <table>
        <thead><tr><th>Run</th><th>Status</th><th>Trigger</th><th>Started</th><th>Duration</th><th>Result</th></tr></thead>
        <tbody>
          {(runs.data?.items || []).map((r) => (
            <tr key={r.id} className="click" onClick={() => setOpen(r.id)}>
              <td>#{r.id}</td>
              <td><Status value={r.status} /></td>
              <td>{r.trigger}{r.triggered_by ? ` (${r.triggered_by})` : ""}</td>
              <td>{fmtDate(r.started_at)}</td>
              <td>{duration(r.started_at, r.finished_at)}</td>
              <td className="small">{summarizeStats(r.stats)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {open && <RunDrawer id={open} onClose={() => setOpen(null)} />}
    </div>
  );
}

export function summarizeStats(s: Record<string, unknown>): string {
  if (!s || !Object.keys(s).length) return "";
  const pick = ["proposals", "findings", "items_created", "items_updated", "items_superseded", "items_auto_approved"];
  return pick.filter((k) => s[k] !== undefined).map((k) => `${k.replace("items_", "")}: ${s[k]}`).join(", ");
}

function CalibrationView({ task }: { task: Task }) {
  const { can } = useAuth();
  const cal = useAsync(() => api.get<Calibration>(`/tasks/${task.key}/calibration`), [task.key]);
  const c = cal.data;
  if (!c) return <ErrorLine error={cal.error} />;
  const max = Math.max(1, ...c.buckets.map((b) => b.samples + b.auto + b.open));
  const rec = c.recommendation;
  return (
    <div className="grid cols-2">
      <div className="card">
        <h2>Accuracy by confidence</h2>
        <p className="small muted">Accuracy counts operator approvals without override and assignments made outside the platform that matched the proposal. Auto-executed items are shown separately so the statistic is not circular.</p>
        <div className="histo">
          {c.buckets.map((b) => (
            <div className="col" key={b.lower} title={`${b.range}: ${b.samples} decided, ${b.auto} auto, ${b.open} open`}>
              <div className="stack" style={{ height: `${((b.samples + b.auto + b.open) / max) * 100}%` }}>
                <div style={{ flex: b.approved + b.external_match, background: "var(--ok)" }} />
                <div style={{ flex: b.overridden + b.rejected + b.external_mismatch, background: "var(--bad)" }} />
                <div style={{ flex: b.auto, background: "var(--warn)" }} />
                <div style={{ flex: b.open, background: "var(--border)" }} />
              </div>
              {b.lower}
            </div>
          ))}
        </div>
        <p className="small muted">Green: correct. Red: rejected or overridden. Amber: auto-executed. Grey: open.</p>
      </div>
      <div className="card">
        <h2>Promotion recommendation</h2>
        {rec ? (
          <>
            <p>At confidence <b>{rec.threshold}</b> or higher, <b>{(rec.accuracy * 100).toFixed(1)}%</b> of {rec.samples} decided items were correct (target {(c.target_accuracy * 100).toFixed(0)}%, minimum {c.min_samples} samples).</p>
            <p>Recommended: <Level level={2} /> with threshold {rec.threshold}.</p>
            {rec.blocked_by_cap ? (
              <div className="banner warn">The autonomy cap prevents L2 for this task. Raise the task cap (and the global cap if needed) to allow promotion.</div>
            ) : rec.current_level >= 2 ? (
              <div className="banner info">This task already runs at L{rec.current_level}.</div>
            ) : can("admin") ? (
              <button className="primary" onClick={() => api.put(`/tasks/${task.key}`, { autonomy_level: 2, auto_threshold: rec.threshold }).then(() => window.location.reload())}>
                Promote to L2 at {rec.threshold}
              </button>
            ) : null}
          </>
        ) : (
          <p className="muted">Not enough reviewed items yet. Keep reviewing; a recommendation appears once a confidence band reaches the accuracy target.</p>
        )}
        <table style={{ marginTop: 12 }}>
          <thead><tr><th>Band</th><th className="num">Decided</th><th className="num">Accuracy</th><th className="num">Auto</th><th className="num">Verify fail</th></tr></thead>
          <tbody>
            {[...c.buckets].reverse().filter((b) => b.samples || b.auto).map((b) => (
              <tr key={b.lower}><td>{b.range}</td><td className="num">{b.samples}</td><td className="num">{b.accuracy === null ? "-" : `${(b.accuracy * 100).toFixed(1)}%`}</td><td className="num">{b.auto}</td><td className="num">{b.verify_failed}</td></tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
