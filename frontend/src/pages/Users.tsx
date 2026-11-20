import { useState } from "react";
import { api, fmtDate } from "../api";
import { ErrorLine, Modal, useAsync } from "../components";
import { Role, User } from "../types";

export default function Users() {
  const users = useAsync(() => api.get<User[]>("/users"), []);
  const [form, setForm] = useState<{ username: string; password: string; role: Role } | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const patch = async (u: User, body: Record<string, unknown>) => {
    setErr(null);
    try {
      await api.patch(`/users/${u.id}`, body);
      users.reload();
    } catch (e: any) {
      setErr(e.message);
    }
  };

  const create = async () => {
    if (!form) return;
    setErr(null);
    try {
      await api.post("/users", form);
      setForm(null);
      users.reload();
    } catch (e: any) {
      setErr(e.message);
    }
  };

  return (
    <>
      <div className="page-head">
        <div><h1>Users</h1><p className="sub">
          Viewer: read only. Operator: decides in the Inbox. Admin: everything, including Configure, Container logs and the
          Audit log. See <a href="https://github.com/mbechc/AI-Assisted-Ops/blob/master/docs/PERMISSIONS.md" target="_blank" rel="noreferrer">Roles and permissions</a>.
        </p></div>
        <button className="primary" onClick={() => setForm({ username: "", password: "", role: "operator" })}>Add user</button>
      </div>
      <ErrorLine error={err || users.error} />
      <div className="card">
        <table>
          <thead><tr><th>User</th><th>Role</th><th>Active</th><th>Last login</th><th /></tr></thead>
          <tbody>
            {(users.data || []).map((u) => (
              <tr key={u.id}>
                <td>{u.username}</td>
                <td>
                  <select value={u.role} onChange={(e) => patch(u, { role: e.target.value })}>
                    <option value="viewer">viewer</option><option value="operator">operator</option><option value="admin">admin</option>
                  </select>
                </td>
                <td><input type="checkbox" checked={u.is_active} onChange={(e) => patch(u, { is_active: e.target.checked })} /></td>
                <td className="small muted">{fmtDate(u.last_login)}</td>
                <td className="num"><button onClick={() => { const p = prompt("New password (min 10 characters)"); if (p) patch(u, { password: p }); }}>Reset password</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {form && (
        <Modal onClose={() => setForm(null)}>
          <h2>Add user</h2>
          <ErrorLine error={err} />
          <label className="field">Username<input autoComplete="off" value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} /></label>
          <label className="field">Password (min 10 characters)<input type="password" autoComplete="new-password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} /></label>
          <label className="field">Role
            <select value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value as Role })}>
              <option value="viewer">viewer</option><option value="operator">operator</option><option value="admin">admin</option>
            </select>
          </label>
          <div className="row end"><button onClick={() => setForm(null)}>Cancel</button><button className="primary" onClick={create}>Create</button></div>
        </Modal>
      )}
    </>
  );
}
