import { useEffect, useMemo, useState } from "react";
import {
  ANCHOR_FIELDS,
  defaultSample,
  normalizeSampleAnchors,
  copyApplicableAnchors,
  labelAt,
  mergeEvents,
  nextEventId,
  removeBoundary,
  sampleComplete,
  sampleTimes,
  segmentEnd,
  setSegmentLabel,
  sortEvents,
  startSegment,
} from "../lib/ann";
import { api } from "../lib/api";
import { CONTEXT_COLORS, EVALUABLE_COLORS, SCS_COLORS, VIS_COLORS } from "../lib/colors";
import { fmtDur, fmtNum, fmtPct, fmtTime } from "../lib/time";
import type { Annotation, AnchorField, Issue, Metrics, OLEvent, Protocol, Sample } from "../lib/types";

export interface WS {
  ann: Annotation;
  update: (fn: (a: Annotation) => Annotation) => void;
  editable: boolean;
  currentMs: number;
  duration: number;
  segmentStart?: number;
  segmentEnd?: number;
  seek: (ms: number) => void;
  playRange: (from: number, to: number) => void;
  protocol: Protocol;
  gold: boolean;
  selectedSample: number | null;
  setSelectedSample: (t: number | null) => void;
  issues: Issue[];
  metrics: Metrics | null;
  assignmentId: number;
  activeAnchor: number | null;
  setActiveAnchor: (i: number | null) => void;
}

// ------------------------------------------------------------------ 采样点（轨道 2/3）

export function currentSampleTime(ws: WS): number {
  const iv = ws.protocol.settings.sampling_interval_ms;
  if (ws.selectedSample != null) return ws.selectedSample;
  return Math.max(Math.ceil((ws.segmentStart ?? 0) / iv) * iv, Math.min(Math.round(ws.currentMs / iv) * iv, Math.floor(((ws.segmentEnd ?? ws.duration) - 1) / iv) * iv));
}

export function setSampleValue(ws: WS, t: number, field: keyof Sample, value: string | undefined) {
  ws.update((a) => {
    const key = String(t);
    const ctx = labelAt(a.context, t);
    if (field !== "note" && labelAt(a.evaluable, t) !== "E1") return a;
    if (ANCHOR_FIELDS.includes(field as AnchorField)) {
      const applic = ctx ? ws.protocol.settings.anchor_applicability[ctx] : undefined;
      if (!applic?.includes(field as AnchorField) || (value !== undefined && !["V", "N", "U"].includes(value))) return a;
    }
    const cur = a.samples[key] ?? defaultSample(ctx, ws.protocol.settings);
    const next = { ...cur, [field]: value };
    if (!value) delete next[field];
    return { ...a, samples: { ...a.samples, [key]: next } };
  });
}

export function copyPrevAnchors(ws: WS, t: number) {
  if (labelAt(ws.ann.evaluable, t) !== "E1") return;
  const iv = ws.protocol.settings.sampling_interval_ms;
  for (let s = t - iv; s >= 0; s -= iv) {
    const prev = ws.ann.samples[String(s)];
    if (prev && ANCHOR_FIELDS.some((f) => prev[f])) {
      ws.update((a) => {
        const cur = a.samples[String(t)] ?? {};
        const next = copyApplicableAnchors(cur, prev, labelAt(a.context, t), ws.protocol.settings);
        return { ...a, samples: { ...a.samples, [String(t)]: next } };
      });
      return;
    }
  }
}

export function nextIncompleteSample(ws: WS, from: number): number | null {
  const iv = ws.protocol.settings.sampling_interval_ms;
  const all = sampleTimes(ws.duration, iv).filter(t => t >= (ws.segmentStart ?? 0) && t < (ws.segmentEnd ?? ws.duration));
  // 先向后找，找不到再从头找
  const order = [...all.filter((t) => t > from), ...all.filter((t) => t <= from)];
  for (const t of order) {
    if (labelAt(ws.ann.evaluable, t) === "E0") continue;
    if (!sampleComplete(ws.ann.samples[String(t)])) return t;
  }
  return null;
}

export function SamplePanel({ ws }: { ws: WS }) {
  const { protocol } = ws;
  const iv = protocol.settings.sampling_interval_ms;
  const win = protocol.settings.sample_review_window_ms;
  const t = currentSampleTime(ws);
  const ctx = labelAt(ws.ann.context, t);
  const s = normalizeSampleAnchors(ws.ann.samples[String(t)] ?? {}, ctx, protocol.settings);
  const ev = labelAt(ws.ann.evaluable, t);
  const applic = ctx ? protocol.settings.anchor_applicability[ctx] ?? [] : [];
  const all = sampleTimes(ws.duration, iv).filter(t => t >= (ws.segmentStart ?? 0) && t < (ws.segmentEnd ?? ws.duration));
  const evalSamples = all.filter((x) => labelAt(ws.ann.evaluable, x) === "E1");
  const done = evalSamples.filter((x) => sampleComplete(ws.ann.samples[String(x)])).length;
  const go = (x: number | null) => {
    if (x == null) return;
    ws.setSelectedSample(x);
    ws.seek(x);
  };
  const dis = !ws.editable || ev !== "E1";

  return (
    <div className="panel">
      <div className="row">
        <b>采样点 {fmtTime(t)}</b>
        <span className="chip" style={{ background: ctx ? CONTEXT_COLORS[ctx] : undefined, color: ctx ? "#fff" : undefined }}>
          {ctx ?? "情境未标"}
        </span>
        <span className="chip" style={{ background: ev ? EVALUABLE_COLORS[ev] : undefined, color: ev ? "#fff" : undefined }}>
          {ev ?? "可评价未标"}
        </span>
        <div className="spacer" />
        <span className="muted small">
          已完成 {done}/{evalSamples.length}（可评价采样点，间隔 {iv / 1000}s）
        </span>
      </div>
      <div className="row tight">
        <button className="btn small" onClick={() => go(Math.max(0, t - iv))}>
          ← 上一个 (J)
        </button>
        <button className="btn small" onClick={() => ws.playRange(Math.max(0, t - win), Math.min(ws.duration, t + win))}>
          回看 ±{win / 1000}s (K)
        </button>
        <button className="btn small" onClick={() => go(Math.min(all[all.length - 1] ?? 0, t + iv))}>
          下一个 (L) →
        </button>
        <button className="btn small primary" onClick={() => go(nextIncompleteSample(ws, t))}>
          下一个未完成 (Enter)
        </button>
      </div>
      {ev === "E0" && <div className="notice">该时刻为不可评价（E0），不进入 SCS 与 LPS 分母，无需填写。</div>}
      {ev == null && <div className="notice">请先在“分段”中完成可评价状态（轨道 0）。</div>}
      {!ctx && <div className="notice">请先标注空间情境，再填写空间锚点。</div>}
      <h4>空间稳定评分 SCS</h4>
      <div className="seg-buttons">
        {protocol.scs.map((c) => (
          <button
            key={c.code}
            disabled={dis}
            className={s?.scs === c.code ? "seg active" : "seg"}
            style={s?.scs === c.code ? { background: SCS_COLORS[c.code], borderColor: SCS_COLORS[c.code] } : undefined}
            onClick={() => setSampleValue(ws, t, "scs", s?.scs === c.code ? undefined : c.code)}
            title={c.desc as string}
          >
            <b>{c.code}</b> {c.label}
            <kbd>{c.code === "U" ? "U" : c.code}</kbd>
          </button>
        ))}
      </div>
      <h4>
        空间锚点可见性
        <button className="btn tiny" disabled={dis} onClick={() => copyPrevAnchors(ws, t)}>
          复制上一采样点 (C)
        </button>
      </h4>
      <table className="anchor-grid">
        <tbody>
          {protocol.anchors.map((a, i) => (
            <tr
              key={a.field}
              className={`${applic.includes(a.field) ? "" : "not-applicable"} ${ws.activeAnchor === i ? "current" : ""}`}
              onClick={() => ws.setActiveAnchor(i)}
            >
              <td title={a.en}>
                <kbd>{["Q", "W", "E", "R", "T"][i]}</kbd> {a.code} {a.label}
              </td>
              {protocol.visibility.map((v) => (
                <td key={v.code}>
                  <button
                    disabled={dis || !ctx || !applic.includes(a.field) || v.code === "NA"}
                    className={s?.[a.field] === v.code ? "vis active" : "vis"}
                    style={s?.[a.field] === v.code ? { background: VIS_COLORS[v.code], borderColor: VIS_COLORS[v.code] } : undefined}
                    title={!applic.includes(a.field) && ctx ? "当前情境不适用，固定为 NA" : v.code === "NA" ? "适用锚点请选择 V、N 或 U" : v.label}
                    onClick={() => setSampleValue(ws, t, a.field, s?.[a.field] === v.code ? undefined : v.code)}
                  >
                    {v.code}
                  </button>
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      <p className="muted small">
        按 Q–T 选中锚点行后用 V / N / U 填写；灰色行在当前情境不适用，固定为 NA，不能修改。NA 与 N 必须严格区分。SCS 不能由锚点数直接推出，应依据连续视频中的镜头、器械行为判断。
      </p>
      <label>
        备注
        <input disabled={!ws.editable} value={s?.note ?? ""} onChange={(e) => setSampleValue(ws, t, "note", e.target.value)} />
      </label>
    </div>
  );
}

// ------------------------------------------------------------------ 分段（轨道 0/1）

export function SegmentsPanel({ ws }: { ws: WS }) {
  const { protocol } = ws;
  const t = ws.currentMs;
  const section = (key: "evaluable" | "context", codes: { code: string; label: string }[], colors: Record<string, string>, keys: string[]) => {
    const parts = ws.ann[key];
    return (
      <div>
        <div className="row tight">
          <span className="muted small">从当前时刻 {fmtTime(t)} 开始：</span>
          {codes.map((c, i) => (
            <button
              key={c.code}
              className="btn small"
              disabled={!ws.editable}
              style={{ borderColor: colors[c.code], color: colors[c.code] }}
              title={c.label}
              onClick={() => ws.update((a) => ({ ...a, [key]: startSegment(a[key], Math.round(t), c.code) }))}
            >
              {c.code}
              <kbd>{keys[i]}</kbd>
            </button>
          ))}
        </div>
        <table className="table compact">
          <tbody>
            {parts.map((p, i) => segmentEnd(parts, i, ws.duration) <= (ws.segmentStart ?? 0) || p.start_ms >= (ws.segmentEnd ?? ws.duration) ? null : (
              <tr key={`${p.start_ms}-${i}`} className={t >= p.start_ms && t < segmentEnd(parts, i, ws.duration) ? "current" : ""}>
                <td>
                  <a onClick={() => ws.seek(p.start_ms)}>{fmtTime(p.start_ms)}</a> – <a onClick={() => ws.seek(Math.max(0, segmentEnd(parts, i, ws.duration) - 1))}>{fmtTime(segmentEnd(parts, i, ws.duration))}</a>
                </td>
                <td>
                  <select
                    disabled={!ws.editable}
                    value={p.label ?? ""}
                    onChange={(e) => ws.update((a) => ({ ...a, [key]: setSegmentLabel(a[key], i, e.target.value || null) }))}
                  >
                    <option value="">未标注</option>
                    {codes.map((c) => (
                      <option key={c.code} value={c.code}>
                        {c.code} {c.label}
                      </option>
                    ))}
                  </select>
                </td>
                <td className="actions">
                  {i > 0 && p.start_ms >= (ws.segmentStart ?? 0) && ws.editable && (
                    <>
                      <button
                        className="btn tiny"
                        title="把该段起点移到当前时刻"
                        onClick={() =>
                          ws.update((a) => {
                            const ps = a[key];
                            const lo = ps[i - 1].start_ms;
                            const hi = i + 1 < ps.length ? ps[i + 1].start_ms : ws.duration;
                            if (t <= lo || t >= hi) return a;
                            return { ...a, [key]: ps.map((x, j) => (j === i ? { ...x, start_ms: Math.round(t) } : x)) };
                          })
                        }
                      >
                        起点=当前
                      </button>
                      <button className="btn tiny" title="删除该边界（与前一段合并）" onClick={() => ws.update((a) => ({ ...a, [key]: removeBoundary(a[key], i) }))}>
                        并入前段
                      </button>
                    </>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  };
  return (
    <div className="panel">
      <h4>轨道 0 · 视频可评价状态</h4>
      {section("evaluable", protocol.evaluable, EVALUABLE_COLORS, ["Alt+E", "Alt+X"])}
      <h4>轨道 1 · 空间情境（连续分段，边界不清可用 CU）</h4>
      {section("context", protocol.contexts, CONTEXT_COLORS, ["Alt+1", "Alt+2", "Alt+3", "Alt+4", "Alt+5", "Alt+6", "Alt+0"])}
      <details className="small">
        <summary>情境定义</summary>
        {protocol.contexts.map((c) => (
          <p key={c.code}>
            <b style={{ color: CONTEXT_COLORS[c.code] }}>
              {c.code} {c.label}
            </b>
            ：{c.desc}
            {c.milestones ? `（对应 ${c.milestones}）` : ""}
          </p>
        ))}
      </details>
    </div>
  );
}

// ------------------------------------------------------------------ 定向丢失与恢复（轨道 4/5）

export function markEvent(ws: WS, what: "onset" | "offset" | "recovery") {
  const t = Math.round(ws.currentMs);
  ws.update((a) => {
    const evs = sortEvents(a.events);
    if (what === "onset") {
      if (evs.some((e) => e.offset_ms == null)) return a;
      const e: OLEvent = { id: nextEventId(evs), onset_ms: t, offset_ms: null, recovery_ms: null, disturbance: null, confidence: null, note: "" };
      return { ...a, events: sortEvents([...evs, e]) };
    }
    if (what === "offset") {
      const open = evs.find((e) => e.offset_ms == null && e.onset_ms < t);
      if (!open) return a;
      return { ...a, events: evs.map((e) => (e === open ? { ...e, offset_ms: t } : e)) };
    }
    const target = [...evs].reverse().find((e) => e.offset_ms != null && e.offset_ms <= t && e.recovery_ms == null);
    if (!target) return a;
    return { ...a, events: evs.map((e) => (e === target ? { ...e, recovery_ms: t } : e)) };
  });
}

export function EventsPanel({ ws }: { ws: WS }) {
  const { protocol } = ws;
  const evs = sortEvents(ws.ann.events).filter(e => e.onset_ms < (ws.segmentEnd ?? ws.duration) && (e.recovery_ms ?? Infinity) >= (ws.segmentStart ?? 0));
  const open = evs.find((e) => e.offset_ms == null);
  const merges = ws.issues.filter((i) => i.fix?.type === "merge_events");
  const minDur = protocol.settings.ol_min_duration_ms;
  const setE = (id: string, patch: Partial<OLEvent>) => ws.update((a) => ({ ...a, events: a.events.map((e) => (e.id === id ? { ...e, ...patch } : e)) }));

  return (
    <div className="panel">
      <div className="row tight">
        <button className="btn small" disabled={!ws.editable || !!open} onClick={() => markEvent(ws, "onset")}>
          标记开始 <kbd>[</kbd>
        </button>
        <button className="btn small" disabled={!ws.editable || !open} onClick={() => markEvent(ws, "offset")}>
          标记结束 <kbd>]</kbd>
        </button>
        <button className="btn small" disabled={!ws.editable} onClick={() => markEvent(ws, "recovery")}>
          标记恢复点 <kbd>\</kbd>
        </button>
      </div>
      <p className="muted small">
        定向丢失：持续失去已有空间控制并出现明显重复重定位或非目标导向行为，预设 ≥{minDur / 1000}s。单纯血液遮挡、模糊、正常重新取景、有目的的系统性探查不单独构成事件。恢复点 = 事件结束后首次达到 SCS=2 并持续 ≥
        {protocol.settings.recovery_min_stable_ms / 1000}s 的起始时间。
        两次事件之间的稳定期不足该时长时，必须合并后才能提交或锁定。
      </p>
      {merges.map((m, i) => (
        <div key={i} className="error row">
          <span>{m.message}</span>
          {ws.editable && (
            <button className="btn tiny" onClick={() => ws.update((a) => ({ ...a, events: mergeEvents(a.events, m.fix!.ids) }))}>
              合并
            </button>
          )}
        </div>
      ))}
      {evs.length === 0 && <p className="muted">尚无定向丢失事件。</p>}
      {evs.map((e) => {
        const dur = e.offset_ms != null ? e.offset_ms - e.onset_ms : null;
        const srt = e.recovery_ms != null && e.offset_ms != null ? e.recovery_ms - e.offset_ms : null;
        return (
          <div key={e.id} className="event-card">
            <div className="row tight">
              <b>{e.id}</b>
              <span className="chip">{labelAt(ws.ann.context, e.onset_ms) ?? "—"}</span>
              <span className={dur != null && dur < minDur ? "error-text small" : "muted small"}>持续 {fmtDur(dur)}</span>
              <span className="muted small">SRT {fmtDur(srt)}</span>
              <div className="spacer" />
              {ws.editable && (
                <button className="btn tiny danger" onClick={() => confirm(`删除 ${e.id}？`) && ws.update((a) => ({ ...a, events: a.events.filter((x) => x.id !== e.id) }))}>
                  删除
                </button>
              )}
            </div>
            <div className="ev-times">
              {(["onset_ms", "offset_ms", "recovery_ms"] as const).map((f) => (
                <div key={f}>
                  <span className="muted small">{{ onset_ms: "开始", offset_ms: "结束", recovery_ms: "恢复" }[f]}</span>
                  <a onClick={() => e[f] != null && ws.seek(e[f]!)}>{fmtTime(e[f])}</a>
                  {ws.editable && (
                    <button className="btn tiny" onClick={() => setE(e.id, { [f]: Math.round(ws.currentMs) })}>
                      =当前
                    </button>
                  )}
                </div>
              ))}
            </div>
            <div className="row tight">
              <select disabled={!ws.editable} value={e.disturbance ?? ""} onChange={(x) => setE(e.id, { disturbance: x.target.value || null })}>
                <option value="">主要干扰因素…</option>
                {protocol.disturbances.map((d) => (
                  <option key={d.code} value={d.code}>
                    {d.label}
                  </option>
                ))}
              </select>
              <select disabled={!ws.editable} value={e.confidence ?? ""} onChange={(x) => setE(e.id, { confidence: x.target.value || null })}>
                <option value="">置信度…</option>
                {protocol.confidence.map((d) => (
                  <option key={d.code} value={d.code}>
                    {d.label}
                  </option>
                ))}
              </select>
              <input disabled={!ws.editable} placeholder="备注" value={e.note ?? ""} onChange={(x) => setE(e.id, { note: x.target.value })} />
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ------------------------------------------------------------------ 里程碑（轨道 6）

export function MilestonePanel({ ws, selected, setSelected }: { ws: WS; selected: string; setSelected: (m: string) => void }) {
  const { protocol } = ws;
  const setM = (code: string, patch: object) =>
    ws.update((a) => ({
      ...a,
      milestones: { ...a.milestones, [code]: { ...(a.milestones[code] ?? { time_ms: null, assessable: 1, confidence: null }), ...patch } },
    }));
  return (
    <div className="panel">
      <p className="muted small">
        里程碑 = “首次可靠达到某空间状态或关键操作节点”的时间点。选中一行后按 <kbd>M</kbd> 设为当前时刻并跳到下一项。无法观察请勾选“无法判断”，不能写成“未发生”。
      </p>
      <table className="table compact milestones">
        <tbody>
          {protocol.milestones.map((m) => {
            const v = ws.ann.milestones[m.code];
            const na = v && !v.assessable;
            return (
              <tr key={m.code} className={selected === m.code ? "current" : ""} onClick={() => setSelected(m.code)}>
                <td>
                  <span className="dot" style={{ background: CONTEXT_COLORS[m.context] }} /> <b>{m.code}</b>
                </td>
                <td title={m.en}>{m.label}</td>
                <td>{na ? <span className="muted">NA</span> : <a onClick={() => v?.time_ms != null && ws.seek(v.time_ms)}>{fmtTime(v?.time_ms)}</a>}</td>
                <td className="actions">
                  {ws.editable && (
                    <>
                      <button className="btn tiny" disabled={!!na} onClick={() => setM(m.code, { time_ms: Math.round(ws.currentMs), assessable: 1 })}>
                        =当前
                      </button>
                      <label className="inline small">
                        <input type="checkbox" checked={!!na} onChange={(e) => setM(m.code, { assessable: e.target.checked ? 0 : 1, time_ms: e.target.checked ? null : v?.time_ms ?? null })} />
                        无法判断
                      </label>
                    </>
                  )}
                  <select disabled={!ws.editable || !!na} value={v?.confidence ?? ""} onChange={(e) => setM(m.code, { confidence: e.target.value || null })}>
                    <option value="">置信度</option>
                    {protocol.confidence.map((c) => (
                      <option key={c.code} value={c.code}>
                        {c.label}
                      </option>
                    ))}
                  </select>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ------------------------------------------------------------------ 检查与指标

export function ChecksPanel({ ws }: { ws: WS }) {
  const { metrics, issues, protocol } = ws;
  const [level, setLevel] = useState<"all" | "error" | "warning" | "info">("all");
  const shown = issues.filter((i) => level === "all" || i.level === level);
  const count = (l: string) => issues.filter((i) => i.level === l).length;
  return (
    <div className="panel">
      <h4>自动指标（保存后更新）</h4>
      {metrics && (
        <table className="table compact">
          <tbody>
            <tr>
              <th>SCS 稳定比例</th>
              <td>{fmtPct(metrics.scs_stable_ratio)}</td>
              <th>可评价时长</th>
              <td>{fmtTime(metrics.evaluable_duration_sec * 1000, false)}</td>
            </tr>
            <tr>
              <th>定向丢失频率 OLF</th>
              <td>{fmtNum(metrics.olf_per_10min)} 次/10 分钟</td>
              <th>事件数</th>
              <td>{metrics.ol_events}</td>
            </tr>
            <tr>
              <th>SRT 均值 / 中位数</th>
              <td colSpan={3}>
                {fmtDur(metrics.srt_mean_ms)} / {fmtDur(metrics.srt_median_ms)}
              </td>
            </tr>
            <tr>
              <th>LPS（V ÷ (V+N)）</th>
              <td colSpan={3}>
                {protocol.anchors.map((a) => (
                  <span key={a.field} className="chip">
                    {a.label} {fmtPct(metrics.lps[a.field as AnchorField])}
                  </span>
                ))}
              </td>
            </tr>
          </tbody>
        </table>
      )}
      <div className="row tight">
        <h4>检查结果</h4>
        {(["all", "error", "warning", "info"] as const).map((l) => (
          <button key={l} className={level === l ? "btn tiny primary" : "btn tiny"} onClick={() => setLevel(l)}>
            {{ all: "全部", error: `错误 ${count("error")}`, warning: `警告 ${count("warning")}`, info: `提示 ${count("info")}` }[l]}
          </button>
        ))}
      </div>
      {shown.length === 0 && <p className="muted">没有问题。</p>}
      <ul className="issues">
        {shown.slice(0, 300).map((i, k) => (
          <li key={k} className={`issue ${i.level}`} onClick={() => i.time_ms != null && ws.seek(i.time_ms)}>
            <span className="lvl">{{ error: "错误", warning: "警告", info: "提示" }[i.level]}</span>
            {i.message}
          </li>
        ))}
      </ul>
    </div>
  );
}

// ------------------------------------------------------------------ 历史版本

export function HistoryPanel({ ws, version }: { ws: WS; version: number }) {
  const [rows, setRows] = useState<{ version: number; saved_at: number; username: string }[]>([]);
  useEffect(() => {
    api.get<typeof rows>(`/api/assignments/${ws.assignmentId}/history`).then(setRows);
  }, [ws.assignmentId, version]);
  const fmt = useMemo(() => new Intl.DateTimeFormat("zh-CN", { dateStyle: "short", timeStyle: "medium" }), []);
  return (
    <div className="panel">
      <p className="muted small">每次保存都会留存完整快照。恢复旧版本会作为一次新的修改保存，原有记录不会被删除。</p>
      <table className="table compact">
        <tbody>
          {rows.map((r) => (
            <tr key={r.version}>
              <td>v{r.version}</td>
              <td>{fmt.format(new Date(r.saved_at * 1000))}</td>
              <td>{r.username}</td>
              <td>
                {ws.editable && r.version !== version && (
                  <button
                    className="btn tiny"
                    onClick={async () => {
                      if (!confirm(`恢复到 v${r.version}？`)) return;
                      const d = await api.get<Annotation>(`/api/assignments/${ws.assignmentId}/history/${r.version}`);
                      ws.update(() => d);
                    }}
                  >
                    恢复
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
