import { useEffect, useRef, useState } from "react";
import { api, fmtDate } from "./api";
import { ErrorLine, Modal, useAsync } from "./components";
import { Connectivity, UpdateState } from "./types";

const TRACKS = [
  { key: "production", label: "Production", text: "Stable releases only." },
  { key: "development", label: "Development", text: "Every release, including pre-releases (vX.Y.Z-dev.N)." },
] as const;

export function ConnectivityChip({ c, onClick }: { c: Connectivity; onClick?: () => void }) {
  const label = c.status === "connected" ? "Connected" : c.status === "airgapped" ? "AirGapped" : "Unknown";
  const when = c.checked_at ? `Last check ${fmtDate(c.checked_at)}.` : "";
  const title = c.status === "connected"
    ? `The host reaches GitHub; updates are possible. ${when}`
    : c.status === "airgapped"
      ? `Not reachable from the host: ${c.failed.join(", ")}. ${when}`
      : c.reason || "No result from the host updater.";
  return (
    <span className={`net-chip ${c.status} ${onClick ? "link" : ""}`} title={title} onClick={onClick}
      role={onClick ? "link" : undefined}>
      <span className="dot" />{label}
    </span>
  );
}

export default function UpdatesSection() {
  const st = useAsync(() => api.get<UpdateState>("/updates"), [], 5000);
  const [err, setErr] = useState<string | null>(null);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [refreshing, setRefreshing] = useState<{ since: number; prev: string | null } | null>(null);
  const ref = useRef<HTMLDivElement>(null);
  const d = st.data;

  useEffect(() => {
    if (d && window.location.hash === "#updates") ref.current?.scrollIntoView({ behavior: "smooth" });
  }, [!!d]);

  // Refresh finishes when a newer connectivity result arrives, or times out if the host updater is missing.
  useEffect(() => {
    if (refreshing === null || !d) return;
    if (d.connectivity.checked_at !== refreshing.prev && !d.pending.some((p) => p.action === "check")) setRefreshing(null);
    else if (Date.now() - refreshing.since > 90000) {
      setRefreshing(null);
      setErr("The host updater did not respond. Check that aiops-updater.path is active on the host (systemctl status aiops-updater.path).");
    }
  }, [d, refreshing]);

  const act = async (fn: () => Promise<UpdateState>) => {
    setErr(null);
    try {
      st.setData(await fn());
    } catch (e: any) {
      setErr(e.message);
    }
  };

  if (!d) return <div className="card" id="updates"><h2>Updates</h2><ErrorLine error={st.error} /></div>;
  const c = d.connectivity;
  const manage = d.can_manage;
  const inst = d.install;
  const busy = d.install_running;
  const canInstall = manage && !!d.available && c.status === "connected" && !busy;

  return (
    <div className="card updates" id="updates" ref={ref}>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h2 style={{ margin: 0 }}>Updates</h2>
        <ConnectivityChip c={c} />
      </div>
      <ErrorLine error={err || st.error} />
      {d.updater_stale && (
        <div className="banner warn">
          Requests are waiting for the host updater. Check <code>systemctl status aiops-updater.path</code> on the host.
        </div>
      )}

      <div className="kv" style={{ margin: "12px 0" }}>
        <span className="k">Installed version</span><span><b>{d.current_version}</b></span>
        <span className="k">Track</span>
        <span>
          {TRACKS.find((t) => t.key === d.channel)?.label}
          {d.channel_pending && d.channel_pending !== d.channel && (
            <span className="muted small"> (switching to {d.channel_pending}...)</span>
          )}
        </span>
        <span className="k">Available</span>
        <span>
          {d.available
            ? <><b>{d.available.version}</b> {d.available.prerelease && <span className="badge warn">pre-release</span>}
                <span className="muted small"> published {fmtDate(d.available.published_at)}</span></>
            : <span className="muted">{d.releases_checked_at ? "You are on the newest release for this track." : "Not checked yet."}</span>}
        </span>
        <span className="k">Last release check</span>
        <span>{fmtDate(d.releases_checked_at)}{d.releases_error && <span className="small" style={{ color: "var(--bad)" }}> {d.releases_error}</span>}</span>
      </div>

      <h3>Track</h3>
      <div className="levels">
        {TRACKS.map((t) => {
          const active = (d.channel_pending || d.channel) === t.key;
          return (
            <div key={t.key} className={`level ${active ? "active" : ""} ${manage && !busy ? "" : "capped"}`}
              onClick={() => manage && !busy && !active &&
                confirm(`Switch the update track to ${t.label}?`) &&
                act(() => api.put<UpdateState>("/updates/channel", { channel: t.key }))}>
              <b>{t.label}</b>{t.text}
            </div>
          );
        })}
      </div>
      {!manage && <p className="small muted">Only administrators can switch track, refresh or install updates.</p>}

      <h3>GitHub reachability from the host</h3>
      <table>
        <tbody>
          {c.endpoints.map((e) => (
            <tr key={e.host}>
              <td style={{ width: 24 }}><span className={`dot-status ${e.ok ? "ok" : "bad"}`} /></td>
              <td>{e.host}</td>
              <td className="muted">{e.ok ? `${e.ms} ms` : e.error}</td>
            </tr>
          ))}
          {!c.endpoints.length && <tr><td colSpan={3} className="muted">{c.reason || "No result yet."}</td></tr>}
        </tbody>
      </table>
      <div className="row" style={{ marginTop: 8 }}>
        <span className="small muted">Last check: {fmtDate(c.checked_at)} (automatic every {c.interval_min} min)</span>
        <span style={{ flex: 1 }} />
        {manage && (
          <button disabled={refreshing !== null || busy}
            onClick={() => { setRefreshing({ since: Date.now(), prev: c.checked_at }); act(() => api.post<UpdateState>("/updates/refresh")); }}>
            {refreshing !== null ? "Checking..." : "Refresh"}
          </button>
        )}
      </div>

      {d.available && (
        <>
          <h3>Release notes {d.available.name !== d.available.tag && <>- {d.available.name}</>}</h3>
          <pre style={{ maxHeight: 220 }}>{d.available.notes || "No release notes."}</pre>
          {d.available.url && <a className="small" href={d.available.url} target="_blank" rel="noreferrer">View on GitHub</a>}
        </>
      )}

      {manage && (
        <div className="row end" style={{ marginTop: 12 }}>
          {d.available && c.status !== "connected" && <span className="small muted">Install needs the host to reach all GitHub endpoints.</span>}
          <button className="primary" disabled={!canInstall} onClick={() => setConfirmOpen(true)}>
            {busy ? "Updating..." : d.available ? `Install update ${d.available.version}` : "Install update"}
          </button>
        </div>
      )}

      {inst && (inst.state === "running" || busy || manage) && (
        <>
          <h3>
            Last update {inst.version && <>to {inst.version}</>}:{" "}
            <span className={`badge ${inst.state === "succeeded" ? "ok" : inst.state === "failed" ? "bad" : "warn"}`}>{inst.state}</span>
          </h3>
          {inst.error && <div className="banner bad">{inst.error}</div>}
          <p className="small muted">Started {fmtDate(inst.started_at)}{inst.finished_at && <>, finished {fmtDate(inst.finished_at)}</>}</p>
          {manage && <UpdateLog live={busy} />}
        </>
      )}

      {confirmOpen && d.available && (
        <Modal onClose={() => setConfirmOpen(false)}>
          <h2>Install AIOps {d.available.version}?</h2>
          <p>
            The host takes a backup (database and configuration), pulls the changed image layers from ghcr.io,
            runs the database migrations and restarts the services. The portal is unavailable for a few minutes.
          </p>
          <p className="small muted">Rolling back means restoring the automatic pre-update backup (see README, Backup and restore).</p>
          <div className="row end">
            <button onClick={() => setConfirmOpen(false)}>Cancel</button>
            <button className="primary" onClick={() => {
              setConfirmOpen(false);
              act(() => api.post<UpdateState>("/updates/install", { version: d.available!.version }));
            }}>Install {d.available.version}</button>
          </div>
        </Modal>
      )}
    </div>
  );
}

function UpdateLog({ live }: { live: boolean }) {
  const log = useAsync(() => api.get<{ log: string }>("/updates/log"), [], live ? 3000 : undefined);
  const ref = useRef<HTMLPreElement>(null);
  useEffect(() => {
    if (ref.current) ref.current.scrollTop = ref.current.scrollHeight;
  }, [log.data?.log]);
  if (!log.data?.log) return null;
  return <pre ref={ref} style={{ maxHeight: 300 }}>{log.data.log}</pre>;
}
