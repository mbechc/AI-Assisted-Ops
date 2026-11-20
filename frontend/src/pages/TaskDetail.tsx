import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useAuth } from "../App";
import { api, duration, fmtDate } from "../api";
import { AiLabel, ErrorLine, Level, Status, useAsync } from "../components";
import { CRON_PRESETS, describeCron } from "../cron";
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

interface InsightRow { name: string; label: string; decided: number; approved: number; rejected: number; overridden: number; bad_rate: number }
interface Insights {
  days: number; decided: number; approved: number; rejected: number; overridden: number; bad_rate: number;
  groups: InsightRow[]; signals: InsightRow[]; hints: string[]; ai_mode: string;
  notes: { item_id: number; target: string; outcome: string; note: string; decided_by: string }[];
  ai: null | {
    provider_label: string; generated_at: string; summary: string; notes_used: number;
    themes: { theme: string; count: number; example: string }[];
    suggestions: { param: string; label: string; current: unknown; value: unknown; why: string }[];
  };
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
          <p className="sub"><Link to="/tasks">Expert Tasks</Link> / {t.category}</p>
          <h1>{t.name} <AiLabel features={t.ai_features} mode={t.ai_mode} style={{ verticalAlign: "middle", marginLeft: 6 }} /></h1>
          <p className="sub" style={{ maxWidth: 820 }}>{t.description}</p>
          <p className="small muted" style={{ marginTop: 6 }}>
            Runs on <Link to="/runners">worker-{t.worker}</Link>
            {t.optional_sources?.length ? <> - optional sources: {t.optional_sources.join(", ")} (used when configured)</> : null}
          </p>
        </div>
        <div className="row">
          <Link to={`/inbox?task=${t.key}&status=proposed`}><button>Inbox ({t.counts.proposed || 0})</button></Link>
          {can("admin") && <button className="primary" onClick={runNow}>Run now</button>}
        </div>
      </div>
      {msg && <div className="banner info">{msg}</div>}
      <ErrorLine error={err} />
      {t.key === "site_hierarchy_drift" && <BaselinePanel task={t} onDone={(m) => setMsg(m)} />}
      {t.key === "telemetry_baseline" && <OwnerReports />}
      <div className="grid cols-4">
        <div className="card stat"><div className="v"><Level level={t.autonomy.level} /></div><div className="l">Effective autonomy{t.autonomy.limited_by ? ` (limited by ${t.autonomy.limited_by})` : ""}</div></div>
        <div className="card stat"><div className="v">{t.counts.proposed || 0}</div><div className="l">Awaiting review</div></div>
        <div className="card stat"><div className="v">{t.counts.done || 0}</div><div className="l">Executed</div></div>
        <div className="card stat"><div className="v">{t.counts.failed || 0}</div><div className="l">Failed</div></div>
      </div>
      <div className="tabs">
        {(["config", "runs", "calibration"] as const).map((x) => (
          <button key={x} className={tab === x ? "active" : ""} onClick={() => setTab(x)}>
            {x === "config" ? "Configuration and autonomy" : x === "runs" ? "Runs" : "Calibration and feedback"}
          </button>
        ))}
      </div>
      {tab === "config" && <Config task={t} onSaved={(nt) => { task.setData(nt); setMsg("Saved."); }} />}
      {tab === "runs" && <TaskRuns taskKey={t.key} />}
      {tab === "calibration" && <><CalibrationView task={t} /><InsightsView task={t} /></>}
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
          <label className="field">Schedule (cron, UTC)
            {admin && (
              <select value={CRON_PRESETS.some((p) => p.cron === cfg.cron) ? cfg.cron : ""}
                onChange={(e) => e.target.value && setCfg({ ...cfg, cron: e.target.value })}>
                <option value="">Custom</option>
                {CRON_PRESETS.map((p) => <option key={p.cron} value={p.cron}>{p.label}</option>)}
              </select>
            )}
            <input disabled={!admin} value={cfg.cron} onChange={(e) => setCfg({ ...cfg, cron: e.target.value })}
              placeholder="minute hour day month weekday" />
            <span className="small">{describeCron(cfg.cron) || "Custom schedule"}
              {cfg.cron === task.config.cron && task.next_run && cfg.enabled && <> - next run {fmtDate(task.next_run)}</>}
              {cfg.cron !== task.config.cron && <> - not saved yet</>}</span>
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

interface OwnerGroup {
  class: string; label: string; count: number; title: string; body: string;
  ai: null | { provider_label: string; title: string | null; body: string | null; rejected_reason: string | null };
}

function OwnerReports() {
  const { can } = useAuth();
  const rep = useAsync(() => api.get<{ ai_mode: string; groups: OwnerGroup[] }>("/digest/owner-reports"), []);
  const [open, setOpen] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);
  const d = rep.data;
  if (!d) return <ErrorLine error={rep.error} />;
  if (!d.groups.length) return null;
  const aiOn = d.ai_mode !== "none";
  const write = async (cls: string) => {
    setBusy(cls);
    setErr(null);
    try {
      const g = await api.post<OwnerGroup>("/digest/owner-reports/ai", { cls });
      rep.setData({ ...d, groups: d.groups.map((x) => (x.class === cls ? g : x)) });
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setBusy(null);
    }
  };
  const copy = (key: string, text: string) => navigator.clipboard?.writeText(text).then(() => { setCopied(key); setTimeout(() => setCopied(null), 1500); });
  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <h2>Owner reports {aiOn && <span className="badge ai-on" style={{ marginLeft: 6 }}>{d.ai_mode === "local" ? "Local AI" : "Cloud AI"}</span>}</h2>
      <p className="small muted">Ticket-ready text per failure cause for the device owners. A resync cannot fix these; copy the text into your ticketing system.</p>
      <ErrorLine error={err} />
      {d.groups.map((g) => {
        const useAi = !!(g.ai?.title && g.ai?.body);
        const title = useAi ? g.ai!.title! : g.title;
        const body = useAi ? g.ai!.body! : g.body;
        return (
          <div key={g.class} style={{ borderTop: "1px solid var(--border)", padding: "8px 0" }}>
            <div className="row" style={{ justifyContent: "space-between" }}>
              <button className="link" onClick={() => setOpen(open === g.class ? null : g.class)}><b>{g.label}</b> ({g.count} devices)</button>
              <div className="row">
                {aiOn && can("admin") && !useAi && <button disabled={busy === g.class} onClick={() => write(g.class)}>{busy === g.class ? "Writing..." : "Write with AI"}</button>}
                <button onClick={() => copy(g.class, `${title}\n\n${body}`)}>{copied === g.class ? "Copied" : "Copy ticket"}</button>
              </div>
            </div>
            {g.ai?.rejected_reason && <div className="banner warn">{g.ai.rejected_reason}</div>}
            {open === g.class && (
              <>
                <p style={{ margin: "8px 0 4px" }}><b>{title}</b></p>
                <pre className="small" style={{ whiteSpace: "pre-wrap" }}>{body}</pre>
                {useAi && <p className="small muted">Written by {g.ai!.provider_label}; every device is listed (checked).</p>}
              </>
            )}
          </div>
        );
      })}
    </div>
  );
}

function InsightsView({ task }: { task: Task }) {
  const { can } = useAuth();
  const ins = useAsync(() => api.get<Insights>(`/tasks/${task.key}/insights`), [task.key]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const d = ins.data;
  if (!d) return <ErrorLine error={ins.error} />;
  const summarise = async () => {
    setBusy(true);
    setErr(null);
    try {
      ins.setData(await api.post<Insights>(`/tasks/${task.key}/insights/ai`));
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  };
  const pct = (x: number) => `${(x * 100).toFixed(0)}%`;
  const aiOn = d.ai_mode !== "none";
  return (
    <div className="grid cols-2" style={{ marginTop: 16 }}>
      <div className="card">
        <h2>Reviewer feedback (last {d.days} days)</h2>
        {!d.decided ? <p className="muted">No reviewed items yet.</p> : (
          <>
            <p className="small">{d.decided} reviewed: {d.approved} approved, {d.rejected} rejected, {d.overridden} overridden ({pct(d.bad_rate)} rejected or overridden).</p>
            {d.hints.length > 0 && <div className="banner warn">{d.hints.map((h, i) => <div key={i}>{h}</div>)}</div>}
            {d.groups.length > 0 && (
              <table style={{ marginTop: 8 }}>
                <thead><tr><th>Group</th><th className="num">Reviewed</th><th className="num">Rejected</th><th className="num">Overridden</th><th className="num">Bad</th></tr></thead>
                <tbody>{d.groups.map((g) => <tr key={g.name}><td>{g.label}</td><td className="num">{g.decided}</td><td className="num">{g.rejected}</td><td className="num">{g.overridden}</td><td className="num">{pct(g.bad_rate)}</td></tr>)}</tbody>
              </table>
            )}
            {d.signals.length > 0 && (
              <table style={{ marginTop: 8 }}>
                <thead><tr><th>When this evidence supported the proposal</th><th className="num">Reviewed</th><th className="num">Bad</th></tr></thead>
                <tbody>{d.signals.map((s) => <tr key={s.name}><td>{s.label}</td><td className="num">{s.decided}</td><td className="num">{pct(s.bad_rate)}</td></tr>)}</tbody>
              </table>
            )}
          </>
        )}
      </div>
      <div className="card">
        <h2>What reviewers say {aiOn && <span className="badge ai-on" style={{ marginLeft: 6 }}>{d.ai_mode === "local" ? "Local AI" : "Cloud AI"}</span>}</h2>
        {d.ai ? (
          <>
            <p>{d.ai.summary}</p>
            {d.ai.themes.length > 0 && <ul className="small">{d.ai.themes.map((t, i) => <li key={i}><b>{t.theme}</b> ({t.count}){t.example && <> - <i>"{t.example}"</i></>}</li>)}</ul>}
            {d.ai.suggestions.length > 0 && (
              <>
                <h3 style={{ marginTop: 10 }}>Suggested parameter changes</h3>
                <table>
                  <thead><tr><th>Parameter</th><th>Current</th><th>Suggested</th><th>Why</th></tr></thead>
                  <tbody>{d.ai.suggestions.map((s) => <tr key={s.param}><td title={s.param}>{s.label}</td><td>{String(s.current)}</td><td><b>{String(s.value)}</b></td><td className="small">{s.why}</td></tr>)}</tbody>
                </table>
                <p className="small muted">Advice only: nothing is changed. Apply a suggestion on the Configuration tab if you agree.</p>
              </>
            )}
            <p className="small muted">Generated by {d.ai.provider_label} {fmtDate(d.ai.generated_at)} from {d.ai.notes_used} notes; refreshed at most once a day.</p>
          </>
        ) : d.notes.length ? (
          <>
            <ul className="small">{d.notes.slice(0, 10).map((n) => <li key={n.item_id}><b>{n.outcome}</b> {n.target}: {n.note} <span className="muted">({n.decided_by})</span></li>)}</ul>
            {d.notes.length > 10 && <p className="small muted">{d.notes.length - 10} more notes.</p>}
          </>
        ) : <p className="muted">No reviewer notes on rejected or overridden items.</p>}
        <ErrorLine error={err} />
        {aiOn ? (
          can("admin") && !d.ai && d.decided > 0 && <button className="primary" disabled={busy} onClick={summarise}>{busy ? "Summarising..." : "Summarise with AI"}</button>
        ) : <p className="small muted">With AI on (Settings, AI assistance), notes can be summarised into themes and suggested parameter changes.</p>}
      </div>
    </div>
  );
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
