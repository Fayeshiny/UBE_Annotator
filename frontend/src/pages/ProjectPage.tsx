import { useCallback, useEffect, useState } from "react";
import { roleName, useApp } from "../App";
import AgreementView from "../components/AgreementView";
import { api } from "../lib/api";
import { fmtNum, fmtPct } from "../lib/time";
import type { AnchorField, Settings, User } from "../lib/types";

/* eslint-disable @typescript-eslint/no-explicit-any */

export default function ProjectPage() {
  const { user } = useApp();
  const [tab, setTab] = useState("overview");
  return (
    <div className="page">
      <div className="tabs">
        {[
          ["overview", "概览与导出"],
          ["agreement", "Pilot 一致性"],
          ["sampling", "采样间隔模拟"],
          ...(user.role === "admin" ? [["settings", "规则设置"], ["users", "用户"]] : []),
        ].map(([k, l]) => (
          <button key={k} className={tab === k ? "tab active" : "tab"} onClick={() => setTab(k)}>
            {l}
          </button>
        ))}
      </div>
      {tab === "overview" && <Overview />}
      {tab === "agreement" && <AgreementView url="/api/project/agreement" />}
      {tab === "sampling" && <Sampling />}
      {tab === "settings" && <SettingsPanel />}
      {tab === "users" && <Users />}
    </div>
  );
}

function Overview() {
  const [s, setS] = useState<any>(null);
  const [msg, setMsg] = useState("");
  useEffect(() => {
    api.get("/api/project/stats").then(setS);
  }, []);
  if (!s) return null;
  return (
    <div className="card">
      <div className="stats">
        <div>
          <b>{s.cases}</b>
          <span>病例</span>
        </div>
        <div>
          <b>{s.gold}</b>
          <span>金标准</span>
        </div>
        <div>
          <b>{s.locked}</b>
          <span>已锁定</span>
        </div>
        {Object.entries(s.assignments as Record<string, number>).map(([k, v]) => (
          <div key={k}>
            <b>{v}</b>
            <span>任务·{k}</span>
          </div>
        ))}
      </div>
      <h4>导出项目级主表</h4>
      <p className="muted small">cases.csv、context_intervals.csv、sampled_ssi.csv、orientation_loss.csv、milestones.csv、mask_index.csv、metrics.csv、review_log.csv</p>
      <p className="muted small">最终导出仅纳入已复核通过并锁定的病例，排除清单见导出包。全部标注者导出可包含未复核数据。</p>
      <div className="row">
        <a className="btn primary" href="/api/project/export.zip?mode=final">
          导出已锁定最终版本
        </a>
        <a className="btn" href="/api/project/export.zip?mode=all">
          导出全部标注者工作版本（一致性分析用）
        </a>
        <button
          className="btn"
          onClick={async () => {
            const r = await api.post<{ written: string[]; skipped: { case_id: string; reason: string }[] }>("/api/project/write-folders");
            setMsg(`已写入 ${r.written.length} 个已锁定病例的文件夹` + (r.skipped.length
              ? `；跳过 ${r.skipped.map((c) => `${c.case_id}：${c.reason}`).join("；")}` : ""));
          }}
        >
          重写已锁定病例文件夹
        </button>
        {msg && <span className="muted">{msg}</span>}
      </div>
    </div>
  );
}

function Sampling() {
  const { protocol } = useApp();
  const [d, setD] = useState<any>(null);
  useEffect(() => {
    api.get("/api/project/sampling-simulation").then(setD);
  }, []);
  if (!d) return <p className="muted">计算中…</p>;
  const base = d.base_interval_ms;
  const ivs = [base, base * 2, base * 3];
  return (
    <div className="card">
      <p>
        用 {base / 1000}s 参考数据模拟 {base / 500}s 与 {(base * 3) / 1000}s 采样。病例级 SCS 稳定比例与 LPS 的 ICC(A,1) ≥ {d.target} 时，可采用更稀疏的间隔（在“规则设置”中修改后冻结）。
      </p>
      <table className="table compact">
        <thead>
          <tr>
            <th>间隔</th>
            <th>SCS 稳定比例 ICC</th>
            {protocol.anchors.map((a) => (
              <th key={a.field}>LPS {a.label}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {Object.entries(d.icc as Record<string, any>).map(([iv, v]) => (
            <tr key={iv}>
              <td>{Number(iv) / 1000}s vs {base / 1000}s</td>
              <td className={v.scs_stable_ratio >= d.target ? "good" : "bad"}>{fmtNum(v.scs_stable_ratio, 3)}</td>
              {protocol.anchors.map((a) => (
                <td key={a.field} className={v[`LPS_${a.field}`] >= d.target ? "good" : "bad"}>
                  {fmtNum(v[`LPS_${a.field}`], 3)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      <details>
        <summary>逐病例数值（{d.cases.length} 例）</summary>
        <table className="table compact">
          <thead>
            <tr>
              <th>病例</th>
              {ivs.map((iv) => (
                <th key={iv}>SCS 稳定 @{iv / 1000}s</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {d.cases.map((c: any) => (
              <tr key={c.case_id}>
                <td>{c.case_id}</td>
                {ivs.map((iv) => (
                  <td key={iv}>{fmtPct(c.scs_stable_ratio[iv])}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </div>
  );
}

function SettingsPanel() {
  const { protocol, reloadProtocol } = useApp();
  const [s, setS] = useState<Settings>(protocol.settings);
  const [msg, setMsg] = useState("");
  const frozen = protocol.settings.rules_frozen;

  async function save(values: Partial<Settings>) {
    setMsg("");
    try {
      await api.put("/api/settings", { values });
      await reloadProtocol();
      setMsg("已保存");
    } catch (e) {
      setMsg((e as Error).message);
    }
  }

  function toggleApplic(ctx: string, f: AnchorField) {
    const cur = new Set(s.anchor_applicability[ctx] ?? []);
    if (cur.has(f)) cur.delete(f);
    else cur.add(f);
    setS({ ...s, anchor_applicability: { ...s.anchor_applicability, [ctx]: [...cur] as AnchorField[] } });
  }

  return (
    <div className="card">
      {frozen && <div className="notice">规则已冻结：采样间隔、事件阈值与锚点适用关系不可修改。</div>}
      <div className="form-grid">
        <label>
          SCS / 锚点采样间隔（毫秒）
          <input type="number" disabled={frozen} value={s.sampling_interval_ms} onChange={(e) => setS({ ...s, sampling_interval_ms: Number(e.target.value) })} />
        </label>
        <label>
          采样点回看窗口（± 毫秒）
          <input type="number" value={s.sample_review_window_ms} onChange={(e) => setS({ ...s, sample_review_window_ms: Number(e.target.value) })} />
        </label>
        <label>
          定向丢失最短持续（毫秒）
          <input type="number" disabled={frozen} value={s.ol_min_duration_ms} onChange={(e) => setS({ ...s, ol_min_duration_ms: Number(e.target.value) })} />
        </label>
        <label>
          恢复所需 SCS=2 持续（毫秒）
          <input type="number" disabled={frozen} value={s.recovery_min_stable_ms} onChange={(e) => setS({ ...s, recovery_min_stable_ms: Number(e.target.value) })} />
        </label>
      </div>
      <h4>锚点适用关系（控制 NA 锁定与 LPS 统计）</h4>
      <p className="muted small">勾选表示该锚点在该空间情境下“适用”（应记录 V/N/U）；未勾选的锚点固定为 NA，不能手动修改，且不进入 LPS 统计。需在 Pilot 中由专家确认。</p>
      <table className="table compact">
        <thead>
          <tr>
            <th>情境</th>
            {protocol.anchors.map((a) => (
              <th key={a.field}>{a.label}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {protocol.contexts.map((c) => (
            <tr key={c.code}>
              <td>
                {c.code} {c.label}
              </td>
              {protocol.anchors.map((a) => (
                <td key={a.field}>
                  <input
                    type="checkbox"
                    disabled={frozen}
                    checked={(s.anchor_applicability[c.code] ?? []).includes(a.field)}
                    onChange={() => toggleApplic(c.code, a.field)}
                  />
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      <div className="row">
        <button className="btn primary" onClick={() => save(s)}>
          保存
        </button>
        {!frozen ? (
          <button
            className="btn danger"
            onClick={() => {
              if (confirm("冻结规则后不能再修改采样间隔与事件规则。确认 Pilot 已完成？")) save({ ...s, rules_frozen: true });
            }}
          >
            冻结规则
          </button>
        ) : (
          <button
            className="btn"
            onClick={() => {
              if (confirm("解除冻结只应在方案修订时使用。后续规则修改会应用于未锁定任务；已锁定最终版本继续使用各自的规则快照。确定？")) save({ rules_frozen: false });
            }}
          >
            解除冻结
          </button>
        )}
        {msg && <span className="muted">{msg}</span>}
      </div>
    </div>
  );
}

function Users() {
  const { user } = useApp();
  const [deleting, setDeleting] = useState<number | null>(null);
  const [savingRole, setSavingRole] = useState<number | null>(null);
  const [roles, setRoles] = useState<Record<number, User["role"]>>({});
  const [users, setUsers] = useState<User[]>([]);
  const [f, setF] = useState({ username: "", display_name: "", role: "annotator", password: "" });
  const [err, setErr] = useState("");
  const load = useCallback(() => api.get<User[]>("/api/users").then(setUsers), []);
  useEffect(() => {
    load();
  }, [load]);

  async function create(e: React.FormEvent) {
    e.preventDefault();
    setErr("");
    try {
      await api.post("/api/users", f);
      setF({ username: "", display_name: "", role: "annotator", password: "" });
      load();
    } catch (e) {
      setErr((e as Error).message);
    }
  }

  async function changeRole(u: User) {
    const role = roles[u.id] ?? u.role;
    if (role === u.role) return;
    if (!confirm(`将 ${u.username} 的角色从“${roleName(u.role)}”改为“${roleName(role)}”？`)) return;
    setErr("");
    setSavingRole(u.id);
    try {
      await api.patch(`/api/users/${u.id}`, { role });
      if (u.id === user.id) {
        window.location.reload();
        return;
      }
      await load();
      setRoles((current) => {
        const next = { ...current };
        delete next[u.id];
        return next;
      });
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setSavingRole(null);
    }
  }

  async function remove(u: User) {
    if (!confirm(`确定删除账号 ${u.username}（${u.display_name}）？删除后不能登录，已有标注和复核署名将保留；有关联记录的用户名不可重新注册。`)) return;
    setErr("");
    setDeleting(u.id);
    try {
      await api.del(`/api/users/${u.id}`);
      await load();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setDeleting(null);
    }
  }

  return (
    <div className="card">
      <p className="muted small">修改角色后点击“保存角色”，相关用户刷新页面即可更新界面权限。角色修改保留原有任务和记录，最后一个启用的管理员不能降级。</p>
      <p className="muted small">删除账号后立即禁止登录，并保留已有标注和复核署名；当前登录账号不能删除。</p>
      <table className="table compact">
        <thead>
          <tr>
            <th>账号（annotator_id）</th>
            <th>姓名</th>
            <th>角色</th>
            <th>状态</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {users.map((u) => (
            <tr key={u.id}>
              <td>{u.username}</td>
              <td>{u.display_name}</td>
              <td>
                <div className="row tight">
                  <select
                    aria-label={`修改 ${u.username} 的角色`}
                    value={roles[u.id] ?? u.role}
                    disabled={savingRole !== null || deleting !== null}
                    onChange={(e) => setRoles((current) => ({ ...current, [u.id]: e.target.value as User["role"] }))}
                  >
                    <option value="annotator">标注者</option>
                    <option value="reviewer">复核专家</option>
                    <option value="admin">管理员</option>
                  </select>
                  {(roles[u.id] ?? u.role) !== u.role && (
                    <button className="btn small primary" disabled={savingRole !== null || deleting !== null} onClick={() => changeRole(u)}>
                      {savingRole === u.id ? "保存中…" : "保存角色"}
                    </button>
                  )}
                </div>
              </td>
              <td>{u.active ? "启用" : "停用"}</td>
              <td className="actions">
                <button className="btn small" onClick={() => api.patch(`/api/users/${u.id}`, { active: !u.active }).then(load).catch((e) => setErr((e as Error).message))}>
                  {u.active ? "停用" : "启用"}
                </button>
                <button
                  className="btn small"
                  onClick={() => {
                    const p = prompt(`为 ${u.username} 设置新密码（至少 8 位）`);
                    if (p) api.patch(`/api/users/${u.id}`, { password: p }).then(load).catch((e) => alert(e.message));
                  }}
                >
                  重置密码
                </button>
                <button
                  className="btn small danger"
                  disabled={u.id === user.id || deleting !== null || savingRole !== null}
                  title={u.id === user.id ? "不能删除当前登录账号" : "删除账号，保留历史标注和复核记录"}
                  onClick={() => remove(u)}
                >
                  {deleting === u.id ? "删除中…" : "删除"}
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <h4>新增用户</h4>
      <form className="row" onSubmit={create}>
        <input required placeholder="账号，如 A01 / EXP01" value={f.username} onChange={(e) => setF({ ...f, username: e.target.value })} />
        <input required placeholder="姓名" value={f.display_name} onChange={(e) => setF({ ...f, display_name: e.target.value })} />
        <select value={f.role} onChange={(e) => setF({ ...f, role: e.target.value })}>
          <option value="annotator">标注者</option>
          <option value="reviewer">复核专家</option>
          <option value="admin">管理员</option>
        </select>
        <input required type="password" placeholder="初始密码（≥8 位）" value={f.password} onChange={(e) => setF({ ...f, password: e.target.value })} />
        <button className="btn primary">创建</button>
      </form>
      {err && <div className="error">{err}</div>}
    </div>
  );
}
