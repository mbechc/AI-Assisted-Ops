export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

/** The browser could not reach the Steward API (network or VPN down, proxy page, API restarting). */
export class ConnectionError extends Error {}

export const LOST_CONNECTION = "Lost connection to the Steward platform. Check VPN or network.";
const PROBE_MS = 5000;
let online = true;
let probeTimer: number | undefined;

export const isOnline = () => online;

function offline(message: string): ConnectionError {
  if (online) {
    online = false;
    window.dispatchEvent(new CustomEvent("steward:offline", { detail: message }));
  }
  if (probeTimer === undefined) probeTimer = window.setInterval(probe, PROBE_MS);
  return new ConnectionError(message);
}

function markOnline() {
  if (probeTimer !== undefined) {
    window.clearInterval(probeTimer);
    probeTimer = undefined;
  }
  if (!online) {
    online = true;
    window.dispatchEvent(new Event("steward:online"));
  }
}

export const APP_VERSION = __APP_VERSION__;
const VERSION_CHECK_MS = 60000;
const RELOADED_FOR = "steward-reloaded-for";

/** After an update the server runs a newer version than this tab: reload once to load the new UI. */
function checkVersion(server: unknown) {
  if (typeof server !== "string" || !server || server === APP_VERSION) return;
  if (sessionStorage.getItem(RELOADED_FOR) === server) return; // the reload did not bring the new UI; avoid a loop
  sessionStorage.setItem(RELOADED_FOR, server);
  window.location.reload();
}

/** Starts the periodic version check; the offline probe checks too, so a reload follows the update restart. */
export function watchVersion() {
  window.setInterval(() => { if (online) probe(); }, VERSION_CHECK_MS);
}

/** Checks whether the API answers again; called on a timer while offline and by "Retry now". */
export async function probe(): Promise<boolean> {
  try {
    const res = await fetch("/api/health", { cache: "no-store", credentials: "same-origin" });
    if (res.ok && (res.headers.get("content-type") || "").includes("json")) {
      checkVersion((await res.json())?.version);
      markOnline();
      return true;
    }
  } catch {
    // still offline
  }
  return false;
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`/api${path}`, {
      method,
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-Steward": "1" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw offline(LOST_CONNECTION);
  }
  let text: string;
  try {
    text = await res.text();
  } catch {
    throw offline(LOST_CONNECTION);
  }
  // Every API reply is JSON; anything else came from nginx (API down) or from the network in between.
  if (text && !(res.headers.get("content-type") || "").includes("json")) {
    throw offline([502, 503, 504].includes(res.status)
      ? `The Steward platform is not responding (HTTP ${res.status}).`
      : `${LOST_CONNECTION} (Unexpected response from the network, HTTP ${res.status}.)`);
  }
  markOnline();
  if (res.status === 401 && !path.startsWith("/auth/")) {
    window.dispatchEvent(new Event("steward:unauthorized"));
  }
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) {
    const detail = data?.detail;
    throw new ApiError(res.status, typeof detail === "string" ? detail : JSON.stringify(detail ?? res.statusText));
  }
  return data as T;
}

export const api = {
  get: <T>(path: string) => request<T>("GET", path),
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, body ?? {}),
  put: <T>(path: string, body: unknown) => request<T>("PUT", path, body),
  patch: <T>(path: string, body: unknown) => request<T>("PATCH", path, body),
  del: <T>(path: string) => request<T>("DELETE", path),
};

export function qs(params: Record<string, string | number | boolean | undefined | null>): string {
  const p = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== "") p.set(k, String(v));
  });
  const s = p.toString();
  return s ? `?${s}` : "";
}

/** Like qs, but array values become repeated parameters (source_id=1&source_id=2). */
export function qsMulti(params: Record<string, string | number | boolean | (string | number)[] | undefined | null>): string {
  const p = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (Array.isArray(v)) v.forEach((x) => p.append(k, String(x)));
    else if (v !== undefined && v !== null && v !== "") p.set(k, String(v));
  });
  const s = p.toString();
  return s ? `?${s}` : "";
}

export function fmtDate(v?: string | null): string {
  if (!v) return "-";
  const d = new Date(v);
  return d.toLocaleString(undefined, { dateStyle: "short", timeStyle: "medium" });
}

export function duration(a?: string | null, b?: string | null): string {
  if (!a || !b) return "-";
  const s = Math.round((new Date(b).getTime() - new Date(a).getTime()) / 1000);
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}
