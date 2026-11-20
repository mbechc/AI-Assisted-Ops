import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "./App";
import { api } from "./api";
import { Modal, useAsync } from "./components";
import { applyTheme, Theme } from "./theme";
import { UpdateState, User } from "./types";

export default function UserMenu() {
  const { user, logout, setUser, can } = useAuth();
  const admin = can("admin");
  const upd = useAsync(() => (admin ? api.get<UpdateState>("/updates") : Promise.resolve(null)), [admin], 300000);
  const update = upd.data?.available;
  const nav = useNavigate();
  const [open, setOpen] = useState(false);
  const [pwOpen, setPwOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const theme: Theme = user.theme === "light" ? "light" : "dark";

  useEffect(() => {
    if (!open) return;
    const click = (e: MouseEvent) => ref.current && !ref.current.contains(e.target as Node) && setOpen(false);
    const key = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", click);
    document.addEventListener("keydown", key);
    return () => {
      document.removeEventListener("mousedown", click);
      document.removeEventListener("keydown", key);
    };
  }, [open]);

  const setTheme = async (t: Theme) => {
    applyTheme(t);
    setUser({ ...user, theme: t });
    try {
      setUser(await api.put<User>("/auth/me/preferences", { theme: t }));
    } catch {
      /* kept locally; saved on the next change */
    }
  };

  return (
    <div className="foot" ref={ref}>
      {open && (
        <div className="user-menu" role="menu">
          <div className="label">Theme</div>
          <div className="segmented">
            {(["light", "dark"] as Theme[]).map((t) => (
              <button key={t} className={theme === t ? "on" : ""} onClick={() => setTheme(t)}>
                {t === "light" ? "☀ Light" : "☾ Dark"}
              </button>
            ))}
          </div>
          <div className="sep" />
          {update && (
            <button className="item" role="menuitem" onClick={() => { setOpen(false); nav("/updates"); }}>
              Update available: {update.version}
            </button>
          )}
          <button className="item" role="menuitem" onClick={() => { setOpen(false); setPwOpen(true); }}>Change password</button>
          <button className="item danger" role="menuitem" onClick={logout}>Log out</button>
        </div>
      )}
      <button className={`user-btn ${open ? "open" : ""}`} onClick={() => setOpen(!open)} aria-haspopup="menu" aria-expanded={open}>
        <span className="avatar">{user.username.slice(0, 1).toUpperCase()}
          {update && <span className="update-dot" title={`Update ${update.version} available`} />}</span>
        <span className="who"><b>{user.username}</b><span>{user.role}</span></span>
        <span className="caret">{open ? "▼" : "▲"}</span>
      </button>
      {pwOpen && <ChangePassword onClose={() => setPwOpen(false)} />}
    </div>
  );
}

function ChangePassword({ onClose }: { onClose: () => void }) {
  const [pw, setPw] = useState({ current_password: "", new_password: "", confirm: "" });
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const mismatch = pw.confirm.length > 0 && pw.confirm !== pw.new_password;

  const save = async () => {
    setMsg(null);
    try {
      await api.post("/auth/password", { current_password: pw.current_password, new_password: pw.new_password });
      setMsg({ ok: true, text: "Password changed." });
      setPw({ current_password: "", new_password: "", confirm: "" });
    } catch (e: any) {
      setMsg({ ok: false, text: e.message });
    }
  };

  return (
    <Modal onClose={onClose}>
      <h2>Change password</h2>
      <label className="field">Current password
        <input type="password" autoFocus autoComplete="current-password" value={pw.current_password}
          onChange={(e) => setPw({ ...pw, current_password: e.target.value })} /></label>
      <label className="field">New password (min 10 characters)
        <input type="password" autoComplete="new-password" value={pw.new_password}
          onChange={(e) => setPw({ ...pw, new_password: e.target.value })} /></label>
      <label className="field">Repeat new password
        <input type="password" autoComplete="new-password" value={pw.confirm}
          onChange={(e) => setPw({ ...pw, confirm: e.target.value })} /></label>
      {mismatch && <p className="small" style={{ color: "var(--bad)" }}>The new passwords do not match.</p>}
      {msg && <div className={`banner ${msg.ok ? "info" : "bad"}`}>{msg.text}</div>}
      <div className="row end">
        <button onClick={onClose}>{msg?.ok ? "Close" : "Cancel"}</button>
        <button className="primary" onClick={save}
          disabled={!pw.current_password || pw.new_password.length < 10 || pw.new_password !== pw.confirm}>Change password</button>
      </div>
    </Modal>
  );
}
