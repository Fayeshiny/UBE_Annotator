import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { roleName, statusName, useApp } from "../App";
import { api } from "../lib/api";
import { fmtTime } from "../lib/time";
import type { Case, User } from "../lib/types";
import AgreementView from "../components/AgreementView";

export default function CasesPage() {
  const { user } = useApp();
  const isReviewer = user.role !== "annotator";
  const [cases, setCases] = useState<Case[]>([]);
  const [users, setUsers] = useState<User[]>([]);
  const [filter, setFilter] = useState("");
  const [showImport, setShowImport] = useState(false);
  const [agreementCase, setAgreementCase] = useState<string | null>(null);
  const [err, setErr] = useState("");

  const load = useCallback(async () => {
    setCases(await api.get<Case[]>("/api/cases"));
    if (isReviewer) setUsers(await api.get<User[]>("/api/users"));
  }, [isReviewer]);

  useEffect(() => {
    load();
  }, [load]);

  // 视频处理中时自动刷新
  useEffect(() => {
    if (!cases.some((c) => c.media_status === "processing")) return;
    const t = setTimeout(load, 3000);
    return () => clearTimeout(t);
  }, [cases, load]);

  const shown = useMemo(
    () => cases.filter((c) => !filter || `${c.case_id} ${c.surgeon_id ?? ""} ${c.center_id ?? ""}`.toLowerCase().includes(filter.toLowerCase())),
    [cases, filter],
  );

  async function act(fn: () => Promise<unknown>) {
    setErr("");
    try {
      await fn();
      await load();
    } catch (e) {
      setErr((e as Error).message);
    }
  }

  if (!isReviewer) return <MyTasks cases={cases} />;

  return (
    <div className="page">
      <div className="row">
        <h2>病例</h2>
        <span className="muted">
          共 {cases.length} 例 · 金标准 {cases.filter((c) => c.gold_set).length} 例 · 已锁定 {cases.filter((c) => c.status === "locked").length} 例
        </span>
        <div className="spacer" />
        <input placeholder="搜索病例/术者/中心" value={filter} onChange={(e) => setFilter(e.target.value)} />
        {user.role === "admin" && (
          <button className="btn primary" onClick={() => setShowImport((v) => !v)}>
            {showImport ? "收起" : "导入病例"}
          </button>
        )}
      </div>
      {err && <div className="error">{err}</div>}
      {showImport && <ImportForm onDone={() => { setShowImport(false); load(); }} />}
      <table className="table">
        <thead>
          <tr>
            <th>病例</th>
            <th>术者 / 中心 / 节段</th>
            <th>视频</th>
            <th>类型</th>
            <th>标注任务</th>
            <th>状态</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          {shown.map((c) => (
            <CaseRow key={c.case_id} c={c} users={users} act={act} onAgreement={() => setAgreementCase(c.case_id)} />
          ))}
        </tbody>
      </table>
      {agreementCase && (
        <div className="modal-bg" onClick={() => setAgreementCase(null)}>
          <div className="modal wide" onClick={(e) => e.stopPropagation()}>
            <div className="row">
              <h3>{agreementCase} 标注者间一致性</h3>
              <div className="spacer" />
              <button className="btn ghost" onClick={() => setAgreementCase(null)}>
                关闭
              </button>
            </div>
            <AgreementView url={`/api/cases/${agreementCase}/agreement`} perCase />
          </div>
        </div>
      )}
    </div>
  );
}

function CaseRow({ c, users, act, onAgreement }: { c: Case; users: User[]; act: (fn: () => Promise<unknown>) => void; onAgreement: () => void }) {
  const { user } = useApp();
  const nav = useNavigate();
  const [assignUser, setAssignUser] = useState("");
  const [assignRole, setAssignRole] = useState("primary");
  const [consensusId, setConsensusId] = useState("");
  const [finalId, setFinalId] = useState<string>("");
  const [workingId, setWorkingId] = useState("");
  const as = c.assignments ?? [];
  const mine = as.find((a) => a.user_id === user.id && a.role === "consensus");
  const approved = as.filter((a) => a.status === "approved"
    && (!c.gold_set || a.role === "consensus")
    && (a.role !== "consensus" || a.consensus?.complete));
  const consensusChoice = as.find((a) => String(a.id) === consensusId)?.id ?? as[0]?.id;
  const finalChoice = approved.find((a) => String(a.id) === finalId)?.id ?? approved[0]?.id;
  const workingChoice = as.find((a) => String(a.id) === workingId)?.id ?? as[0]?.id;

  return (
    <tr>
      <td>
        <b>{c.case_id}</b>
      </td>
      <td className="small">
        {c.surgeon_id ?? "—"} / {c.center_id ?? "—"} / {c.operative_level ?? "—"}
        {c.cumulative_UBE_cases != null && <div className="muted">累计 {c.cumulative_UBE_cases} 例</div>}
      </td>
      <td className="small">
        {c.media_status === "ready" ? (
          <>
            {fmtTime((c.video_duration_sec ?? 0) * 1000, false)} · {c.fps}fps
          </>
        ) : (
          <span className={c.media_status === "error" ? "error-text" : "muted"} title={c.media_error ?? ""}>
            {statusName(c.media_status)}
          </span>
        )}
      </td>
      <td>
        {c.gold_set ? <span className="badge gold">金标准</span> : <span className="badge">核心队列</span>}
        {c.status === "locked" && !c.locked_rule_snapshot_id && (
          <div className="error-text small">缺少历史规则快照，需解锁并重新复核锁定</div>
        )}
        {c.status === "open" && (
          <button
            className="btn link small"
            onClick={() => act(() => api.patch(`/api/cases/${c.case_id}`, { gold_set: !c.gold_set }))}
          >
            切换
          </button>
        )}
      </td>
      <td>
        {as.map((a) => (
          <div key={a.id} className="assign-line">
            <Link to={`/a/${a.id}`}>
              {a.display_name}（{a.username}）
            </Link>{" "}
            <span className="muted small">{roleName(a.role)}</span>{" "}
            <span className={`badge st-${a.status}`}>{statusName(a.status)}</span>
            {a.consensus && <span className="badge">共识 {a.consensus.approved_count}/{a.consensus.required}</span>}
            {c.status === "locked" && c.final_assignment_id === a.id && <span className="badge gold">最终</span>}
          </div>
        ))}
        {c.status === "open" && (
          <div className="row tight">
            <select value={assignUser} onChange={(e) => setAssignUser(e.target.value)}>
              <option value="">分配给…</option>
              {users
                .filter((u) => u.active)
                .map((u) => (
                  <option key={u.id} value={u.id}>
                    {u.display_name}（{u.username}）
                  </option>
                ))}
            </select>
            <select value={assignRole} onChange={(e) => setAssignRole(e.target.value)}>
              <option value="primary">主标注</option>
              <option value="secondary">重复标注（20%）</option>
              <option value="pilot">Pilot 独立标注</option>
            </select>
            <button
              className="btn small"
              disabled={!assignUser}
              onClick={() => act(() => api.post(`/api/cases/${c.case_id}/assignments`, { user_id: Number(assignUser), role: assignRole }))}
            >
              分配
            </button>
          </div>
        )}
      </td>
      <td>
        <span className={`badge st-${c.status}`}>{statusName(c.status)}</span>
      </td>
      <td className="actions">
        {as.length >= 2 && (
          <button className="btn small" onClick={onAgreement}>
            一致性
          </button>
        )}
        {c.status === "open" && as.length > 0 && !mine && (
          <div className="row tight version-actions">
            <label>
              建立共识版本
              <select className="small" value={consensusChoice ?? ""} onChange={(e) => setConsensusId(e.target.value)}>
                {as.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.username} · {roleName(a.role)}
                  </option>
                ))}
              </select>
            </label>
            <button
              className="btn small"
              disabled={consensusChoice == null}
              onClick={() => act(async () => {
                const r = await api.post<{ id: number }>(`/api/cases/${c.case_id}/consensus`, { from_assignment_id: consensusChoice });
                nav(`/a/${r.id}`);
              })}
            >
              建立
            </button>
          </div>
        )}
        {c.status === "open" && as.length > 0 && (
          <div className="row tight version-actions">
            <label>
              选择最终版本
              <select value={finalChoice ?? ""} disabled={!approved.length} onChange={(e) => setFinalId(e.target.value)}>
                {!approved.length && <option value="">暂无可用版本</option>}
                {approved.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.username} · {roleName(a.role)}
                  </option>
                ))}
              </select>
            </label>
            <button
              className="btn small"
              disabled={finalChoice == null}
              onClick={() => {
                if (confirm(`锁定 ${c.case_id}？将保存当前规则快照，锁定后所有标注不可修改，并写入病例文件夹。`))
                  act(() => api.post(`/api/cases/${c.case_id}/lock`, { final_assignment_id: finalChoice }));
              }}
            >
              锁定
            </button>
          </div>
        )}
        {c.status === "locked" && user.role === "admin" && (
          <button
            className="btn small"
            onClick={() => {
              const comment = prompt("解锁后已提交/通过的任务需重新提交、复核，共识需重新签署；旧规则快照保留。请填写解锁原因（记录到复核日志）");
              if (comment) act(() => api.post(`/api/cases/${c.case_id}/unlock`, { comment }));
            }}
          >
            解锁
          </button>
        )}
        {c.status === "locked" && c.locked_rule_snapshot_id && c.final_assignment_id && approved.some((a) => a.id === c.final_assignment_id) && (
          <a className="btn small" href={`/api/cases/${c.case_id}/export.zip?mode=final`}>
            导出已锁定最终版本
          </a>
        )}
        {as.length > 0 && (
          <div className="row tight version-actions">
            <label>
              选择工作版本
              <select value={workingChoice ?? ""} onChange={(e) => setWorkingId(e.target.value)}>
                {as.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.username} · {roleName(a.role)} · {statusName(a.status)}
                  </option>
                ))}
              </select>
            </label>
            {workingChoice != null && (
              <a className="btn small" href={`/api/cases/${c.case_id}/export.zip?mode=working&assignment_id=${workingChoice}`}>
                导出工作版本
              </a>
            )}
            <span className="muted small">工作版本可包含未复核数据；金标准最终版本须完成三位独立专家共识并锁定。</span>
          </div>
        )}
      </td>
    </tr>
  );
}

function MyTasks({ cases }: { cases: Case[] }) {
  const { user } = useApp();
  const rows = cases.flatMap((c) => (c.assignments ?? []).filter((a) => a.user_id === user.id).map((a) => ({ c, a })));
  return (
    <div className="page">
      <h2>我的标注任务</h2>
      {rows.length === 0 && <p className="muted">暂无分配给你的病例。</p>}
      <table className="table">
        <thead>
          <tr>
            <th>病例</th>
            <th>类型</th>
            <th>任务</th>
            <th>状态</th>
            <th>视频时长</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {rows.map(({ c, a }) => (
            <tr key={a.id}>
              <td>
                <b>{c.case_id}</b>
              </td>
              <td>{c.gold_set ? <span className="badge gold">金标准</span> : <span className="badge">核心队列</span>}</td>
              <td>{roleName(a.role)}</td>
              <td>
                <span className={`badge st-${a.status}`}>{statusName(a.status)}</span>
                {c.status === "locked" && <span className="badge st-locked">已锁定</span>}
              </td>
              <td>{c.media_status === "ready" ? fmtTime((c.video_duration_sec ?? 0) * 1000, false) : statusName(c.media_status)}</td>
              <td>
                {c.media_status === "ready" && (
                  <Link className="btn primary small" to={`/a/${a.id}`}>
                    {a.status === "in_progress" || a.status === "returned" ? "开始标注" : "查看"}
                  </Link>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ImportForm({ onDone }: { onDone: () => void }) {
  const [mode, setMode] = useState<"path" | "upload">("path");
  const [f, setF] = useState({
    case_id: "",
    source_path: "",
    surgeon_id: "",
    center_id: "",
    operative_level: "",
    cumulative_UBE_cases: "",
    gold_set: false,
    start_sec: "",
    end_sec: "",
  });
  const [file, setFile] = useState<File | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const set = (k: string, v: unknown) => setF((p) => ({ ...p, [k]: v }));

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setErr("");
    setBusy(true);
    const num = (s: string) => (s.trim() === "" ? null : Number(s));
    try {
      if (mode === "path") {
        await api.post("/api/cases/import", {
          ...f,
          cumulative_UBE_cases: num(f.cumulative_UBE_cases),
          start_sec: num(f.start_sec),
          end_sec: num(f.end_sec),
        });
      } else {
        if (!file) throw new Error("请选择视频文件");
        const fd = new FormData();
        fd.append("file", file);
        for (const [k, v] of Object.entries(f)) {
          if (k === "source_path" || v === "" || v === null) continue;
          fd.append(k, String(v));
        }
        await api.post("/api/cases/upload", fd);
      }
      onDone();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="card form-grid" onSubmit={submit}>
      <div className="full row">
        <label className="inline">
          <input type="radio" checked={mode === "path"} onChange={() => setMode("path")} /> 服务器本地路径（大文件推荐）
        </label>
        <label className="inline">
          <input type="radio" checked={mode === "upload"} onChange={() => setMode("upload")} /> 浏览器上传
        </label>
      </div>
      <label>
        病例编号 *
        <input required placeholder="SSI_0001" value={f.case_id} onChange={(e) => set("case_id", e.target.value)} />
      </label>
      {mode === "path" ? (
        <label className="span2">
          视频路径（服务器上）*
          <input required placeholder="/data/raw/xxx.mp4" value={f.source_path} onChange={(e) => set("source_path", e.target.value)} />
        </label>
      ) : (
        <label className="span2">
          视频文件 *
          <input type="file" accept="video/*" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
        </label>
      )}
      <label>
        术者编号
        <input placeholder="SUR_01" value={f.surgeon_id} onChange={(e) => set("surgeon_id", e.target.value)} />
      </label>
      <label>
        中心编号
        <input placeholder="C01" value={f.center_id} onChange={(e) => set("center_id", e.target.value)} />
      </label>
      <label>
        手术节段
        <input placeholder="L4/5" value={f.operative_level} onChange={(e) => set("operative_level", e.target.value)} />
      </label>
      <label>
        术者累计独立 UBE 例数
        <input type="number" value={f.cumulative_UBE_cases} onChange={(e) => set("cumulative_UBE_cases", e.target.value)} />
      </label>
      <label>
        有效起点（秒，可选）
        <input type="number" step="0.001" value={f.start_sec} onChange={(e) => set("start_sec", e.target.value)} />
      </label>
      <label>
        有效终点（秒，可选）
        <input type="number" step="0.001" value={f.end_sec} onChange={(e) => set("end_sec", e.target.value)} />
      </label>
      <label className="inline">
        <input type="checkbox" checked={f.gold_set} onChange={(e) => set("gold_set", e.target.checked)} /> 金标准病例
      </label>
      <div className="full muted small">
        导入时会去除音轨和容器元数据，并转码为可逐帧定位的 H.264。<b>画面内烧录的姓名等信息无法自动去除，请在导入前确认视频已脱敏。</b>
      </div>
      {err && <div className="full error">{err}</div>}
      <div className="full">
        <button className="btn primary" disabled={busy}>
          {busy ? "提交中…" : "导入"}
        </button>
      </div>
    </form>
  );
}
