import { useState } from "react";
import { useAuth } from "../App";
import { api } from "../api";
import { ErrorLine, Level, useAsync } from "../components";

interface GlobalSettings { kill_switch: boolean; global_autonomy_cap: number }

const CAP_TEXT = ["Report only everywhere", "Human review everywhere", "Auto above threshold allowed", "Full autonomy allowed"];

export default function Settings() {
  const { can } = useAuth();
  const admin = can("admin");
  const s = useAsync(() => api.get<GlobalSettings>("/settings"), []);
  const [err, setErr] = useState<string | null>(null);
  const update = async (body: Partial<GlobalSettings>) => {
    setErr(null);
    try {
      s.setData(await api.put<GlobalSettings>("/settings", body));
    } catch (e: any) {
      setErr(e.message);
    }
  };

  return (
    <>
      <div className="page-head"><div><h1>Settings</h1></div></div>
      <ErrorLine error={err || s.error} />
      {s.data && (
        <div className="grid cols-2">
          <div className="card">
            <h2>Kill switch</h2>
            <p className="small muted">Stops all execution against the network immediately. Tasks keep running and proposing; pending auto-approvals return to review.</p>
            <div className="row">
              {s.data.kill_switch ? <span className="badge bad">ON</span> : <span className="badge ok">off</span>}
              {admin && (
                <button className={s.data.kill_switch ? "ok" : "danger"}
                  onClick={() => confirm(s.data!.kill_switch ? "Resume execution?" : "Stop all execution now?") && update({ kill_switch: !s.data!.kill_switch })}>
                  {s.data.kill_switch ? "Turn off (resume execution)" : "Turn on (stop execution)"}
                </button>
              )}
            </div>
          </div>
          <div className="card">
            <h2>Global autonomy cap</h2>
            <p className="small muted">Upper bound for every task, on top of each task's own cap.</p>
            <div className="levels">
              {[0, 1, 2, 3].map((l) => (
                <div key={l} className={`level ${s.data!.global_autonomy_cap === l ? "active" : ""} ${admin ? "" : "capped"}`}
                  onClick={() => admin && update({ global_autonomy_cap: l })}>
                  <b>L{l}</b>{CAP_TEXT[l]}
                </div>
              ))}
            </div>
            <p className="small muted" style={{ marginTop: 8 }}>Current: <Level level={s.data.global_autonomy_cap} /></p>
          </div>
        </div>
      )}
    </>
  );
}
