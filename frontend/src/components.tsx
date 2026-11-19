import { ReactNode, useCallback, useEffect, useRef, useState } from "react";
import { ConnectionError } from "./api";

export function useAsync<T>(fn: () => Promise<T>, deps: unknown[], pollMs?: number) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const loaded = useRef(false);
  const load = useCallback(() => {
    setLoading(true);
    return fn()
      .then((d) => {
        loaded.current = true;
        setData(d);
        setError(null);
      })
      .catch((e) => {
        // The connection banner explains an outage; keep showing the last data instead of an error.
        if (!(e instanceof ConnectionError && loaded.current)) setError(String(e.message || e));
      })
      .finally(() => setLoading(false));
  }, deps);
  useEffect(() => {
    load();
    window.addEventListener("aiops:online", load);
    const t = pollMs ? setInterval(load, pollMs) : undefined;
    return () => {
      window.removeEventListener("aiops:online", load);
      if (t) clearInterval(t);
    };
  }, [load, pollMs]);
  return { data, error, loading, reload: load, setData };
}

export function Confidence({ value, small }: { value: number | null | undefined; small?: boolean }) {
  if (value === null || value === undefined) return <span className="muted">-</span>;
  const color = value >= 90 ? "var(--ok)" : value >= 70 ? "var(--info)" : value >= 50 ? "var(--warn)" : "var(--bad)";
  return (
    <span className="conf" title={`Confidence ${value}`}>
      {!small && (
        <span className="bar">
          <span style={{ width: `${value}%`, background: color }} />
        </span>
      )}
      <b style={{ color }}>{value}</b>
    </span>
  );
}

const STATUS_CLASS: Record<string, string> = {
  proposed: "info", reported: "muted", approved: "warn", executing: "warn", done: "ok", failed: "bad",
  rejected: "muted", superseded: "muted", success: "ok", running: "warn", queued: "muted", ok: "ok",
};

export function Status({ value }: { value: string | null | undefined }) {
  if (!value) return <span className="muted">-</span>;
  const cls = value.startsWith("error") ? "bad" : STATUS_CLASS[value] || "muted";
  return <span className={`badge ${cls}`}>{value.length > 40 ? value.slice(0, 40) + "..." : value}</span>;
}

export function Level({ level }: { level: number }) {
  const cls = level >= 3 ? "bad" : level === 2 ? "warn" : level === 1 ? "info" : "muted";
  return <span className={`badge ${cls}`}>L{level}</span>;
}

const AI_LABEL = { none: "AI Capable", local: "Local AI Assisted", cloud: "Cloud AI Assisted" } as const;

export function AiLabel({ features, mode, style }: {
  features: Record<string, string>; mode: keyof typeof AI_LABEL; style?: React.CSSProperties;
}) {
  const list = Object.values(features || {});
  if (!list.length) return null;
  const title = (mode === "none"
    ? "Runs fully on rules. With AI enabled (Settings > AI assistance) it adds:\n"
    : `AI (${mode === "local" ? "local LLM" : "Azure OpenAI"}) adds:\n`) + list.map((f) => `- ${f}`).join("\n");
  return <span className={`badge ${mode === "none" ? "ai" : "ai-on"}`} style={style} title={title}>{AI_LABEL[mode]}</span>;
}

export function Drawer({ onClose, children }: { onClose: () => void; children: ReactNode }) {
  useEffect(() => {
    const h = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [onClose]);
  return (
    <>
      <div className="drawer-bg" onClick={onClose} />
      <div className="drawer">{children}</div>
    </>
  );
}

export function Modal({ onClose, children }: { onClose: () => void; children: ReactNode }) {
  return (
    <>
      <div className="drawer-bg" onClick={onClose} />
      <div className="modal">{children}</div>
    </>
  );
}

export function Pager({ total, limit, offset, onChange }: { total: number; limit: number; offset: number; onChange: (o: number) => void }) {
  if (total <= limit) return null;
  return (
    <div className="pager">
      <span>
        {offset + 1}-{Math.min(offset + limit, total)} of {total}
      </span>
      <button disabled={offset === 0} onClick={() => onChange(Math.max(0, offset - limit))}>Previous</button>
      <button disabled={offset + limit >= total} onClick={() => onChange(offset + limit)}>Next</button>
    </div>
  );
}

export function ErrorLine({ error }: { error: string | null }) {
  return error ? <div className="banner bad">{error}</div> : null;
}
