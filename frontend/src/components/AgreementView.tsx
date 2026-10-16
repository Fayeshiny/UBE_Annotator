import { useEffect, useState } from "react";
import { useApp } from "../App";
import { api } from "../lib/api";
import { fmtDur, fmtNum, fmtPct } from "../lib/time";

/* eslint-disable @typescript-eslint/no-explicit-any */
type Summary = any;

function k(x: number | null | undefined, target = 0.7) {
  if (x === null || x === undefined) return <span className="muted">—</span>;
  const cls = x >= target ? "good" : x >= 0.6 ? "warn" : "bad";
  return <span className={`kappa ${cls}`}>{x.toFixed(3)}</span>;
}

export function SummaryTable({ s }: { s: Summary }) {
  const { protocol } = useApp();
  if (!s) return <p className="muted">暂无配对数据。</p>;
  return (
    <div className="agreement">
      <table className="table compact">
        <tbody>
          <tr>
            <th>空间情境 κ</th>
            <td>{k(s.context.kappa)}</td>
            <th>边界误差</th>
            <td>
              {fmtDur(s.context.boundary_mean_abs_err_ms)}（未匹配 {s.context.boundary_unmatched}）
            </td>
          </tr>
          <tr>
            <th>情境时间 IoU</th>
            <td colSpan={3}>
              {Object.entries(s.context.temporal_iou as Record<string, number>).map(([c, v]) => (
                <span key={c} className="chip">
                  {c} {fmtNum(v)}
                </span>
              ))}
            </td>
          </tr>
          <tr>
            <th>可评价状态 κ</th>
            <td>{k(s.evaluable.kappa)}</td>
            <th>SCS 加权 κ（线性/二次）</th>
            <td>
              {k(s.scs.weighted_kappa_linear)} / {k(s.scs.weighted_kappa_quadratic)}{" "}
              <span className="muted small">n={s.scs.n_pairs}，U 比例 {fmtPct(s.scs.u_rate_a)} / {fmtPct(s.scs.u_rate_b)}</span>
            </td>
          </tr>
          <tr>
            <th>空间锚点 κ</th>
            <td colSpan={3}>
              {protocol.anchors.map((a) => (
                <span key={a.field} className="chip">
                  {a.label} {k(s.anchors[a.field])}
                </span>
              ))}
            </td>
          </tr>
          <tr>
            <th>定向丢失事件</th>
            <td colSpan={3}>
              A {s.events.n_a} 次 / B {s.events.n_b} 次，匹配 {s.events.matched}，F1 {fmtNum(s.events.f1)}，时间 IoU{" "}
              {fmtNum(s.events.temporal_iou)}；起点误差 {fmtDur(s.events.mean_onset_err_ms)}，终点误差 {fmtDur(s.events.mean_offset_err_ms)}，恢复点误差{" "}
              {fmtDur(s.events.mean_recovery_err_ms)}
            </td>
          </tr>
          {Object.keys(s.milestones).length > 0 && (
            <tr>
              <th>里程碑（平均绝对差）</th>
              <td colSpan={3}>
                {Object.entries(s.milestones as Record<string, any>).map(([m, v]) => (
                  <span key={m} className="chip" title={`可评价一致率 ${fmtPct(v.assessable_agreement)}`}>
                    {m} {fmtDur(v.mean_abs_diff_ms)}
                  </span>
                ))}
              </td>
            </tr>
          )}
        </tbody>
      </table>
      <p className="muted small">目标：κ ≥ 0.70（绿色）；0.60–0.69 先修订规则并增加 5–10 例复测（黄色）；&lt;0.60 暂停扩大标注（红色）。</p>
    </div>
  );
}

export default function AgreementView({ url, perCase }: { url: string; perCase?: boolean }) {
  const [data, setData] = useState<any>(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    api.get(url).then(setData).catch((e) => setErr(e.message));
  }, [url]);
  if (err) return <div className="error">{err}</div>;
  if (!data) return <p className="muted">计算中…</p>;
  if (perCase) {
    return (
      <div>
        {(data as any[]).map((p, i) => (
          <div key={i}>
            <h4>
              {p.a} vs {p.b}
            </h4>
            <SummaryTable s={p.summary} />
          </div>
        ))}
      </div>
    );
  }
  return (
    <div>
      <h4>全部配对合并</h4>
      <SummaryTable s={data.pooled} />
      {Object.entries(data.pairs as Record<string, Summary>).map(([pair, s]) => (
        <details key={pair}>
          <summary>
            {pair}（{s.n_cases} 例）
          </summary>
          <SummaryTable s={s} />
        </details>
      ))}
    </div>
  );
}
