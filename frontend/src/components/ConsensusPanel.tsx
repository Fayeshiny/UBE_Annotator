import { useState } from "react";
import { useApp } from "../App";
import { api } from "../lib/api";
import type { AssignmentPayload, ConsensusHistory } from "../lib/types";

export default function ConsensusPanel({ payload, onAction }: {
  payload: AssignmentPayload;
  onAction: (action: string, comment?: string) => void;
}) {
  const { user } = useApp();
  const [comment, setComment] = useState("");
  const [history, setHistory] = useState<ConsensusHistory[] | null>(null);
  const [error, setError] = useState("");
  const s = payload.consensus;
  if (!s) return null;
  const a = payload.assignment;
  const mine = a.user_id === user.id;
  const own = s.reviews.find((r) => r.reviewer_id === user.id);
  const canReview = !mine && user.role !== "annotator" && payload.case.status !== "locked" && s.current;
  const labels = { approve: "同意", reject: "退回", withdraw: "已撤回" };
  return (
    <div className="consensus-panel">
      <div className="row tight">
        <b>三专家共识</b>
        <span className={`badge ${s.complete ? "st-approved" : "st-submitted"}`}>独立确认 {s.approved_count}/{s.required}</span>
        <span className="muted small">{s.round ? `第 ${s.round} 轮` : "尚未提交"} · 内容版本 {s.revision} · 创建者不计入人数</span>
        {!payload.editable && <button className="btn tiny" onClick={() => window.location.reload()}>刷新结果</button>}
        <button className="btn tiny" onClick={async () => {
          if (history) { setHistory(null); return; }
          try { setHistory(await api.get<ConsensusHistory[]>(`/api/assignments/${a.id}/consensus-reviews`)); }
          catch (e) { setError((e as Error).message); }
        }}>{history ? "收起签署历史" : "查看签署历史"}</button>
      </div>
      <p className="muted small">
        {a.status === "returned" ? "本轮已退回。创建者修改并重新提交后，三位专家需重新确认。"
          : s.complete ? "本版本已完成三位独立专家确认，可返回病例列表选定并锁定。"
          : a.status === "submitted" ? "评审期间内容只读，需另外三位管理员或专家分别同意。需要修改时由创建者重新打开，原确认失效。"
          : "创建者完成编辑后，请点击上方“提交三专家共识”；内容修改后必须重新提交和确认。"}
      </p>
      {s.reviews.length > 0 && <div className="row tight">
        {s.reviews.map((r) => <span key={r.id} className="chip" title={r.comment}>
          {r.display_name}（{r.username}）：{labels[r.decision]} · {r.comment}
        </span>)}
      </div>}
      {canReview && <div className="row tight">
        <label>复核意见<input value={comment} onChange={(e) => setComment(e.target.value)} placeholder="同意可留空；退回必须说明原因" /></label>
        {own?.decision !== "approve" && <button className="btn small primary" onClick={() => onAction("approve", comment)}>同意本轮共识</button>}
        {own?.decision === "approve" && <button className="btn small" onClick={() => onAction("withdraw", comment)}>撤回我的确认</button>}
        <button className="btn small danger" disabled={!comment.trim()} onClick={() => onAction("return", comment)}>退回修改</button>
      </div>}
      {history && <div className="consensus-history"><table className="table compact">
        <thead><tr><th>轮次 / 内容版本</th><th>专家</th><th>意见</th><th>说明</th><th>时间</th></tr></thead>
        <tbody>{history.map((r, i) => <tr key={i}><td>{r.round} / {r.revision}</td><td>{r.reviewer_id}</td>
          <td>{labels[r.decision]}</td><td>{r.comment}</td><td>{new Date(r.created_at * 1000).toLocaleString()}</td></tr>)}</tbody>
      </table></div>}
      {error && <p className="error-text">{error}</p>}
    </div>
  );
}
