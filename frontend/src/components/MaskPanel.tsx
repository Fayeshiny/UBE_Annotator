import { useState } from "react";
import { labelAt, partIndexAt, segmentEnd } from "../lib/ann";
import { api } from "../lib/api";
import { fmtTime } from "../lib/time";
import type { MaskObject, SamSession } from "../lib/types";
import type { WS } from "./panels";
import type { Tool } from "./VideoStage";

interface Props {
  ws: WS;
  canVerify: boolean;
  frame: number;
  objects: MaskObject[];
  reload: () => Promise<void>;
  activeObject: number | null;
  setActiveObject: (id: number | null) => void;
  tool: Tool;
  setTool: (t: Tool) => void;
  session: SamSession | null;
  setSession: (s: SamSession | null) => void;
  creatingSession: boolean;
  onStartSession: (startMs: number, endMs: number, stride: number) => Promise<void>;
  showMasks: boolean;
  setShowMasks: (b: boolean) => void;
  onError: (msg: string) => void;
  seekFrame: (f: number) => void;
}

const SOURCE_LABEL: Record<string, string> = {
  manual: "人工",
  sam_propagated: "模型传播（未复核）",
  sam_corrected: "模型辅助校正",
};

export default function MaskPanel(p: Props) {
  const { ws } = p;
  const colors = Object.fromEntries(ws.protocol.mask_classes.map((c) => [c.code, c.color]));
  const ctxIdx = partIndexAt(ws.ann.context, ws.currentMs);
  const [range, setRange] = useState<[number, number] | null>(null);
  const [stride, setStride] = useState(1);
  const segStart = range?.[0] ?? ws.ann.context[ctxIdx]?.start_ms ?? 0;
  const segEnd = range?.[1] ?? segmentEnd(ws.ann.context, ctxIdx, ws.duration);
  const active = p.objects.find((o) => o.id === p.activeObject) ?? null;
  const s = p.session;
  const establishing = p.creatingSession || s?.status === "loading";
  const sessionBusy = establishing || s?.status === "propagating";
  const inSession = (o: MaskObject) => !!s && o.start_ms >= s.start_ms - 1 && o.end_ms <= s.end_ms + 1;

  async function run(fn: () => Promise<unknown>) {
    try {
      await fn();
    } catch (e) {
      p.onError((e as Error).message);
    }
  }

  async function addObject(cls: string) {
    await run(async () => {
      const o = await api.post<MaskObject>(`/api/assignments/${ws.assignmentId}/mask-objects`, {
        cls,
        start_ms: s ? s.start_ms : segStart,
        end_ms: s ? s.end_ms : segEnd,
      });
      await p.reload();
      p.setActiveObject(o.id);
      p.setTool(s ? "point" : "poly-add");
    });
  }

  async function propagate(direction: string) {
    if (!s) return;
    await run(async () => p.setSession(await api.post<SamSession>(`/api/sam/sessions/${s.id}/propagate`, { frame_idx: p.frame, direction })));
  }

  const maskFrames = active?.frames?.map((f) => f.frame_idx) ?? [];
  const prevMask = [...maskFrames].reverse().find((f) => f < p.frame);
  const nextMask = maskFrames.find((f) => f > p.frame);

  return (
    <div className="panel">
      <p className="muted small">
        仅对 5 类关键结构做专家引导的分割。按空间情境或稳定可视区间建立片段，不跨明显情境转换传播；出现掩膜漂移、结构离开视野、长时间遮挡或镜头大幅重定位时停止当前片段，结构重新出现后新建片段重新初始化。
        <b>未经人工复核的传播结果不能作为金标准。</b>
      </p>

      <h4>① SAM 分割片段（后端：{ws.protocol.sam_backend}）</h4>
      <div className="row tight">
        <span className="small">
          {fmtTime(segStart)} – {fmtTime(segEnd)}（{labelAt(ws.ann.context, segStart) ?? "情境未标"}）
        </span>
        <button className="btn tiny" disabled={sessionBusy} onClick={() => setRange([Math.round(ws.currentMs), segEnd])}>
          起点=当前
        </button>
        <button className="btn tiny" disabled={sessionBusy} onClick={() => setRange([segStart, Math.round(ws.currentMs)])}>
          终点=当前
        </button>
        <button className="btn tiny" disabled={sessionBusy} onClick={() => setRange(null)}>
          用当前情境段
        </button>
      </div>
      <div className="row tight">
        <label className="inline small">
          帧间隔
          <select disabled={sessionBusy} value={stride} onChange={(e) => setStride(Number(e.target.value))}>
            {[1, 2, 3, 5, 10, 15].map((n) => (
              <option key={n} value={n}>
                每 {n} 帧
              </option>
            ))}
          </select>
        </label>
        <button
          className="btn small primary"
          disabled={!ws.editable || sessionBusy || segEnd <= segStart}
          aria-busy={establishing}
          onClick={() => p.onStartSession(segStart, segEnd, stride)}
        >
          {establishing && <span className="spinner" aria-hidden="true" />}
          {establishing ? "正在建立片段…" : s ? "重建片段" : "建立片段"}
        </button>
        {s && (
          <button
            className="btn small"
            disabled={sessionBusy}
            onClick={() =>
              run(async () => {
                await api.del(`/api/sam/sessions/${s.id}`);
                p.setSession(null);
              })
            }
          >
            结束片段
          </button>
        )}
      </div>
      {establishing && (
        <div className="notice" role="status" aria-live="polite">
          {p.creatingSession ? "正在建立片段，请等待服务器响应…" : "正在抽帧或加载模型，请稍候…"}
          建立完成前不能重复创建。
        </div>
      )}
      {s && (
        <div className="notice">
          片段 {fmtTime(s.start_ms)}–{fmtTime(s.end_ms)} · {s.frames.length} 帧 ·{" "}
          {{ loading: "抽帧/加载模型中…", ready: "就绪", propagating: `传播中 ${(s.progress * 100).toFixed(0)}%`, error: "出错", closed: "已关闭" }[s.status]}
          {s.error && <div className="error-text">{s.error}</div>}
          {s.status === "propagating" && (
            <button className="btn tiny" onClick={() => run(() => api.post(`/api/sam/sessions/${s.id}/cancel`))}>
              停止
            </button>
          )}
        </div>
      )}

      <h4>② 结构目标</h4>
      <p className="muted small">掩膜确认及撤销确认仅限管理员和复核专家操作。</p>
      <div className="row tight">
        {ws.protocol.mask_classes.map((c) => (
          <button key={c.code} className="btn small" disabled={!ws.editable} style={{ borderColor: c.color, color: c.color }} onClick={() => addObject(c.code)}>
            + {c.label}
          </button>
        ))}
      </div>
      <table className="table compact">
        <tbody>
          {p.objects.map((o) => (
            <tr key={o.id} className={o.id === p.activeObject ? "current" : ""} onClick={() => p.setActiveObject(o.id)}>
              <td>
                <span className="dot" style={{ background: colors[o.class] }} /> <b>{o.object_id}</b> {ws.protocol.mask_classes.find((c) => c.code === o.class)?.label}
              </td>
              <td className="small">
                <a onClick={() => ws.seek(o.start_ms)}>{fmtTime(o.start_ms, false)}</a>–{fmtTime(o.end_ms, false)}
                <div className="muted">
                  {o.n_frames} 帧 · {SOURCE_LABEL[o.annotation_source] ?? o.annotation_source}
                  {s && !inSession(o) && " · 不在当前片段"}
                </div>
              </td>
              <td className="actions">
                {o.verified ? <span className="badge st-approved">已确认</span> : <span className="badge st-returned">未确认</span>}
                {p.canVerify && (
                    <button
                      className="btn tiny"
                      onClick={(e) => {
                        e.stopPropagation();
                        run(async () => {
                          await api.post(`/api/mask-objects/${o.id}/verify`, { verified: !o.verified });
                          await p.reload();
                        });
                      }}
                    >
                      {o.verified ? "撤销确认" : "专家确认"}
                    </button>
                )}
                {ws.editable && (
                    <button
                      className="btn tiny danger"
                      onClick={(e) => {
                        e.stopPropagation();
                        if (!confirm(`删除 ${o.object_id} 及其全部掩膜？`)) return;
                        run(async () => {
                          await api.del(`/api/mask-objects/${o.id}`);
                          if (p.activeObject === o.id) p.setActiveObject(null);
                          await p.reload();
                        });
                      }}
                    >
                      删除
                    </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <h4>③ 当前帧工具（帧 {p.frame}）</h4>
      {!active && <p className="muted small">先在上面选择或新建一个结构目标。</p>}
      {active && (
        <>
          <div className="row tight">
            {(
              [
                ["none", "浏览"],
                ["point", "点提示 (P)"],
                ["box", "框提示 (B)"],
                ["poly-add", "多边形补画"],
                ["poly-sub", "多边形擦除"],
              ] as [Tool, string][]
            ).map(([t, l]) => (
              <button
                key={t}
                disabled={!ws.editable || ((t === "point" || t === "box") && (!s || s.status !== "ready" || !inSession(active)))}
                className={p.tool === t ? "btn small primary" : "btn small"}
                onClick={() => p.setTool(t)}
              >
                {l}
              </button>
            ))}
            <label className="inline small">
              <input type="checkbox" checked={p.showMasks} onChange={(e) => p.setShowMasks(e.target.checked)} /> 显示掩膜 (H)
            </label>
          </div>
          <p className="muted small">
            点提示：左键=前景，右键或 Shift+左键=背景，连续点击逐步修正。多边形：逐点单击，点回起点、双击或 Enter 闭合，Backspace 撤销一点，Esc 取消。
          </p>
          <div className="row tight">
            <button className="btn small" disabled={!s || s.status !== "ready"} onClick={() => propagate("forward")}>
              向后传播 →
            </button>
            <button className="btn small" disabled={!s || s.status !== "ready"} onClick={() => propagate("backward")}>
              ← 向前传播
            </button>
            <button className="btn small" disabled={!s || s.status !== "ready"} onClick={() => propagate("both")}>
              双向传播
            </button>
          </div>
          <div className="row tight">
            <button className="btn tiny" disabled={prevMask == null} onClick={() => prevMask != null && p.seekFrame(prevMask)}>
              ← 上一掩膜帧
            </button>
            <button className="btn tiny" disabled={nextMask == null} onClick={() => nextMask != null && p.seekFrame(nextMask)}>
              下一掩膜帧 →
            </button>
            {ws.editable && maskFrames.includes(p.frame) && (
              <button
                className="btn tiny danger"
                onClick={() =>
                  run(async () => {
                    await api.del(`/api/mask-objects/${active.id}/frames/${p.frame}`);
                    await p.reload();
                  })
                }
              >
                删除本帧掩膜
              </button>
            )}
            <span className="muted small">本目标已有 {maskFrames.length} 帧掩膜</span>
          </div>
        </>
      )}
    </div>
  );
}
