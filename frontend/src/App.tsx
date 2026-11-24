import { createContext, ReactNode, useContext, useEffect, useRef, useState } from "react";
import { NavLink, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { api, ConnectionError, isOnline, LOST_CONNECTION, probe, TERMS_URL, TermsRecord } from "./api";
import { useAsync } from "./components";
import { applyTheme } from "./theme";
import { Role, User } from "./types";
import UserMenu from "./UserMenu";
import { NewDot, NewHere, NewsProvider, useNews } from "./whatsNew";
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
import Automation from "./pages/Automation";
import AiAssistance from "./pages/AiAssistance";
import Updates from "./pages/Updates";

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
    window.addEventListener("steward:unauthorized", h);
    window.addEventListener("steward:online", retryMe);
    return () => {
      window.removeEventListener("steward:unauthorized", h);
      window.removeEventListener("steward:online", retryMe);
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
      <h1>Steward</h1>
      <p className="err" style={{ marginBottom: 12 }}>Cannot reach the Steward platform.</p>
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
    window.addEventListener("steward:offline", down);
    window.addEventListener("steward:online", up);
    return () => {
      window.removeEventListener("steward:offline", down);
      window.removeEventListener("steward:online", up);
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
  const { user, can } = useAuth();
  return (
    <NewsProvider username={user.username} admin={can("admin")}>
      <ShellBody />
    </NewsProvider>
  );
}

/** Menu link with a blue dot while its page has changes the user has not seen. */
function Item({ to, end, children }: { to: string; end?: boolean; children: ReactNode }) {
  const { hasNew } = useNews();
  return (
    <NavLink to={to} end={end}>
      <span>{children}{hasNew(to) && <NewDot />}</span>
    </NavLink>
  );
}

function ShellBody() {
  const { can } = useAuth();
  const { hasNew } = useNews();
  const { data: summary } = useAsync(() => api.get<Record<string, Record<string, number>>>("/items/summary"), [], 30000);
  const { data: settings } = useAsync(() => api.get<{ kill_switch: boolean; global_autonomy_cap: number }>("/settings"), [], 30000);
  const { data: terms } = useAsync(() => api.get<TermsRecord>("/terms"), [], 300000);
  const pending = Object.values(summary || {}).reduce((n, s) => n + (s.proposed || 0), 0);

  return (
    <div className="layout">
      <aside className="sidebar">
        <div className="brand">
          Steward
          <small>IT Operations Assistant</small>
        </div>
        <nav className="nav">
          <div className="nav-group">
            <div className="nav-title">Work</div>
            <Item to="/" end>Dashboard</Item>
            <NavLink to="/inbox">
              <span>Inbox{hasNew("/inbox") && <NewDot />}</span> {pending > 0 && <span className="count">{pending}</span>}
            </NavLink>
            <Item to="/history">History</Item>
          </div>
          <NavFlyout title="Observe" paths={OBSERVE}>
            <Item to="/runs">Expert Task history</Item>
            <Item to="/runners">Runners</Item>
            {can("admin") && <Item to="/logs">Container logs</Item>}
            {can("admin") && <Item to="/audit">Audit log</Item>}
          </NavFlyout>
          {can("admin") && (
            <NavFlyout title="Configure" paths={CONFIGURE}>
              <Item to="/tasks">Expert Tasks</Item>
              <Item to="/sources">Data sources</Item>
              <Item to="/automation">Automation</Item>
              <Item to="/ai">AI assistance</Item>
              <Item to="/updates">Updates</Item>
              <Item to="/users">Users</Item>
            </NavFlyout>
          )}
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
        {can("admin") && terms && !terms.accepted && (
          <div className="banner warn">
            Steward's <a href={TERMS_URL} target="_blank" rel="noreferrer">terms</a> are not accepted on this installation yet.
            Run <code>sudo /opt/Steward/scripts/accept-terms.sh</code> on the host.
          </div>
        )}
        <NewHere />
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/inbox" element={<Review view="inbox" />} />
          <Route path="/history" element={<Review view="history" />} />
          <Route path="/activity" element={<Navigate to="/history" replace />} />
          <Route path="/review" element={<LegacyReview />} />
          <Route path="/tasks" element={<Admin><Tasks /></Admin>} />
          <Route path="/tasks/:key" element={<Admin><TaskDetail /></Admin>} />
          <Route path="/runs" element={<Runs />} />
          <Route path="/runners" element={<Runners />} />
          <Route path="/logs" element={<Admin><Logs /></Admin>} />
          <Route path="/sources" element={<Admin><Sources /></Admin>} />
          <Route path="/sources/:id/geoip" element={<Admin><GeoipSource /></Admin>} />
          <Route path="/audit" element={<Admin><Audit /></Admin>} />
          <Route path="/users" element={<Admin><Users /></Admin>} />
          <Route path="/automation" element={<Admin><Automation /></Admin>} />
          <Route path="/ai" element={<Admin><AiAssistance /></Admin>} />
          <Route path="/updates" element={<Admin><Updates /></Admin>} />
          <Route path="/settings" element={<LegacySettings />} />
          <Route path="*" element={<Navigate to="/" />} />
        </Routes>
      </main>
    </div>
  );
}

const CONFIGURE = ["/tasks", "/sources", "/automation", "/ai", "/updates", "/users"];
const OBSERVE = ["/runs", "/runners", "/logs", "/audit"];

function Admin({ children }: { children: ReactNode }) {
  return useAuth().can("admin") ? <>{children}</> : <Navigate to="/" replace />;
}

/** Settings was split into Automation, AI assistance and Updates. */
function LegacySettings() {
  const { hash } = useLocation();
  return <Navigate to={hash === "#updates" ? "/updates" : hash === "#ai" ? "/ai" : "/automation"} replace />;
}

/** Old /review links: open items go to the Inbox, decided ones to History. */
function LegacyReview() {
  const { search } = useLocation();
  const statuses = new URLSearchParams(search).getAll("status").flatMap((v) => v.split(","));
  const decided = statuses.length > 0 && statuses.every((v) => v !== "proposed" && v !== "reported");
  return <Navigate to={`/${decided ? "history" : "inbox"}${search}`} replace />;
}

/** Collapsed group: hovering, focusing or clicking the row opens its pages in a panel beside the sidebar. */
function NavFlyout({ title, paths, children }: { title: string; paths: string[]; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const timer = useRef<number>();
  const { pathname } = useLocation();
  const here = paths.some((p) => pathname === p || pathname.startsWith(`${p}/`));
  const { hasNew } = useNews();
  const show = () => {
    window.clearTimeout(timer.current);
    setOpen(true);
  };
  const hide = () => {
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setOpen(false), 150);
  };
  useEffect(() => setOpen(false), [pathname]);
  useEffect(() => () => window.clearTimeout(timer.current), []);
  return (
    <div className="nav-flyout-wrap" onMouseEnter={show} onMouseLeave={hide}
      onBlur={(e) => { if (!e.currentTarget.contains(e.relatedTarget as Node | null)) hide(); }}
      onKeyDown={(e) => { if (e.key === "Escape") setOpen(false); }}>
      <button className={`nav-parent ${here ? "active" : ""} ${open ? "open" : ""}`} aria-haspopup="true" aria-expanded={open}
        onClick={() => setOpen(!open)}>
        <span>{title}{hasNew(...paths) && <NewDot />}</span><span className="chev" aria-hidden>{"\u203A"}</span>
      </button>
      {open && (
        <div className="nav-flyout" onClick={(e) => { if ((e.target as HTMLElement).closest("a")) setOpen(false); }}>
          <div className="nav-flyout-head">{title}</div>
          {children}
        </div>
      )}
    </div>
  );
}
