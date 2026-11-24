import { useEffect, useState } from "react";
import { api } from "../api";
import { ErrorLine, useAsync } from "../components";

const MAX = 500;

export default function Announcement() {
  const s = useAsync(() => api.get<{ announcement: string }>("/settings"), []);
  const [text, setText] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  useEffect(() => { if (s.data) setText(s.data.announcement || ""); }, [s.data]);

  const save = async (value: string) => {
    setErr(null);
    setSaved(false);
    try {
      const r = await api.put<{ announcement: string }>("/settings", { announcement: value });
      setText(r.announcement);
      s.setData(r);
      setSaved(true);
      window.dispatchEvent(new Event("steward:settings"));
    } catch (e: any) {
      setErr(e.message);
    }
  };

  const current = s.data?.announcement || "";
  const preview = text.trim();
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Announcement</h1>
          <p className="sub">A short message shown to every user in the sidebar, above the user box, until you clear it.</p>
        </div>
      </div>
      <ErrorLine error={err || s.error} />
      <div className="grid cols-2">
        <div className="card">
          <label className="field">
            Message
            <textarea rows={6} maxLength={MAX} value={text} placeholder="For example: Change freeze until Monday 08:00."
              onChange={(e) => { setText(e.target.value); setSaved(false); }} />
          </label>
          <div className="row" style={{ justifyContent: "space-between" }}>
            <span className={`small ${text.length > MAX - 50 ? "" : "muted"}`}>{text.length} / {MAX}</span>
            <div className="row">
              {saved && <span className="small muted">Saved</span>}
              <button disabled={!current} onClick={() => save("")}>Clear</button>
              <button className="primary" disabled={preview === current} onClick={() => save(text)}>Save</button>
            </div>
          </div>
          <p className="small muted">Plain text; line breaks are kept. Saving and clearing are recorded in the audit log.</p>
        </div>
        <div className="card">
          <h3 style={{ marginTop: 0 }}>Preview</h3>
          <div style={{ width: 220 }}>
            {preview ? <div className="sidebar-notice">{preview}</div> : <p className="small muted">No message: nothing is shown.</p>}
          </div>
        </div>
      </div>
    </>
  );
}
