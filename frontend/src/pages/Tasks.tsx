import { Link } from "react-router-dom";
import { api, fmtDate } from "../api";
import { ErrorLine, Level, Status, useAsync } from "../components";
import { Task } from "../types";

export default function Tasks() {
  const { data, error } = useAsync(() => api.get<Task[]>("/tasks"), [], 15000);
  const byCat: Record<string, Task[]> = {};
  (data || []).forEach((t) => (byCat[t.category] ||= []).push(t));

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Recurring tasks</h1>
          <p className="sub">Every use case is a recurring task that produces work items with a confidence score.</p>
        </div>
      </div>
      <ErrorLine error={error} />
      {Object.entries(byCat).map(([cat, tasks]) => (
        <div className="card" key={cat}>
          <h2>{cat}</h2>
          <table>
            <thead>
              <tr><th>Task</th><th>Schedule</th><th>Autonomy (effective / cap)</th><th className="num">Review</th><th className="num">Done</th><th>Last run</th><th>Next run</th></tr>
            </thead>
            <tbody>
              {tasks.map((t) => (
                <tr key={t.key}>
                  <td>
                    <Link to={`/tasks/${t.key}`}><b>{t.name}</b></Link>
                    {!t.config.enabled && <span className="badge muted" style={{ marginLeft: 6 }}>disabled</span>}
                    <div className="small muted" style={{ maxWidth: 520 }}>{t.description}</div>
                  </td>
                  <td><code>{t.config.cron}</code></td>
                  <td>
                    <Level level={t.autonomy.level} /> / cap L{t.autonomy.task_cap}
                    {t.autonomy.limited_by && <div className="small muted">limited by {t.autonomy.limited_by}</div>}
                  </td>
                  <td className="num">{t.counts.proposed || 0}</td>
                  <td className="num">{t.counts.done || 0}</td>
                  <td>{t.last_run ? <Status value={t.last_run.status} /> : <span className="muted">never</span>}</td>
                  <td className="small">{t.config.enabled ? fmtDate(t.next_run) : "-"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
    </>
  );
}
