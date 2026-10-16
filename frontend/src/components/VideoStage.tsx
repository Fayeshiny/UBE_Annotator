import { useEffect, useRef, useState } from "react";
import { hexToRgb, paintRle } from "../lib/rle";
import type { FrameMask } from "../lib/types";

export type Tool = "none" | "point" | "box" | "poly-add" | "poly-sub";

interface Props {
  videoRef: React.RefObject<HTMLVideoElement | null>;
  src: string;
  videoW: number;
  videoH: number;
  masks: FrameMask[];
  classColors: Record<string, string>;
  activeObject: number | null;
  tool: Tool;
  frame: number;
  showMasks: boolean;
  onPrompt: (points: number[][], labels: number[], box: number[] | null) => void;
  onPolygon: (polygon: number[][], op: "add" | "subtract") => void;
  onVideoEvent: () => void;
}

export default function VideoStage(p: Props) {
  const box = useRef<HTMLDivElement>(null);
  const overlay = useRef<HTMLCanvasElement>(null);
  const maskCanvas = useRef<HTMLCanvasElement | null>(null);
  const [rect, setRect] = useState({ x: 0, y: 0, w: 1, h: 1, cw: 1, ch: 1 });
  const [points, setPoints] = useState<number[][]>([]);
  const [labels, setLabels] = useState<number[]>([]);
  const [dragBox, setDragBox] = useState<number[] | null>(null);
  const [poly, setPoly] = useState<number[][]>([]);
  const [cursor, setCursor] = useState<number[] | null>(null);

  // 换帧/换目标/换工具时清空未提交的提示
  useEffect(() => {
    setPoints([]);
    setLabels([]);
    setPoly([]);
    setDragBox(null);
  }, [p.frame, p.activeObject, p.tool]);

  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const update = () => {
      const cw = el.clientWidth;
      const ch = el.clientHeight;
      const s = Math.min(cw / p.videoW, ch / p.videoH);
      const w = p.videoW * s;
      const h = p.videoH * s;
      setRect({ x: (cw - w) / 2, y: (ch - h) / 2, w, h, cw, ch });
    };
    const ro = new ResizeObserver(update);
    ro.observe(el);
    update();
    return () => ro.disconnect();
  }, [p.videoW, p.videoH]);

  // 掩膜层：按原始分辨率绘制到离屏画布
  useEffect(() => {
    if (!maskCanvas.current) maskCanvas.current = document.createElement("canvas");
    const mc = maskCanvas.current;
    mc.width = p.videoW;
    mc.height = p.videoH;
    const g = mc.getContext("2d")!;
    const img = g.createImageData(p.videoW, p.videoH);
    if (p.showMasks) {
      for (const m of p.masks) {
        const [r, gg, b] = hexToRgb(p.classColors[m.class] ?? "#ffffff");
        const alpha = m.object_pk === p.activeObject ? 140 : 95;
        paintRle(img, m.rle, [r, gg, b, m.exact ? alpha : Math.round(alpha * 0.5)]);
      }
    }
    g.putImageData(img, 0, 0);
    draw();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [p.masks, p.showMasks, p.activeObject, p.videoW, p.videoH, p.classColors]);

  useEffect(draw);

  function draw() {
    const c = overlay.current;
    if (!c) return;
    const dpr = window.devicePixelRatio || 1;
    c.width = rect.cw * dpr;
    c.height = rect.ch * dpr;
    const g = c.getContext("2d")!;
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, rect.cw, rect.ch);
    if (maskCanvas.current) g.drawImage(maskCanvas.current, rect.x, rect.y, rect.w, rect.h);
    const toS = (v: number[]) => [rect.x + (v[0] / p.videoW) * rect.w, rect.y + (v[1] / p.videoH) * rect.h];
    points.forEach((pt, i) => {
      const [x, y] = toS(pt);
      g.fillStyle = labels[i] ? "#22c55e" : "#ef4444";
      g.strokeStyle = "#fff";
      g.lineWidth = 2;
      g.beginPath();
      g.arc(x, y, 6, 0, Math.PI * 2);
      g.fill();
      g.stroke();
    });
    if (dragBox) {
      const [x0, y0] = toS([dragBox[0], dragBox[1]]);
      const [x1, y1] = toS([dragBox[2], dragBox[3]]);
      g.strokeStyle = "#facc15";
      g.lineWidth = 2;
      g.strokeRect(x0, y0, x1 - x0, y1 - y0);
    }
    if (poly.length) {
      g.strokeStyle = p.tool === "poly-sub" ? "#ef4444" : "#22d3ee";
      g.lineWidth = 2;
      g.beginPath();
      poly.forEach((v, i) => {
        const [x, y] = toS(v);
        if (i) g.lineTo(x, y);
        else g.moveTo(x, y);
      });
      if (cursor) {
        const [x, y] = toS(cursor);
        g.lineTo(x, y);
      }
      g.stroke();
      poly.forEach((v) => {
        const [x, y] = toS(v);
        g.fillStyle = "#fff";
        g.fillRect(x - 3, y - 3, 6, 6);
      });
    }
  }

  function toVideo(e: React.MouseEvent): number[] | null {
    const r = overlay.current!.getBoundingClientRect();
    const x = ((e.clientX - r.left - rect.x) / rect.w) * p.videoW;
    const y = ((e.clientY - r.top - rect.y) / rect.h) * p.videoH;
    if (x < 0 || y < 0 || x > p.videoW || y > p.videoH) return null;
    return [Math.round(x), Math.round(y)];
  }

  const interactive = p.tool !== "none" && p.activeObject != null;

  function onDown(e: React.MouseEvent) {
    if (!interactive) return;
    const v = toVideo(e);
    if (!v) return;
    if (p.tool === "point") {
      e.preventDefault();
      const neg = e.button === 2 || e.shiftKey || e.altKey ? 0 : 1;
      const np = [...points, v];
      const nl = [...labels, neg];
      setPoints(np);
      setLabels(nl);
      p.onPrompt(np, nl, null);
    } else if (p.tool === "box") {
      setDragBox([v[0], v[1], v[0], v[1]]);
    } else if (p.tool === "poly-add" || p.tool === "poly-sub") {
      if (poly.length >= 3) {
        const [fx, fy] = poly[0];
        if (Math.hypot(fx - v[0], fy - v[1]) < (12 * p.videoW) / rect.w) {
          finishPoly();
          return;
        }
      }
      setPoly([...poly, v]);
    }
  }

  function finishPoly() {
    if (poly.length >= 3) p.onPolygon(poly, p.tool === "poly-sub" ? "subtract" : "add");
    setPoly([]);
  }

  function onMove(e: React.MouseEvent) {
    const v = toVideo(e);
    if (p.tool === "box" && dragBox && v) setDragBox([dragBox[0], dragBox[1], v[0], v[1]]);
    if (poly.length) setCursor(v);
  }

  function onUp() {
    if (p.tool === "box" && dragBox) {
      const [x0, y0, x1, y1] = dragBox;
      if (Math.abs(x1 - x0) > 4 && Math.abs(y1 - y0) > 4)
        p.onPrompt([], [], [Math.min(x0, x1), Math.min(y0, y1), Math.max(x0, x1), Math.max(y0, y1)]);
      setDragBox(null);
    }
  }

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!poly.length) return;
      if (e.key === "Enter") {
        e.preventDefault();
        e.stopPropagation();
        finishPoly();
      } else if (e.key === "Escape") {
        e.stopPropagation();
        setPoly([]);
      } else if (e.key === "Backspace") {
        e.stopPropagation();
        setPoly(poly.slice(0, -1));
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  });

  return (
    <div className="stage" ref={box}>
      <video
        ref={p.videoRef}
        src={p.src}
        preload="auto"
        playsInline
        onSeeked={p.onVideoEvent}
        onLoadedMetadata={p.onVideoEvent}
        onPause={p.onVideoEvent}
        onPlay={p.onVideoEvent}
      />
      <canvas
        ref={overlay}
        className="overlay"
        style={{ width: rect.cw, height: rect.ch, cursor: interactive ? "crosshair" : "default", pointerEvents: interactive ? "auto" : "none" }}
        onMouseDown={onDown}
        onMouseMove={onMove}
        onMouseUp={onUp}
        onDoubleClick={() => (p.tool === "poly-add" || p.tool === "poly-sub") && finishPoly()}
        onContextMenu={(e) => e.preventDefault()}
      />
    </div>
  );
}
