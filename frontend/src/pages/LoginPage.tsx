import { useState } from "react";
import { api } from "../lib/api";
import type { User } from "../lib/types";

export default function LoginPage({ onLogin }: { onLogin: (u: User) => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErr("");
    try {
      onLogin(await api.post<User>("/api/auth/login", { username, password }));
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login">
      <form className="card login-card" onSubmit={submit}>
        <h1>UBE 外科空间智能标注平台</h1>
        <p className="muted small">SSI Protocol v1.0 · 时序视频标注与 SAM 辅助分割</p>
        <label>
          账号（标注者编号）
          <input autoFocus value={username} onChange={(e) => setUsername(e.target.value)} />
        </label>
        <label>
          密码
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} />
        </label>
        {err && <div className="error">{err}</div>}
        <button className="btn primary" disabled={busy || !username || !password}>
          登录
        </button>
      </form>
    </div>
  );
}
