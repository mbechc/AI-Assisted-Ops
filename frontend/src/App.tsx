import { createContext, useContext, useEffect, useRef, useState } from "react";
import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { api, ConnectionError, isOnline, LOST_CONNECTION, probe } from "./api";
import { useAsync } from "./components";
import { applyTheme } from "./theme";
import { Role, User } from "./types";
import UserMenu from "./UserMenu";
import Login from "./pages/Login";
import Dashboard from "./pages/Dashboard";
import Tasks from "./pages/Tasks";
import TaskDetail from "./pages/TaskDetail";
import Review from "./pages/Review";
import Runs from "./pages/Runs";
import Runners from "./pages/Runners";
import Logs from "./pages/Logs";
import Sources from "./pages/Sources";
import GeoipSource from "./pages/GeoipSource";
import Audit from "./pages/Audit";
import Users from "./pages/Users";
import Settings from "./pages/Settings";

const RANK: Record<Role, number> = { viewer: 0, operator: 1, admin: 2 };

interface AuthCtx {
  user: User;
  can: (role: Role) => boolean;
  logout: () => void;
  setUser: (u: User) => void;
}

const Ctx = createContext<AuthCtx | null>(null);
export const useAuth = () => useContext(Ctx)!;

export default function App() {
  const [user, setUser] = useState<User | null | undefined>(undefined);
  const [unreachable, setUnreachable] = useState(false);
  const userRef = useRef(user);
  userRef.current = user;

  useEffect(() => {
    const me = () =>
      api.get<User>("/auth/me")
        .then((u) => {
          setUnreachable(false);
          setUser(u);
        })
        .catch((e) => {
          // Only a real 401 means "logged out"; a lost connection must not show the login page.
          if (e instanceof ConnectionError) setUnreachable(true);
          else setUser(null);
        });
    me();
    const retryMe = () => {
      if (userRef.current === undefined) me();
    };
    const h = () => setUser(null);
    window.addEventListener("aiops:unauthorized", h);
    window.addEventListener("aiops:online", retryMe);
    return () => {
      window.removeEventListener("aiops:unauthorized", h);
      window.removeEventListener("aiops:online", retryMe);
    };
  }, []);

  useEffect(() => {
    if (user?.theme) applyTheme(user.theme);
  }, [user?.theme]);

  if (user === undefined) return unreachable ? <Unreachable /> : null;
  if (user === null) return <Login onLogin={setUser} />;

  const ctx: AuthCtx = {
    user,
    can: (role) => RANK[user.role] >= RANK[role],
    logout: () => api.post("/auth/logout").finally(() => setUser(null)),
    setUser,
  };
  return (
    <Ctx.Provider value={ctx}>
      <Shell />
    </Ctx.Provider>
  );
}

function Unreachable() {
  const [busy, setBusy] = useState(false);
  return (
    <div className="login card">
      <h1>AIOps</h1>
      <p className="err" style={{ marginBottom: 12 }}>Cannot reach the AIOps platform.</p>
      <p className="sub" style={{ marginBottom: 16 }}>Check VPN or network. Retrying automatically...</p>
      <button disabled={busy} onClick={() => { setBusy(true); probe().finally(() => setBusy(false)); }}>
        {busy ? "Checking..." : "Retry now"}
      </button>
    </div>
  );
}

function ConnectionBanner() {
  const [message, setMessage] = useState<string | null>(isOnline() ? null : LOST_CONNECTION);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const down = (e: Event) => setMessage((e as CustomEvent<string>).detail || LOST_CONNECTION);
    const up = () => setMessage(null);
    window.addEventListener("aiops:offline", down);
    window.addEventListener("aiops:online", up);
    return () => {
      window.removeEventListener("aiops:offline", down);
      window.removeEventListener("aiops:online", up);
    };
  }, []);
  if (!message) return null;
  return (
    <div className="banner bad conn-banner" role="alert">
      <span>{message} Retrying automatically...</span>
      <button disabled={busy} onClick={() => { setBusy(true); probe().finally(() => setBusy(false)); }}>
        {busy ? "Checking..." : "Retry now"}
      </button>
    </div>
  );
}

function Shell() {
  const { can } = useAuth();
  const { data: summary } = useAsync(() => api.get<Record<string, Record<string, number>>>("/items/summary"), [], 30000);
  const { data: settings } = useAsync(() => api.get<{ kill_switch: boolean; global_autonomy_cap: number }>("/settings"), [], 30000);
  const pending = Object.values(summary || {}).reduce((n, s) => n + (s.proposed || 0), 0);

  return (
    <div className="layout">
      <aside className="sidebar">
        <div className="brand">
          AIOps
          <small>Local AI operations assistant</small>
        </div>
        <nav className="nav">
          <NavLink to="/" end>Dashboard</NavLink>
          <NavLink to="/review">
            Review queue {pending > 0 && <span className="count">{pending}</span>}
          </NavLink>
          <NavLink to="/tasks">Recurring tasks</NavLink>
          <NavLink to="/runs">Run history</NavLink>
          <NavLink to="/runners">Runners</NavLink>
          {can("admin") && <NavLink to="/logs">Container logs</NavLink>}
          <NavLink to="/sources">Data sources</NavLink>
          {can("operator") && <NavLink to="/audit">Audit log</NavLink>}
          {can("admin") && <NavLink to="/users">Users</NavLink>}
          <NavLink to="/settings">Settings</NavLink>
        </nav>
        <UserMenu />
      </aside>
      <main className="main">
        <ConnectionBanner />
        {settings?.kill_switch && (
          <div className="banner bad">
            Kill switch is ON: no changes are executed against the network. Proposals are still generated and can be reviewed.
          </div>
        )}
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/review" element={<Review />} />
          <Route path="/tasks" element={<Tasks />} />
          <Route path="/tasks/:key" element={<TaskDetail />} />
          <Route path="/runs" element={<Runs />} />
          <Route path="/runners" element={<Runners />} />
          <Route path="/logs" element={<Logs />} />
          <Route path="/sources" element={<Sources />} />
          <Route path="/sources/:id/geoip" element={<GeoipSource />} />
          <Route path="/audit" element={<Audit />} />
          <Route path="/users" element={<Users />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="*" element={<Navigate to="/" />} />
        </Routes>
      </main>
    </div>
  );
}
