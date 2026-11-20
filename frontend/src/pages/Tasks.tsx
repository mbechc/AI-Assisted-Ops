import { useState } from "react";
import { Link } from "react-router-dom";
import { useAuth } from "../App";
import { api, fmtDate } from "../api";
import { AccordionCard, AiLabel, ErrorLine, Level, Status, useAsync } from "../components";
import { describeCron } from "../cron";
import { Run, Task } from "../types";

export default function Tasks() {
  const { can } = useAuth();
  const { data, error, reload } = useAsync(() => api.get<Task[]>("/tasks"), [], 15000);
  const [starting, setStarting] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [openCat, setOpenCat] = useState<string | null>(null);
  const byCat: Record<string, Task[]> = {};
  (data || []).forEach((t) => (byCat[t.category] ||= []).push(t));

  const run = async (t: Task) => {
    setStarting(t.key);
    setErr(null);
    setMsg(null);
    try {
      const r = await api.post<Run>(`/tasks/${t.key}/run`);
      setMsg(`${t.name}: run ${r.id} queued.`);
      await reload();
    } catch (e: any) {
      setErr(`${t.name}: ${e.message}`);
    } finally {
      setStarting(null);
    }
  };

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Expert Tasks</h1>
          <p className="sub">
            Every use case is an Expert Task that runs on a schedule and produces work items with a confidence score.
            <span className="badge ai" style={{ margin: "0 4px 0 8px" }}>AI Capable</span>runs on rules, AI off;
            <span className="badge ai-on" style={{ margin: "0 4px 0 8px" }}>AI Assisted</span>uses the AI selected in Settings.
          </p>
        </div>
      </div>
      {msg && <div className="banner info">{msg} <Link to="/runs">Expert Task history</Link></div>}
      <ErrorLine error={err || error} />
      {Object.entries(byCat).map(([cat, tasks]) => (
        <AccordionCard key={cat} title={cat} summary={<CategorySummary tasks={tasks} />} open={openCat === cat}
          onToggle={() => setOpenCat(openCat === cat ? null : cat)}>
          <table>
            <thead>
              <tr><th>Task</th><th>Schedule</th><th>Autonomy (effective / cap)</th><th className="num">Review</th><th className="num">Done</th><th>Last run</th><th>Next run</th>{can("admin") && <th />}</tr>
            </thead>
            <tbody>
              {tasks.map((t) => {
                const active = t.last_run?.status === "queued" || t.last_run?.status === "running";
                return (
                  <tr key={t.key}>
                    <td>
                      <Link to={`/tasks/${t.key}`}><b>{t.name}</b></Link>
                      <AiLabel features={t.ai_features} mode={t.ai_mode} style={{ marginLeft: 6 }} />
                      {!t.config.enabled && <span className="badge muted" style={{ marginLeft: 6 }}>disabled</span>}
                      <div className="small muted" style={{ maxWidth: 520 }}>{t.description}</div>
                    </td>
                    <td title={t.config.cron}>
                      {describeCron(t.config.cron) || <code>{t.config.cron}</code>}
                      {describeCron(t.config.cron) && <div className="small muted"><code>{t.config.cron}</code></div>}
                    </td>
                    <td>
                      <Level level={t.autonomy.level} /> / cap L{t.autonomy.task_cap}
                      {t.autonomy.limited_by && <div className="small muted">limited by {t.autonomy.limited_by}</div>}
                    </td>
                    <td className="num">{t.counts.proposed || 0}</td>
                    <td className="num">{t.counts.done || 0}</td>
                    <td>{t.last_run ? <Status value={t.last_run.status} /> : <span className="muted">never</span>}</td>
                    <td className="small">{t.config.enabled ? fmtDate(t.next_run) : "-"}</td>
                    {can("admin") && (
                      <td className="num">
                        <button disabled={active || starting === t.key} onClick={() => run(t)}
                          title={active ? "A run is already queued or running" : "Start a run now"}>
                          {starting === t.key ? "Starting..." : active ? "Running..." : "Run"}
                        </button>
                      </td>
                    )}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </AccordionCard>
      ))}
    </>
  );
}

function CategorySummary({ tasks }: { tasks: Task[] }) {
  const review = tasks.reduce((n, t) => n + (t.counts.proposed || 0), 0);
  const failed = tasks.filter((t) => t.last_run?.status === "failed").length;
  const running = tasks.filter((t) => t.last_run?.status === "queued" || t.last_run?.status === "running").length;
  return (
    <>
      <span>{tasks.length} task{tasks.length === 1 ? "" : "s"}</span>
      {review > 0 && <span>{review} awaiting review</span>}
      {running > 0 && <span>{running} running</span>}
      {failed > 0 && <span className="err">{failed} last run failed</span>}
    </>
  );
}
