import { useCallback, useEffect, useRef, useState } from "react";
import { ANCHOR_FIELDS, moveBoundary, sampleTimes } from "../lib/ann";
import { CONTEXT_COLORS, EVALUABLE_COLORS, SCS_COLORS, UNLABELED, VIS_COLORS } from "../lib/colors";
import { fmtTime } from "../lib/time";
import type { Annotation, MaskObject, Protocol } from "../lib/types";

interface Props {
  ann: Annotation;
  duration: number;
  currentMs: number;
  protocol: Protocol;
  gold: boolean;
  editable: boolean;
  maskObjects: MaskObject[];
  selectedSample: number | null;
  activeObject: number | null;
  onSeek: (ms: number) => void;
  onSelectSample: (ms: number) => void;
  onChange: (ann: Annotation) => void;
  snap: (ms: number) => number;
}

const LABEL_W = 118;
const RULER_H = 22;
const ROW_H = 24;

type Row = { key: string; label: string; h?: number };

type Drag =
  | { kind: "boundary"; track: "evaluable" | "context"; idx: number }
  | { kind: "event"; id: string; field: "onset_ms" | "offset_ms" | "recovery_ms" }
  | { kind: "scrub" }
  | { kind: "pan"; x0: number; v0: number; v1: number };

export default function Timeline(p: Props) {
  const wrap = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const [width, setWidth] = useState(800);
  const [view, setView] = useState<[number, number]>([0, Math.max(p.duration, 1000)]);
  const [drag, setDrag] = useState<Drag | null>(null);
  const [preview, setPreview] = useState<Annotation | null>(null);
  const [hover, setHover] = useState<string>("");
  const ann = preview ?? p.ann;

  const rows: Row[] = [
    { key: "evaluable", label: "0 可评价" },
    { key: "context", label: "1 空间情境" },
    { key: "anchors", label: "2 空间锚点", h: 34 },
    { key: "scs", label: "3 SCS" },
    { key: "events", label: "4 定向丢失" },
    { key: "recovery", label: "5 空间恢复" },
    ...(p.gold
      ? [
          { key: "milestones", label: "6 里程碑", h: 28 },
          { key: "masks", label: "7 结构分割", h: Math.max(ROW_H, 14 * Math.max(1, p.maskObjects.length) + 6) },
        ]
      : []),
  ];
  const rowY: Record<string, [number, number]> = {};
  let y = RULER_H;
  for (const r of rows) {
    const h = r.h ?? ROW_H;
    rowY[r.key] = [y, h];
    y += h;
  }
  const height = y + 2;

  useEffect(() => {
    setView([0, Math.max(p.duration, 1000)]);
  }, [p.duration]);

  useEffect(() => {
    const el = wrap.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setWidth(el.clientWidth));
    ro.observe(el);
    setWidth(el.clientWidth);
    return () => ro.disconnect();
  }, []);

  // 播放头离开视窗时自动跟随
  useEffect(() => {
    const [v0, v1] = view;
    if (p.currentMs < v0 || p.currentMs > v1) {
      const span = v1 - v0;
      const n0 = Math.max(0, Math.min(p.duration - span, p.currentMs - span * 0.1));
      setView([n0, n0 + span]);
    }
  }, [p.currentMs, view, p.duration]);

  const plotW = Math.max(10, width - LABEL_W);
  const xOf = useCallback((t: number) => LABEL_W + ((t - view[0]) / (view[1] - view[0])) * plotW, [view, plotW]);
  const tOf = useCallback((x: number) => view[0] + ((x - LABEL_W) / plotW) * (view[1] - view[0]), [view, plotW]);

  // ---------------------------------------------------------------- 绘制
  useEffect(() => {
    const c = canvas.current;
    if (!c) return;
    const dpr = window.devicePixelRatio || 1;
    c.width = width * dpr;
    c.height = height * dpr;
    const g = c.getContext("2d")!;
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, width, height);
    g.font = "11px -apple-system, 'PingFang SC', sans-serif";
    g.textBaseline = "middle";

    // 行背景与标签
    rows.forEach((r, i) => {
      const [ry, rh] = rowY[r.key];
      g.fillStyle = i % 2 ? "#f8fafc" : "#ffffff";
      g.fillRect(0, ry, width, rh);
      g.fillStyle = "#334155";
      g.fillText(r.label, 6, ry + rh / 2);
    });
    g.strokeStyle = "#e2e8f0";
    g.beginPath();
    g.moveTo(LABEL_W - 0.5, 0);
    g.lineTo(LABEL_W - 0.5, height);
    g.stroke();

    g.save();
    g.beginPath();
    g.rect(LABEL_W, 0, plotW, height);
    g.clip();

    // 刻度
    const span = view[1] - view[0];
    const steps = [100, 200, 500, 1000, 2000, 5000, 10000, 15000, 30000, 60000, 120000, 300000, 600000];
    const step = steps.find((s) => (s / span) * plotW > 70) ?? 600000;
    g.fillStyle = "#f1f5f9";
    g.fillRect(LABEL_W, 0, plotW, RULER_H);
    g.fillStyle = "#64748b";
    g.strokeStyle = "#cbd5e1";
    for (let t = Math.floor(view[0] / step) * step; t <= view[1]; t += step) {
      const x = xOf(t);
      g.beginPath();
      g.moveTo(x + 0.5, RULER_H - 6);
      g.lineTo(x + 0.5, RULER_H);
      g.stroke();
      g.fillText(fmtTime(t, step < 1000), x + 3, 9);
    }
    // 超出视频长度的区域
    if (xOf(p.duration) < width) {
      g.fillStyle = "#e5e7eb";
      g.fillRect(xOf(p.duration), RULER_H, width, height);
    }

    // 轨道 0 / 1：分段
    const drawParts = (key: "evaluable" | "context", colors: Record<string, string>) => {
      const [ry, rh] = rowY[key];
      const parts = ann[key];
      parts.forEach((part, i) => {
        const end = i + 1 < parts.length ? parts[i + 1].start_ms : p.duration;
        const x0 = xOf(part.start_ms);
        const x1 = xOf(end);
        if (x1 < LABEL_W || x0 > width) return;
        g.fillStyle = part.label ? colors[part.label] ?? "#999" : UNLABELED;
        g.fillRect(x0, ry + 3, Math.max(1, x1 - x0), rh - 6);
        if (!part.label) {
          g.strokeStyle = "#94a3b8";
          g.setLineDash([3, 3]);
          g.strokeRect(x0 + 0.5, ry + 3.5, Math.max(1, x1 - x0 - 1), rh - 7);
          g.setLineDash([]);
        }
        if (x1 - x0 > 26) {
          g.fillStyle = part.label ? "#fff" : "#64748b";
          g.fillText(part.label ?? "未标注", Math.max(x0, LABEL_W) + 4, ry + rh / 2);
        }
        if (i > 0) {
          g.fillStyle = "#0f172a";
          g.fillRect(x0 - 1, ry + 1, 2, rh - 2);
        }
      });
    };
    drawParts("evaluable", EVALUABLE_COLORS);
    drawParts("context", CONTEXT_COLORS);

    // 轨道 2 / 3：采样点
    const st = sampleTimes(p.duration, p.protocol.settings.sampling_interval_ms);
    const pxPer = plotW / span;
    const sw = Math.max(2, Math.min(10, p.protocol.settings.sampling_interval_ms * pxPer * 0.7));
    const [ay, ah] = rowY.anchors;
    const [sy, sh] = rowY.scs;
    for (const t of st) {
      if (t < view[0] - 10000 || t > view[1]) continue;
      const x = xOf(t) - sw / 2;
      const s = ann.samples[String(t)];
      const sel = p.selectedSample === t;
      // SCS
      if (s?.scs) {
        g.fillStyle = SCS_COLORS[s.scs];
        g.fillRect(x, sy + 4, sw, sh - 8);
      } else {
        g.strokeStyle = "#94a3b8";
        g.strokeRect(x + 0.5, sy + 4.5, sw - 1, sh - 9);
      }
      // 锚点 5 行
      const lh = (ah - 4) / 5;
      ANCHOR_FIELDS.forEach((f, i) => {
        const v = s?.[f];
        g.fillStyle = v ? VIS_COLORS[v] : "#f1f5f9";
        g.fillRect(x, ay + 2 + i * lh, sw, lh - 0.5);
      });
      if (sel) {
        g.strokeStyle = "#2563eb";
        g.lineWidth = 2;
        g.strokeRect(x - 1, ay, sw + 2, sy + sh - ay);
        g.lineWidth = 1;
      }
    }

    // 轨道 4 / 5：定向丢失与恢复
    const [ey, eh] = rowY.events;
    const [ry2, rh2] = rowY.recovery;
    for (const e of ann.events) {
      const x0 = xOf(e.onset_ms);
      const x1 = e.offset_ms != null ? xOf(e.offset_ms) : xOf(p.currentMs);
      g.fillStyle = e.offset_ms != null ? "#dc2626" : "rgba(220,38,38,0.35)";
      g.fillRect(x0, ey + 3, Math.max(2, x1 - x0), eh - 6);
      if (x1 - x0 > 34) {
        g.fillStyle = "#fff";
        g.fillText(e.id, x0 + 3, ey + eh / 2);
      }
      if (e.offset_ms != null && e.recovery_ms != null) {
        const xr = xOf(e.recovery_ms);
        g.strokeStyle = "#16a34a";
        g.lineWidth = 2;
        g.beginPath();
        g.moveTo(x1, ry2 + rh2 / 2);
        g.lineTo(xr, ry2 + rh2 / 2);
        g.stroke();
        g.lineWidth = 1;
        g.fillStyle = "#16a34a";
        g.beginPath();
        g.moveTo(xr, ry2 + 3);
        g.lineTo(xr + 6, ry2 + rh2 / 2);
        g.lineTo(xr, ry2 + rh2 - 3);
        g.lineTo(xr - 6, ry2 + rh2 / 2);
        g.fill();
      } else if (e.offset_ms != null) {
        g.fillStyle = "#f59e0b";
        g.fillText("待标恢复", x1 + 3, ry2 + rh2 / 2);
      }
    }

    // 轨道 6：里程碑
    if (p.gold) {
      const [my, mh] = rowY.milestones;
      for (const m of p.protocol.milestones) {
        const v = ann.milestones[m.code];
        if (!v || !v.assessable || v.time_ms == null) continue;
        const x = xOf(v.time_ms);
        g.fillStyle = CONTEXT_COLORS[m.context];
        g.fillRect(x - 0.5, my + 2, 1.5, mh - 4);
        g.fillText(m.code, x + 2, my + 8);
      }
      // 轨道 7：分割
      const [oy] = rowY.masks;
      const colors = Object.fromEntries(p.protocol.mask_classes.map((c) => [c.code, c.color]));
      p.maskObjects.forEach((o, i) => {
        const yy = oy + 3 + i * 14;
        const x0 = xOf(o.start_ms);
        const x1 = xOf(o.end_ms);
        g.fillStyle = colors[o.class] + "40";
        g.fillRect(x0, yy, Math.max(2, x1 - x0), 11);
        g.strokeStyle = colors[o.class];
        g.lineWidth = o.id === p.activeObject ? 2 : 1;
        g.strokeRect(x0 + 0.5, yy + 0.5, Math.max(2, x1 - x0 - 1), 10);
        g.lineWidth = 1;
        g.fillStyle = "#0f172a";
        g.fillText(`${o.object_id}${o.verified ? " ✓" : ""}`, Math.max(x0, LABEL_W) + 3, yy + 6);
      });
    }

    // 播放头
    const xp = xOf(p.currentMs);
    g.strokeStyle = "#2563eb";
    g.lineWidth = 1.5;
    g.beginPath();
    g.moveTo(xp, 0);
    g.lineTo(xp, height);
    g.stroke();
    g.fillStyle = "#2563eb";
    g.beginPath();
    g.moveTo(xp - 5, 0);
    g.lineTo(xp + 5, 0);
    g.lineTo(xp, 7);
    g.fill();
    g.restore();
  });

  // ---------------------------------------------------------------- 交互
  function hit(x: number, yy: number): Drag | null {
    const row = Object.entries(rowY).find(([, [ry, rh]]) => yy >= ry && yy < ry + rh)?.[0];
    if (yy < RULER_H) return { kind: "scrub" };
    if (!p.editable) return null;
    const near = (t: number | null | undefined) => t != null && Math.abs(xOf(t) - x) <= 5;
    if (row === "evaluable" || row === "context") {
      const parts = ann[row];
      for (let i = 1; i < parts.length; i++) if (near(parts[i].start_ms)) return { kind: "boundary", track: row, idx: i };
    }
    if (row === "events") {
      for (const e of ann.events) {
        if (near(e.offset_ms)) return { kind: "event", id: e.id, field: "offset_ms" };
        if (near(e.onset_ms)) return { kind: "event", id: e.id, field: "onset_ms" };
      }
    }
    if (row === "recovery") {
      for (const e of ann.events) if (near(e.recovery_ms)) return { kind: "event", id: e.id, field: "recovery_ms" };
    }
    return null;
  }

  function applyDrag(d: Drag, t: number): Annotation | null {
    const st = p.snap(Math.max(0, Math.min(p.duration, t)));
    if (d.kind === "boundary") return { ...p.ann, [d.track]: moveBoundary(p.ann[d.track], d.idx, st) };
    if (d.kind === "event") {
      return {
        ...p.ann,
        events: p.ann.events.map((e) => (e.id === d.id ? { ...e, [d.field]: st } : e)),
      };
    }
    return null;
  }

  function pos(e: React.MouseEvent) {
    const r = canvas.current!.getBoundingClientRect();
    return [e.clientX - r.left, e.clientY - r.top];
  }

  function onDown(e: React.MouseEvent) {
    const [x, yy] = pos(e);
    if (x < LABEL_W) return;
    if (e.button === 1 || e.altKey) {
      setDrag({ kind: "pan", x0: x, v0: view[0], v1: view[1] });
      return;
    }
    const h = hit(x, yy);
    if (h) {
      setDrag(h);
      if (h.kind === "scrub") p.onSeek(tOf(x));
      return;
    }
    const t = tOf(x);
    const row = Object.entries(rowY).find(([, [ry, rh]]) => yy >= ry && yy < ry + rh)?.[0];
    if (row === "anchors" || row === "scs") {
      const iv = p.protocol.settings.sampling_interval_ms;
      const s = Math.max(0, Math.min(Math.round(t / iv) * iv, Math.floor((p.duration - 1) / iv) * iv));
      p.onSelectSample(s);
      return;
    }
    p.onSeek(t);
  }

  function onMove(e: React.MouseEvent) {
    const [x, yy] = pos(e);
    if (!drag) {
      const h = x >= LABEL_W ? hit(x, yy) : null;
      setHover(h && h.kind !== "scrub" ? "ew-resize" : x >= LABEL_W ? "pointer" : "default");
      return;
    }
    if (drag.kind === "scrub") p.onSeek(tOf(x));
    else if (drag.kind === "pan") {
      const dt = ((x - drag.x0) / plotW) * (drag.v1 - drag.v0);
      const span = drag.v1 - drag.v0;
      const n0 = Math.max(0, Math.min(p.duration - span, drag.v0 - dt));
      setView([n0, n0 + span]);
    } else setPreview(applyDrag(drag, tOf(x)));
  }

  function onUp() {
    if (drag && (drag.kind === "boundary" || drag.kind === "event") && preview) p.onChange(preview);
    setDrag(null);
    setPreview(null);
  }

  function onWheel(e: React.WheelEvent) {
    const [x] = pos(e);
    const span = view[1] - view[0];
    if (e.shiftKey || Math.abs(e.deltaX) > Math.abs(e.deltaY)) {
      const d = ((e.shiftKey ? e.deltaY : e.deltaX) / plotW) * span;
      const n0 = Math.max(0, Math.min(p.duration - span, view[0] + d));
      setView([n0, n0 + span]);
      return;
    }
    const factor = Math.exp(e.deltaY * 0.002);
    const ns = Math.max(2000, Math.min(p.duration, span * factor));
    const anchor = x >= LABEL_W ? tOf(x) : p.currentMs;
    const ratio = (anchor - view[0]) / span;
    const n0 = Math.max(0, Math.min(p.duration - ns, anchor - ratio * ns));
    setView([n0, n0 + ns]);
  }

  // 阻止页面滚动
  useEffect(() => {
    const c = canvas.current;
    if (!c) return;
    const h = (e: WheelEvent) => e.preventDefault();
    c.addEventListener("wheel", h, { passive: false });
    return () => c.removeEventListener("wheel", h);
  }, []);

  function zoomTo(span: number) {
    const ns = Math.min(p.duration, span);
    const n0 = Math.max(0, Math.min(p.duration - ns, p.currentMs - ns / 2));
    setView([n0, n0 + ns]);
  }

  return (
    <div className="timeline" ref={wrap}>
      <div className="timeline-tools">
        <span className="muted small">
          视窗 {fmtTime(view[0], false)}–{fmtTime(view[1], false)} · 滚轮缩放 · Shift+滚轮/Alt+拖动平移 · 拖动黑色边界/红色事件端点可调整
        </span>
        <div className="spacer" />
        {[
          ["30s", 30000],
          ["2min", 120000],
          ["10min", 600000],
        ].map(([l, s]) => (
          <button key={l} className="btn tiny" onClick={() => zoomTo(s as number)}>
            {l}
          </button>
        ))}
        <button className="btn tiny" onClick={() => setView([0, p.duration])}>
          全程
        </button>
      </div>
      <canvas
        ref={canvas}
        style={{ width, height, cursor: hover }}
        onMouseDown={onDown}
        onMouseMove={onMove}
        onMouseUp={onUp}
        onMouseLeave={onUp}
        onWheel={onWheel}
      />
    </div>
  );
}
