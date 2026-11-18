import { useEffect, useMemo, useRef, useState } from "react";
import { qs } from "./api";

interface Line { n: number; service: string; ts: string; line: string; level: string }

const MAX_LINES = 5000;
const COLORS = ["#58a6ff", "#3fb950", "#d29922", "#bc8cff", "#39c5cf", "#ff7b72", "#e3b341", "#79c0ff", "#56d364", "#f778ba"];

export function serviceColor(service: string): string {
  let h = 0;
  for (const c of service) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return COLORS[h % COLORS.length];
}

/** Live container log stream (server-sent events). Pass services, or runId to follow one run. */
export default function LogView({ services, runId, tail = 200, short }: { services?: string[]; runId?: number; tail?: number; short?: boolean }) {
  const [lines, setLines] = useState<Line[]>([]);
  const [paused, setPaused] = useState(false);
  const [filter, setFilter] = useState("");
  const [levels, setLevels] = useState<Set<string>>(new Set(["error", "warn", "info", "debug"]));
  const [state, setState] = useState<"connecting" | "live" | "ended" | "error">("connecting");
  const [err, setErr] = useState<string | null>(null);
  const buf = useRef<Line[]>([]);
  const counter = useRef(0);
  const pausedRef = useRef(false);
  const box = useRef<HTMLDivElement>(null);
  const key = runId ? `run:${runId}` : (services || []).join(",");

  useEffect(() => {
    if (!key) return;
    buf.current = [];
    setLines([]);
    setState("connecting");
    setErr(null);
    const url = `/api/admin/logs/stream${qs({ services: runId ? undefined : (services || []).join(","), run_id: runId, tail })}`;
    const es = new EventSource(url, { withCredentials: true });
    let ended = 0;
    let flush: number | null = null;
    const schedule = () => {
      if (flush !== null) return;
      flush = window.setTimeout(() => {
        flush = null;
        if (!pausedRef.current) setLines(buf.current.slice());
      }, 150);
    };
    es.onopen = () => setState("live");
    es.onmessage = (ev) => {
      const d = JSON.parse(ev.data);
      buf.current.push({ n: counter.current++, ...d });
      if (buf.current.length > MAX_LINES) buf.current.splice(0, buf.current.length - MAX_LINES);
      schedule();
    };
    es.addEventListener("ended", () => {
      ended += 1;
      if (ended >= (runId ? 1 : (services || []).length)) { setState("ended"); es.close(); }
    });
    es.onerror = () => {
      if (es.readyState === EventSource.CLOSED) {
        setState("error");
        setErr("Log stream unavailable (admin only; the logproxy service must be running).");
      }
    };
    return () => { es.close(); if (flush !== null) clearTimeout(flush); };
  }, [key, tail]);

  useEffect(() => {
    pausedRef.current = paused;
    if (!paused) setLines(buf.current.slice());
  }, [paused]);

  useEffect(() => {
    if (!paused && box.current) box.current.scrollTop = box.current.scrollHeight;
  }, [lines, paused]);

  const shown = useMemo(() => {
    const f = filter.toLowerCase();
    return lines.filter((l) => levels.has(l.level) && (!f || l.line.toLowerCase().includes(f) || l.service.includes(f)));
  }, [lines, filter, levels]);

  const download = () => {
    const text = buf.current.map((l) => `${l.ts} [${l.service}] ${l.line}`).join("\n");
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([text], { type: "text/plain" }));
    a.download = `aiops-${runId ? `run-${runId}` : "logs"}-${new Date().toISOString().replace(/[:.]/g, "")}.log`;
    a.click();
    URL.revokeObjectURL(a.href);
  };

  const toggleLevel = (lv: string) => {
    const s = new Set(levels);
    s.has(lv) ? s.delete(lv) : s.add(lv);
    setLevels(s);
  };

  return (
    <div>
      <div className="row" style={{ marginBottom: 8 }}>
        <span className={`dot ${state === "live" ? "ok" : state === "error" ? "bad" : state === "ended" ? "muted" : "warn"}`} />
        <span className="small muted">{state}{paused ? " (paused)" : ""} - {shown.length} of {lines.length} lines</span>
        <div className="spacer" />
        <input placeholder="Filter" value={filter} onChange={(e) => setFilter(e.target.value)} style={{ width: 180 }} />
        <div className="chips">
          {["error", "warn", "info", "debug"].map((lv) => (
            <span key={lv} className={`chip ${levels.has(lv) ? "on" : ""}`} onClick={() => toggleLevel(lv)}>{lv}</span>
          ))}
        </div>
        <button onClick={() => setPaused(!paused)}>{paused ? "Follow" : "Pause"}</button>
        <button onClick={() => { buf.current = []; setLines([]); }}>Clear</button>
        <button onClick={download}>Download</button>
      </div>
      {err && <div className="banner bad">{err}</div>}
      <div className={`logview ${short ? "short" : ""}`} ref={box}>
        {shown.map((l) => (
          <div key={l.n} className={`logline lv-${l.level}`}>
            <span className="svc" style={{ color: serviceColor(l.service) }}>{l.service}</span>
            <Highlight text={l.line} q={filter} />
          </div>
        ))}
        {!shown.length && <div className="muted">{state === "connecting" ? "Connecting..." : "No log lines yet."}</div>}
      </div>
    </div>
  );
}

function Highlight({ text, q }: { text: string; q: string }) {
  if (!q) return <>{text}</>;
  const i = text.toLowerCase().indexOf(q.toLowerCase());
  if (i < 0) return <>{text}</>;
  return <>{text.slice(0, i)}<mark>{text.slice(i, i + q.length)}</mark>{text.slice(i + q.length)}</>;
}
