import { useEffect, useRef, useState } from "react";
import { videoErrorMessage } from "../lib/videoErrors";
import { hexToRgb, paintRle } from "../lib/rle";
import type { CandidateMask, FrameMask } from "../lib/types";

export type Tool = "none" | "point" | "box" | "poly-add" | "poly-sub";

interface Props {
  videoRef: React.RefObject<HTMLVideoElement | null>;
  src: string | undefined;
  videoW: number;
  videoH: number;
  imageSrc: string | null;
  frameReady: boolean;
  showVideo: boolean;
  showPausedVideo: boolean;
  playbackStarting: boolean;
  frameError: string;
  onRetryFrame: () => void;
  frameElapsedMs: number;
  frameCached: boolean;
  masks: FrameMask[];
  candidates?: CandidateMask[];
  classColors: Record<string, string>;
  activeObject: number | null;
  tool: Tool;
  frame: number;
  showMasks: boolean;
  onPrompt: (points: number[][], labels: number[], box: number[] | null) => void;
  onPolygon: (polygon: number[][], op: "add" | "subtract") => void;
  onVideoEvent: () => void;
  onPlaybackError?: (message: string) => void;
}

export default function VideoStage(p: Props) {
  const box = useRef<HTMLDivElement>(null);
  const overlay = useRef<HTMLCanvasElement>(null);
  const maskCanvas = useRef<HTMLCanvasElement | null>(null);
  const [seeking, setSeeking] = useState(false);
  const [buffering, setBuffering] = useState(false);
  const [videoError, setVideoError] = useState("");
  useEffect(() => { setSeeking(false); setBuffering(false); setVideoError(""); }, [p.src]);
  const [rect, setRect] = useState({ x: 0, y: 0, w: 1, h: 1, cw: 1, ch: 1 });
  const [points, setPoints] = useState<number[][]>([]);
  const [labels, setLabels] = useState<number[]>([]);
  const [dragBox, setDragBox] = useState<number[] | null>(null);
  const [poly, setPoly] = useState<number[][]>([]);
  const [cursor, setCursor] = useState<number[] | null>(null);

  // 换帧/换目标/换工具时清空未提交的提示
  useEffect(() => {
    setPoints((current) => current.length ? [] : current);
    setLabels((current) => current.length ? [] : current);
    setPoly((current) => current.length ? [] : current);
    setDragBox(null);
  }, [p.frame, p.activeObject, p.tool, p.frameReady]);

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
    // Empty overlays are common (all core-cohort videos). Avoid allocating an
    // entire RGBA video frame on every playback/scrubbing render.
    if (!p.frameReady || ((!p.showMasks || !p.masks.length) && !p.candidates?.length)) {
      maskCanvas.current = null;
      draw();
      return;
    }
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
    for (const candidate of p.candidates ?? []) {
      // 不同候选使用不同颜色，便于查看重叠区域；不赋予解剖类别。
      const palette = ["#22d3ee", "#f97316", "#a78bfa", "#4ade80", "#f472b6", "#facc15"];
      const [r, gg, b] = hexToRgb(palette[(candidate.id - 1) % palette.length]);
      paintRle(img, candidate.rle, [r, gg, b, 100]);
    }
    g.putImageData(img, 0, 0);
    draw();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [p.masks, p.candidates, p.showMasks, p.activeObject, p.videoW, p.videoH, p.classColors, p.frameReady]);

  useEffect(draw);

  function draw() {
    const c = overlay.current;
    if (!c) return;
    const dpr = window.devicePixelRatio || 1;
    const width = Math.round(rect.cw * dpr);
    const height = Math.round(rect.ch * dpr);
    if (c.width !== width) c.width = width;
    if (c.height !== height) c.height = height;
    const g = c.getContext("2d")!;
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, rect.cw, rect.ch);
    if (!p.frameReady) return;
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

  const interactive = p.frameReady && !p.showVideo && !p.playbackStarting && p.tool !== "none" && p.activeObject != null;

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
    if (!interactive) return;
    if (poly.length >= 3) p.onPolygon(poly, p.tool === "poly-sub" ? "subtract" : "add");
    setPoly([]);
  }

  function onMove(e: React.MouseEvent) {
    if (!interactive) return;
    const v = toVideo(e);
    if (p.tool === "box" && dragBox && v) setDragBox([dragBox[0], dragBox[1], v[0], v[1]]);
    if (poly.length) setCursor(v);
  }

  function onUp() {
    if (!interactive) return;
    if (p.tool === "box" && dragBox) {
      const [x0, y0, x1, y1] = dragBox;
      if (Math.abs(x1 - x0) > 4 && Math.abs(y1 - y0) > 4)
        p.onPrompt([], [], [Math.min(x0, x1), Math.min(y0, y1), Math.max(x0, x1), Math.max(y0, y1)]);
      setDragBox(null);
    }
  }

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!poly.length || !interactive) return;
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
    <div className="stage" ref={box}
      data-requested-frame={p.frame} data-displayed-frame={p.imageSrc || p.showVideo || p.showPausedVideo ? p.frame : ""}
      data-frame-ready={p.frameReady} data-playback={p.showVideo ? "playing" : p.playbackStarting ? "starting" : "still"}
      data-paused-preview={p.showPausedVideo}
      data-frame-ms={Math.round(p.frameElapsedMs)} data-frame-cached={p.frameCached}
      aria-busy={!p.frameReady && !p.showVideo}>
      <video
        ref={p.videoRef}
        src={p.src}
        preload="metadata"
        style={{ visibility: p.showVideo || p.showPausedVideo ? "visible" : "hidden" }}
        playsInline
        onSeeking={() => setSeeking(true)}
        onSeeked={() => {
          p.onVideoEvent();
          setSeeking(!!p.videoRef.current?.seeking);
        }}
        onError={() => {
          const video = p.videoRef.current;
          if (!p.src || !video?.error) return;
          setSeeking(false); setBuffering(false);
          const message = videoErrorMessage(video.error.code);
          setVideoError(message);
          console.warn("ssi-video-error", { code: video.error.code, detail: video.error.message,
            time: video.currentTime, local: p.src.startsWith("blob:") });
          p.onPlaybackError?.(message);
        }}
        onLoadStart={() => { setVideoError(""); setBuffering(false); }}
        onWaiting={() => setBuffering(true)}
        onStalled={() => setBuffering(true)}
        onPlaying={() => { setBuffering(false); setVideoError(""); }}
        onLoadedMetadata={p.onVideoEvent}
        onCanPlay={() => { setBuffering(false); setVideoError(""); }}
        onPause={p.onVideoEvent}
        onPlay={p.onVideoEvent}
        onEnded={p.onVideoEvent}
      />
      {p.imageSrc && !p.showVideo && <img className="frame-image" src={p.imageSrc}
        alt={`视频帧 ${p.frame}`} draggable={false} />}
      {!p.showVideo && !p.frameReady && (
        <div className={`frame-status${p.imageSrc || p.showPausedVideo ? " frame-status-preview" : ""}`} role="status" aria-live="polite">
          <span>{p.playbackStarting ? videoError || "正在准备播放…" : p.frameError || (p.showPausedVideo ? "画面已暂停，正在准备标注…" : `正在加载帧 ${p.frame}…`)}</span>
          {p.frameError && !p.playbackStarting && <button className="btn small" onClick={p.onRetryFrame}>重试加载</button>}
        </div>
      )}
      {p.showVideo && (seeking || buffering || videoError) && (
        <div role="status" style={{ position: "absolute", top: 12, left: 12, zIndex: 2, padding: "6px 10px", borderRadius: 4, background: "rgba(15,23,42,0.85)", color: "white", pointerEvents: "none" }}>
          {videoError || (buffering ? p.src?.startsWith("blob:") ? "正在解码本地视频…" : "正在缓冲视频…可暂停后等待本段缓存完成" : "正在定位画面…")}
        </div>
      )}
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
