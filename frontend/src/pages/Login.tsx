import { FormEvent, useState } from "react";
import { api, TERMS_URL } from "../api";
import { User } from "../types";

export default function Login({ onLogin }: { onLogin: (u: User) => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onLogin(await api.post<User>("/auth/login", { username, password }));
    } catch (err: any) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login card">
      <h1>Steward</h1>
      <p className="sub" style={{ marginBottom: 16 }}>Sign in to review and automate operational tasks.</p>
      <form method="post" action="/login" onSubmit={submit}>
        <label className="field">
          Username
          <input id="username" name="username" autoComplete="username" autoCapitalize="none" spellCheck={false} autoFocus value={username} onChange={(e) => setUsername(e.target.value)} />
        </label>
        <label className="field">
          Password
          <input id="password" name="password" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} />
        </label>
        {error && <p className="err">{error}</p>}
        <button className="primary" disabled={busy || !username || !password} type="submit">Sign in</button>
      </form>
      <p className="small muted" style={{ marginTop: 16 }}>
        Steward is an independent open-source project, not a Cisco product. Provided as is, without warranty or
        support. <a href={TERMS_URL} target="_blank" rel="noreferrer">Terms</a>
      </p>
    </div>
  );
}
