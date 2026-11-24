import { useEffect, useRef, useState } from "react";
import { api, fmtDate } from "./api";
import { ErrorLine, Modal, useAsync } from "./components";
import { AiCloudConfig, AiMode, AiState } from "./types";

const MODES: { key: AiMode; label: string; text: string }[] = [
  { key: "none", label: "No AI", text: "Rules only. Nothing is downloaded and no data leaves the appliance." },
  { key: "local", label: "Local LLM", text: "An Ollama model on this appliance (CPU). Downloaded only when you select it." },
  { key: "cloud", label: "Microsoft Azure OpenAI", text: "Your organisation's Azure OpenAI deployment, in your own tenant." },
];

const LOCAL_STATE: Record<AiState["local"]["state"], { label: string; cls: string }> = {
  absent: { label: "Not installed", cls: "muted" },
  queued: { label: "Waiting for the host", cls: "warn" },
  installing: { label: "Starting container", cls: "warn" },
  pulling: { label: "Downloading model", cls: "warn" },
  ready: { label: "Ready", cls: "ok" },
  failed: { label: "Failed", cls: "bad" },
  disabled: { label: "Stopped (model kept)", cls: "muted" },
  removed: { label: "Removed", cls: "muted" },
};
const BUSY = ["queued", "installing", "pulling"];

type CloudForm = Omit<AiCloudConfig, "api_key_set" | "client_secret_set"> & { api_key: string; client_secret: string };

export function AiModeChip({ mode }: { mode: AiMode }) {
  return mode === "none"
    ? <span className="badge ai">No AI</span>
    : <span className="badge ai-on">{mode === "local" ? "Local AI" : "Cloud AI"} active</span>;
}

export default function AiSection() {
  const st = useAsync(() => api.get<AiState>("/settings/ai"), [], 5000);
  const [err, setErr] = useState<string | null>(null);
  const [ask, setAsk] = useState<null | "local" | "leave-local">(null);
  const [removeModel, setRemoveModel] = useState(false);
  const [showCloud, setShowCloud] = useState(false);
  const [form, setForm] = useState<CloudForm | null>(null);
  const [test, setTest] = useState<{ ok: boolean; error: string | null; latency_ms: number } | null>(null);
  const [testing, setTesting] = useState(false);
  const pendingMode = useRef<AiMode>("none");
  const d = st.data;

  useEffect(() => {
    if (d && !form) {
      const { api_key_set, client_secret_set, ...rest } = d.cloud;
      setForm({ ...rest, auth: rest.auth || "api_key", api_key: "", client_secret: "" });
      setShowCloud(d.mode === "cloud");
    }
  }, [d]);

  const act = async (fn: () => Promise<AiState>) => {
    setErr(null);
    try {
      st.setData(await fn());
    } catch (e: any) {
      setErr(e.message);
    }
  };

  if (!d || !form) return <div className="card" id="ai"><ErrorLine error={st.error} /></div>;
  const manage = d.can_manage;
  const local = d.local;
  const localBusy = BUSY.includes(local.state);

  const cloudBody = () => ({
    endpoint: form.endpoint, deployment: form.deployment, api_version: form.api_version, auth: form.auth,
    tenant_id: form.tenant_id, client_id: form.client_id,
    ...(form.api_key ? { api_key: form.api_key } : {}), ...(form.client_secret ? { client_secret: form.client_secret } : {}),
  });

  const choose = (m: AiMode) => {
    if (!manage) return;
    if (m === d.mode && m !== "cloud") { setShowCloud(false); return; }
    pendingMode.current = m;
    if (m === "cloud") { setShowCloud(true); return; }
    setShowCloud(false);
    if (d.mode === "local") { setRemoveModel(false); setAsk("leave-local"); return; }
    if (m === "local") { setAsk("local"); return; }
    act(() => api.put<AiState>("/settings/ai", { mode: m }));
  };

  const saveCloud = () => act(async () => {
    const leaving = d.mode === "local";
    const r = await api.put<AiState>("/settings/ai", { mode: "cloud", cloud: cloudBody(), remove_local: leaving && removeModel });
    setForm({ ...form, api_key: "", client_secret: "" });
    return r;
  });

  const runTest = async () => {
    setTesting(true); setTest(null); setErr(null);
    try {
      setTest(await api.post("/settings/ai/test", { mode: "cloud", cloud: cloudBody() }));
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setTesting(false);
    }
  };

  const shownMode: AiMode = showCloud ? "cloud" : d.mode;
  const field = (k: keyof CloudForm, label: string, opts: { type?: string; placeholder?: string } = {}) => (
    <label className="field">{label}
      <input type={opts.type || "text"} value={form[k] as string} placeholder={opts.placeholder} disabled={!manage}
        autoComplete="off" onChange={(e) => setForm({ ...form, [k]: e.target.value })} />
    </label>
  );

  return (
    <div className="card" id="ai">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <span className="muted small">Current mode</span>
        <AiModeChip mode={d.effective_mode} />
      </div>
      <ErrorLine error={err || st.error} />
      {local.updater_stale && (
        <div className="banner warn">
          Requests are waiting for the host updater. Check <code>systemctl status steward-updater.path</code> on the host.
        </div>
      )}

      <div className="levels">
        {MODES.map((m) => (
          <div key={m.key} className={`level ${shownMode === m.key ? "active" : ""} ${manage ? "" : "capped"}`}
            onClick={() => choose(m.key)}>
            <b>{m.label}</b>{m.text}
          </div>
        ))}
      </div>
      {!manage && <p className="small muted">Only administrators can change the AI mode.</p>}

      {(d.mode === "local" || local.state !== "absent") && (
        <>
          <h3>Local LLM</h3>
          <div className="kv">
            <span className="k">Status</span>
            <span><span className={`badge ${LOCAL_STATE[local.state].cls}`}>{LOCAL_STATE[local.state].label}</span>
              {d.mode === "local" && local.state !== "ready" && <span className="small muted"> AI stays off until the model is ready.</span>}</span>
            <span className="k">Model</span><span><code>{local.model}</code></span>
            <span className="k">Last change</span><span>{fmtDate(local.updated_at)}</span>
            <span className="k">Hardware</span><span className="small muted">16 vCPU and 64 GB RAM recommended; inference runs on the CPU.</span>
          </div>
          {local.error && <div className="banner bad" style={{ marginTop: 10 }}>{local.error}</div>}
          {manage && (
            <div className="row end" style={{ marginTop: 10 }}>
              {d.mode === "local" && local.state === "failed" && (
                <button onClick={() => act(() => api.post<AiState>("/settings/ai/local", { action: "enable" }))}>Retry install</button>
              )}
              {!["absent", "removed"].includes(local.state) && !localBusy && (
                <button className="danger" onClick={() => confirm("Stop the local LLM and delete the downloaded model? AI switches to No AI.")
                  && act(() => api.post<AiState>("/settings/ai/local", { action: "remove" }))}>Remove model</button>
              )}
            </div>
          )}
          {manage && local.state !== "absent" && <LlmLog live={localBusy} />}
        </>
      )}

      {shownMode === "cloud" && (
        <>
          <h3>Microsoft Azure OpenAI</h3>
          <div className="banner warn">
            Cloud mode sends evidence summaries (hostnames, site names, IP addresses, reviewer notes) to your Azure tenant.
            Credentials and device configuration are never sent.
          </div>
          <div className="grid cols-2" style={{ gap: 12 }}>
            {field("endpoint", "Endpoint", { placeholder: "https://<resource>.openai.azure.com" })}
            {field("deployment", "Deployment name", { placeholder: "gpt-4o-mini" })}
            {field("api_version", "API version", { placeholder: "2024-10-21" })}
            <label className="field">Authentication
              <select value={form.auth} disabled={!manage} onChange={(e) => setForm({ ...form, auth: e.target.value as CloudForm["auth"] })}>
                <option value="api_key">API key</option>
                <option value="entra">Microsoft Entra ID (app registration)</option>
              </select>
            </label>
            {form.auth === "api_key"
              ? field("api_key", d.cloud.api_key_set ? "API key (saved; type to replace)" : "API key", { type: "password" })
              : <>
                {field("tenant_id", "Tenant ID")}
                {field("client_id", "Client ID")}
                {field("client_secret", d.cloud.client_secret_set ? "Client secret (saved; type to replace)" : "Client secret", { type: "password" })}
              </>}
          </div>
          {d.mode === "local" && manage && (
            <label className="check small"><input type="checkbox" checked={removeModel} onChange={(e) => setRemoveModel(e.target.checked)} />
              Also delete the local LLM model (otherwise it is stopped and kept)</label>
          )}
          {test && (
            <div className={`banner ${test.ok ? "info" : "bad"}`} style={{ marginTop: 10 }}>
              {test.ok ? `Connection OK (${test.latency_ms} ms).` : `Test failed: ${test.error}`}
            </div>
          )}
          {manage && (
            <div className="row end" style={{ marginTop: 10 }}>
              {d.mode !== "cloud" && <button onClick={() => setShowCloud(false)}>Cancel</button>}
              <button disabled={testing} onClick={runTest}>{testing ? "Testing..." : "Test connection"}</button>
              <button className="primary" onClick={saveCloud}>{d.mode === "cloud" ? "Save" : "Save and use Azure OpenAI"}</button>
            </div>
          )}
        </>
      )}

      {ask === "local" && (
        <Modal onClose={() => setAsk(null)}>
          <h2>Enable the local LLM?</h2>
          <p>
            The host starts the Ollama container and downloads <code>{local.model}</code> (several GB, one time).
            AI stays off until the download finishes; progress shows here.
          </p>
          <p className="small muted">Inference runs on the CPU: 16 vCPU and 64 GB RAM recommended. Nothing leaves the appliance.</p>
          <div className="row end">
            <button onClick={() => setAsk(null)}>Cancel</button>
            <button className="primary" onClick={() => { setAsk(null); act(() => api.put<AiState>("/settings/ai", { mode: "local" })); }}>
              Enable local LLM
            </button>
          </div>
        </Modal>
      )}
      {ask === "leave-local" && (
        <Modal onClose={() => setAsk(null)}>
          <h2>Switch to {MODES.find((m) => m.key === pendingMode.current)?.label}?</h2>
          <p>The local LLM container is stopped to free its memory.</p>
          <label className="check"><input type="checkbox" checked={removeModel} onChange={(e) => setRemoveModel(e.target.checked)} />
            Also delete the downloaded model (re-enabling then downloads it again)</label>
          <div className="row end" style={{ marginTop: 12 }}>
            <button onClick={() => setAsk(null)}>Cancel</button>
            <button className="primary" onClick={() => {
              setAsk(null);
              act(() => api.put<AiState>("/settings/ai", { mode: pendingMode.current, remove_local: removeModel }));
            }}>Switch</button>
          </div>
        </Modal>
      )}
    </div>
  );
}

function LlmLog({ live }: { live: boolean }) {
  const log = useAsync(() => api.get<{ log: string }>("/settings/ai/log"), [], live ? 3000 : undefined);
  const ref = useRef<HTMLPreElement>(null);
  useEffect(() => {
    if (ref.current) ref.current.scrollTop = ref.current.scrollHeight;
  }, [log.data?.log]);
  if (!log.data?.log) return null;
  return <pre ref={ref} style={{ maxHeight: 220, marginTop: 10 }}>{log.data.log}</pre>;
}
