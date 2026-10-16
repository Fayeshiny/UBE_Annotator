import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { roleName, statusName, useApp } from "../App";
import MaskPanel from "../components/MaskPanel";
import ConsensusPanel from "../components/ConsensusPanel";
import {
  ChecksPanel,
  copyPrevAnchors,
  currentSampleTime,
  EventsPanel,
  HistoryPanel,
  markEvent,
  MilestonePanel,
  nextIncompleteSample,
  SamplePanel,
  SegmentsPanel,
  setSampleValue,
  type WS,
} from "../components/panels";
import Timeline from "../components/Timeline";
import VideoStage, { type Tool } from "../components/VideoStage";
import { ANCHOR_FIELDS, normalizeAnchorSamples, startSegment } from "../lib/ann";
import { api, ApiError } from "../lib/api";
import { mergeMaskIssues } from "../lib/checks";
import { fmtTime, frameAt } from "../lib/time";
import type { Annotation, AssignmentPayload, FrameMask, MaskObject, SamSession } from "../lib/types";

type Tab = "samples" | "segments" | "events" | "milestones" | "masks" | "checks" | "history";

const VIS_KEYS: Record<string, string> = { v: "V", n: "N", u: "U" };
const CONTEXT_KEYS: Record<string, string> = { Digit1: "C1", Digit2: "C2", Digit3: "C3", Digit4: "C4", Digit5: "C5", Digit6: "C6", Digit0: "CU" };

export default function WorkspacePage() {
  const { aid } = useParams();
  const assignmentId = Number(aid);
  const { protocol: projectProtocol, user } = useApp();
  const [payload, setPayload] = useState<AssignmentPayload | null>(null);
  const protocol = useMemo(() => ({
    ...projectProtocol,
    settings: payload?.settings ?? projectProtocol.settings,
  }), [projectProtocol, payload?.settings]);
  const [ann, setAnn] = useState<Annotation | null>(null);
  const [frameTimes, setFrameTimes] = useState<number[]>([]);
  const [saveState, setSaveState] = useState<"saved" | "dirty" | "saving" | "error">("saved");
  const [error, setError] = useState("");
  const [tab, setTab] = useState<Tab>("samples");
  const [currentMs, setCurrentMs] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [rate, setRate] = useState(1);
  const [selectedSample, setSelectedSample] = useState<number | null>(null);
  const [activeAnchor, setActiveAnchor] = useState<number | null>(null);
  const [selectedMilestone, setSelectedMilestone] = useState("M1");
  const [showHelp, setShowHelp] = useState(false);
  // 掩膜
  const [objects, setObjects] = useState<MaskObject[]>([]);
  const [activeObject, setActiveObject] = useState<number | null>(null);
  const [tool, setTool] = useState<Tool>("none");
  const [session, setSession] = useState<SamSession | null>(null);
  const [creatingSession, setCreatingSession] = useState(false);
  const creatingSessionRef = useRef(false);
  const [frameMasks, setFrameMasks] = useState<FrameMask[]>([]);
  const [showMasks, setShowMasks] = useState(true);

  const videoRef = useRef<HTMLVideoElement | null>(null);
  const undo = useRef<Annotation[]>([]);
  const redo = useRef<Annotation[]>([]);
  const versionRef = useRef(0);
  const annRef = useRef<Annotation | null>(null);
  const rangeEnd = useRef<number | null>(null);
  annRef.current = ann;

  const c = payload?.case;
  const gold = !!c?.gold_set;
  const duration = Math.round((c?.video_duration_sec ?? 0) * 1000);
  const editable = !!payload?.editable;
  const frame = frameAt(frameTimes, currentMs + 1);

  // ---------------------------------------------------------------- 加载
  const loadObjects = useCallback(async () => {
    const [objects, refreshed] = await Promise.all([
      api.get<MaskObject[]>(`/api/assignments/${assignmentId}/mask-objects`),
      api.get<AssignmentPayload>(`/api/assignments/${assignmentId}`),
    ]);
    setObjects(objects);
    // 掩膜独立保存，不会触发时序标注自动保存；同步其检查结果，
    // 不覆盖尚未保存的时序编辑、任务版本号或其他轨道的检查项。
    setPayload((current) => current?.assignment.id === assignmentId
      ? { ...current, consensus: refreshed.consensus, issues: mergeMaskIssues(current.issues, refreshed.issues) }
      : current);
  }, [assignmentId]);

  useEffect(() => {
    (async () => {
      try {
        const p = await api.get<AssignmentPayload>(`/api/assignments/${assignmentId}`);
        setPayload(p);
        setAnn(p.data);
        versionRef.current = p.assignment.version;
        setFrameTimes(await api.get<number[]>(`/api/cases/${p.case.case_id}/frames`));
        if (p.case.gold_set) {
          await loadObjects();
          const ss = await api.get<SamSession[]>(`/api/assignments/${assignmentId}/sam/sessions`);
          if (ss.length) setSession(ss[ss.length - 1]);
        }
      } catch (e) {
        setError((e as Error).message);
      }
    })();
  }, [assignmentId, loadObjects]);

  // ---------------------------------------------------------------- 编辑与自动保存
  const update = useCallback(
    (fn: (a: Annotation) => Annotation) => {
      if (!editable) return;
      setAnn((prev) => {
        if (!prev) return prev;
        const next = normalizeAnchorSamples(fn(prev), protocol.settings);
        if (next === prev) return prev;
        undo.current.push(prev);
        if (undo.current.length > 200) undo.current.shift();
        redo.current = [];
        setSaveState("dirty");
        return next;
      });
    },
    [editable, protocol.settings],
  );

  const doUndo = useCallback(() => {
    const prev = undo.current.pop();
    if (!prev || !annRef.current) return;
    redo.current.push(annRef.current);
    setAnn(normalizeAnchorSamples(prev, protocol.settings));
    setSaveState("dirty");
  }, [protocol.settings]);

  const doRedo = useCallback(() => {
    const next = redo.current.pop();
    if (!next || !annRef.current) return;
    undo.current.push(annRef.current);
    setAnn(normalizeAnchorSamples(next, protocol.settings));
    setSaveState("dirty");
  }, [protocol.settings]);

  const save = useCallback(async () => {
    const data = annRef.current;
    if (!data) return false;
    setSaveState("saving");
    try {
      const p = await api.put<AssignmentPayload>(`/api/assignments/${assignmentId}/data`, { data, version: versionRef.current });
      versionRef.current = p.assignment.version;
      setPayload(p);
      setSaveState(annRef.current === data ? "saved" : "dirty");
      return annRef.current === data;
    } catch (e) {
      setSaveState("error");
      setError((e as Error).message + (e instanceof ApiError && e.status === 409 ? "（请刷新页面）" : ""));
      return false;
    }
  }, [assignmentId]);

  useEffect(() => {
    if (saveState !== "dirty") return;
    const t = setTimeout(save, 1200);
    return () => clearTimeout(t);
  }, [ann, saveState, save]);

  useEffect(() => {
    const h = (e: BeforeUnloadEvent) => {
      if (saveState === "dirty" || saveState === "saving") e.preventDefault();
    };
    window.addEventListener("beforeunload", h);
    return () => window.removeEventListener("beforeunload", h);
  }, [saveState]);

  // ---------------------------------------------------------------- 播放控制
  const syncFromVideo = useCallback(() => {
    const v = videoRef.current;
    if (!v) return;
    setCurrentMs(v.currentTime * 1000);
    setPlaying(!v.paused);
  }, []);

  useEffect(() => {
    const v = videoRef.current;
    if (!v || !playing) return;
    let handle = 0;
    const hasRVFC = "requestVideoFrameCallback" in v;
    const tick = (_: number, meta?: { mediaTime: number }) => {
      const t = meta ? meta.mediaTime * 1000 : v.currentTime * 1000;
      setCurrentMs(t);
      if (rangeEnd.current != null && t >= rangeEnd.current) {
        rangeEnd.current = null;
        v.pause();
      }
      handle = hasRVFC ? v.requestVideoFrameCallback(tick) : requestAnimationFrame((n) => tick(n));
    };
    handle = hasRVFC ? v.requestVideoFrameCallback(tick) : requestAnimationFrame((n) => tick(n));
    return () => (hasRVFC ? v.cancelVideoFrameCallback(handle) : cancelAnimationFrame(handle));
  }, [playing]);

  const seekFrame = useCallback(
    (f: number) => {
      const v = videoRef.current;
      if (!v || !frameTimes.length) return;
      const i = Math.max(0, Math.min(frameTimes.length - 1, f));
      const next = i + 1 < frameTimes.length ? frameTimes[i + 1] : frameTimes[i] + 33;
      // 定位到帧显示区间的中点，避免浮点误差显示成前一帧
      v.currentTime = (frameTimes[i] + (next - frameTimes[i]) / 2) / 1000;
      setCurrentMs(frameTimes[i]);
    },
    [frameTimes],
  );

  const seek = useCallback((ms: number) => seekFrame(frameAt(frameTimes, Math.max(0, ms))), [frameTimes, seekFrame]);
  const snap = useCallback((ms: number) => frameTimes[frameAt(frameTimes, ms)] ?? Math.round(ms), [frameTimes]);
  // 标注使用当前帧的时间戳
  const frameMs = frameTimes[frame] ?? Math.round(currentMs);

  const togglePlay = useCallback(() => {
    const v = videoRef.current;
    if (!v) return;
    rangeEnd.current = null;
    if (v.paused) v.play();
    else v.pause();
  }, []);

  const playRange = useCallback(
    (from: number, to: number) => {
      const v = videoRef.current;
      if (!v) return;
      seek(from);
      rangeEnd.current = to;
      v.play();
    },
    [seek],
  );

  useEffect(() => {
    if (videoRef.current) videoRef.current.playbackRate = rate;
  }, [rate]);

  // ---------------------------------------------------------------- 掩膜
  const loadMasks = useCallback(async () => {
    if (!gold || playing) return;
    setFrameMasks(await api.get<FrameMask[]>(`/api/assignments/${assignmentId}/masks?frame_idx=${frame}`));
  }, [gold, playing, assignmentId, frame]);

  useEffect(() => {
    const t = setTimeout(loadMasks, 60);
    return () => clearTimeout(t);
  }, [loadMasks]);

  const startSamSession = useCallback(
    async (start_ms: number, end_ms: number, stride: number) => {
      // A ref also blocks a second click before React renders the disabled button.
      if (creatingSessionRef.current || session?.status === "loading" || session?.status === "propagating") return;
      creatingSessionRef.current = true;
      setCreatingSession(true);
      try {
        if (session) {
          await api.del(`/api/sam/sessions/${session.id}`);
          setSession(null);
        }
        setSession(await api.post<SamSession>(`/api/assignments/${assignmentId}/sam/sessions`, { start_ms, end_ms, stride }));
      } catch (e) {
        setError((e as Error).message);
      } finally {
        creatingSessionRef.current = false;
        setCreatingSession(false);
      }
    },
    [assignmentId, session],
  );

  useEffect(() => {
    if (creatingSession || !session || (session.status !== "loading" && session.status !== "propagating")) return;
    const t = setTimeout(async () => {
      try {
        const s = await api.get<SamSession>(`/api/sam/sessions/${session.id}`);
        setSession(s);
        if (s.status === "ready" && session.status === "propagating") {
          await loadObjects();
          await loadMasks();
        }
      } catch (e) {
        if (e instanceof ApiError && [401, 403, 404].includes(e.status)) {
          setSession(null);
          setError((e as Error).message);
        } else {
          // A temporary polling failure does not mean the loading session ended.
          // Keep the button locked and trigger another polling attempt.
          setSession((current) => current?.id === session.id ? { ...current } : current);
          setError("暂时无法获取片段状态，正在重试，请勿重复建立片段。");
        }
      }
    }, 700);
    return () => clearTimeout(t);
  }, [creatingSession, session, loadObjects, loadMasks]);

  const onPrompt = useCallback(
    async (points: number[][], labels: number[], box: number[] | null) => {
      if (!session || activeObject == null) return;
      try {
        await api.post(`/api/sam/sessions/${session.id}/prompt`, { object_pk: activeObject, frame_idx: frame, points, labels, box });
        await Promise.all([loadMasks(), loadObjects()]);
      } catch (e) {
        setError((e as Error).message);
      }
    },
    [session, activeObject, frame, loadMasks, loadObjects],
  );

  const onPolygon = useCallback(
    async (polygon: number[][], op: "add" | "subtract") => {
      if (activeObject == null) return;
      try {
        await api.post(`/api/mask-objects/${activeObject}/edit`, { frame_idx: frame, op, polygon });
        await Promise.all([loadMasks(), loadObjects()]);
      } catch (e) {
        setError((e as Error).message);
      }
    },
    [activeObject, frame, loadMasks, loadObjects],
  );

  // ---------------------------------------------------------------- 工作区上下文
  const ws: WS | null = useMemo(
    () =>
      ann && payload
        ? {
            ann,
            update,
            editable,
            currentMs: frameMs,
            duration,
            seek,
            playRange,
            protocol,
            gold,
            selectedSample,
            setSelectedSample,
            issues: payload.issues,
            metrics: payload.metrics,
            assignmentId,
            activeAnchor,
            setActiveAnchor,
          }
        : null,
    [ann, payload, update, editable, frameMs, duration, seek, playRange, protocol, gold, selectedSample, assignmentId, activeAnchor],
  );

  // ---------------------------------------------------------------- 快捷键
  useEffect(() => {
    if (!ws) return;
    const onKey = (e: KeyboardEvent) => {
      const tgt = e.target as HTMLElement;
      if (tgt && (tgt.tagName === "INPUT" || tgt.tagName === "TEXTAREA" || tgt.tagName === "SELECT")) return;
      const k = e.key.toLowerCase();
      const mod = e.metaKey || e.ctrlKey;
      if (mod && k === "z") {
        e.preventDefault();
        if (e.shiftKey) doRedo();
        else doUndo();
        return;
      }
      if (mod && k === "s") {
        e.preventDefault();
        save();
        return;
      }
      if (mod) return;
      const iv = protocol.settings.sampling_interval_ms;
      const st = currentSampleTime(ws);
      if (e.altKey) {
        if (CONTEXT_KEYS[e.code]) update((a) => ({ ...a, context: startSegment(a.context, frameMs, CONTEXT_KEYS[e.code]) }));
        else if (e.code === "KeyE") update((a) => ({ ...a, evaluable: startSegment(a.evaluable, frameMs, "E1") }));
        else if (e.code === "KeyX") update((a) => ({ ...a, evaluable: startSegment(a.evaluable, frameMs, "E0") }));
        else return;
        e.preventDefault();
        return;
      }
      const step = e.shiftKey ? 1000 : 0;
      switch (e.key) {
        case " ":
          togglePlay();
          break;
        case "ArrowLeft":
          if (step) seek(frameMs - step);
          else seekFrame(frame - 1);
          break;
        case "ArrowRight":
          if (step) seek(frameMs + step);
          else seekFrame(frame + 1);
          break;
        case "ArrowUp":
          seek(frameMs - 5000);
          break;
        case "ArrowDown":
          seek(frameMs + 5000);
          break;
        case "[":
          markEvent(ws, "onset");
          break;
        case "]":
          markEvent(ws, "offset");
          break;
        case "\\":
          markEvent(ws, "recovery");
          break;
        case "?":
          setShowHelp((v) => !v);
          break;
        case "Escape":
          setActiveAnchor(null);
          setTool("none");
          break;
        default: {
          if (k === "j" || k === "l") {
            const t = Math.max(0, Math.min(Math.floor((duration - 1) / iv) * iv, st + (k === "j" ? -iv : iv)));
            setSelectedSample(t);
            seek(t);
          } else if (k === "k") {
            const w = protocol.settings.sample_review_window_ms;
            playRange(Math.max(0, st - w), Math.min(duration, st + w));
          } else if (e.key === "Enter" && tab === "samples") {
            const t = nextIncompleteSample(ws, st);
            if (t != null) {
              setSelectedSample(t);
              seek(t);
              setActiveAnchor(null);
            }
          } else if (tab === "samples" && ["0", "1", "2"].includes(e.key)) {
            setSampleValue(ws, st, "scs", e.key);
          } else if (tab === "samples" && "qwert".includes(k) && k.length === 1) {
            setActiveAnchor("qwert".indexOf(k));
          } else if (tab === "samples" && activeAnchor != null && VIS_KEYS[k]) {
            setSampleValue(ws, st, ANCHOR_FIELDS[activeAnchor], VIS_KEYS[k]);
            setActiveAnchor(activeAnchor < 4 ? activeAnchor + 1 : null);
          } else if (tab === "samples" && k === "u") {
            setSampleValue(ws, st, "scs", "U");
          } else if (tab === "samples" && k === "c") {
            copyPrevAnchors(ws, st);
          } else if (k === "m" && gold) {
            update((a) => ({ ...a, milestones: { ...a.milestones, [selectedMilestone]: { confidence: a.milestones[selectedMilestone]?.confidence ?? null, time_ms: frameMs, assessable: 1 } } }));
            const idx = protocol.milestones.findIndex((m) => m.code === selectedMilestone);
            if (idx + 1 < protocol.milestones.length) setSelectedMilestone(protocol.milestones[idx + 1].code);
          } else if (k === "h" && gold) {
            setShowMasks((v) => !v);
          } else if (k === "p" && gold && activeObject != null) {
            setTool("point");
          } else if (k === "b" && gold && activeObject != null) {
            setTool("box");
          } else return;
        }
      }
      e.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [ws, protocol, frameMs, frame, duration, tab, activeAnchor, gold, selectedMilestone, activeObject, update, doUndo, doRedo, save, seek, seekFrame, togglePlay, playRange]);

  // ---------------------------------------------------------------- 状态流转
  async function action(act: string, suppliedComment?: string) {
    let comment: string | null = suppliedComment ?? null;
    if (act === "return" && suppliedComment === undefined) {
      comment = prompt("退回原因（会记录到复核日志）");
      if (!comment) return;
    }
    if (act === "submit" && !confirm("提交后不可再修改，确认提交？")) return;
    try {
      if (editable && saveState !== "saved" && !(await save())) return;
      const p = await api.post<AssignmentPayload>(`/api/assignments/${assignmentId}/action`, {
        action: act, comment,
        consensus_round: payload?.consensus?.round,
        consensus_revision: payload?.consensus?.revision,
      });
      setPayload(p);
      versionRef.current = p.assignment.version;
    } catch (e) {
      setError((e as Error).message);
    }
  }

  if (error && !payload) return <div className="page error">{error}</div>;
  if (!payload || !ann || !ws || !c) return <div className="center muted">加载中…</div>;

  const a = payload.assignment;
  const errors = payload.issues.filter((i) => i.level === "error").length;
  const classColors = Object.fromEntries(protocol.mask_classes.map((m) => [m.code, m.color]));

  return (
    <div className="workspace">
      <div className="ws-header">
        <Link to="/" className="btn ghost small">
          ← 病例
        </Link>
        <b>{c.case_id}</b>
        {gold ? <span className="badge gold">金标准</span> : <span className="badge">核心队列</span>}
        <span className="muted small">
          {a.owner.display_name}（{a.owner.username}）· {roleName(a.role)}
        </span>
        <span className={`badge st-${a.status}`}>{statusName(a.status)}</span>
        {c.status === "locked" && <span className="badge st-locked">已锁定</span>}
        {payload.applied_rules.source === "locked_snapshot" && (
          <span className="muted small">锁定规则 #{payload.applied_rules.id} · 采样 {protocol.settings.sampling_interval_ms / 1000}s</span>
        )}
        {!editable && <span className="badge">只读</span>}
        <div className="spacer" />
        <span className={`save-state ${saveState}`}>
          {{ saved: "已保存", dirty: "未保存…", saving: "保存中…", error: "保存失败" }[saveState]} · v{a.version}
        </span>
        <span className={errors ? "error-text small" : "muted small"} onClick={() => setTab("checks")} style={{ cursor: "pointer" }}>
          {errors ? `${errors} 个错误` : "无错误"}
        </span>
        <button className="btn small" onClick={() => setShowHelp(true)}>
          快捷键 ?
        </button>
        <WorkflowButtons payload={payload} onAction={action} />
      </div>
      {error && (
        <div className="error banner" onClick={() => setError("")}>
          {error}（点击关闭）
        </div>
      )}
      {payload.rules_warning && <div className="notice banner">{payload.rules_warning}</div>}
      {payload.consensus && <ConsensusPanel key={a.id} payload={payload} onAction={action} />}
      <div className="ws-main">
        <div className="ws-left">
          <VideoStage
            videoRef={videoRef}
            src={`/api/cases/${c.case_id}/video`}
            videoW={c.width}
            videoH={c.height}
            masks={frameMasks}
            classColors={classColors}
            activeObject={activeObject}
            tool={gold && tab === "masks" ? tool : "none"}
            frame={frame}
            showMasks={gold && showMasks}
            onPrompt={onPrompt}
            onPolygon={onPolygon}
            onVideoEvent={syncFromVideo}
          />
          <div className="controls">
            <button className="btn small" onClick={() => seekFrame(frame - 1)} title="上一帧 (←)">
              ◀︎|
            </button>
            <button className="btn small primary" onClick={togglePlay} title="播放/暂停 (空格)">
              {playing ? "暂停" : "播放"}
            </button>
            <button className="btn small" onClick={() => seekFrame(frame + 1)} title="下一帧 (→)">
              |▶︎
            </button>
            <select value={rate} onChange={(e) => setRate(Number(e.target.value))}>
              {[0.25, 0.5, 1, 1.5, 2, 4].map((r) => (
                <option key={r} value={r}>
                  {r}×
                </option>
              ))}
            </select>
            <span className="timecode">
              {fmtTime(frameMs)} / {fmtTime(duration)} · 帧 {frame}
            </span>
            <div className="spacer" />
            <button className="btn small" onClick={doUndo} disabled={!editable} title="撤销 (Ctrl/⌘+Z)">
              撤销
            </button>
            <button className="btn small" onClick={doRedo} disabled={!editable} title="重做 (Ctrl/⌘+Shift+Z)">
              重做
            </button>
          </div>
        </div>
        <div className="ws-right">
          <div className="tabs">
            {(
              [
                ["samples", "采样点"],
                ["segments", "分段"],
                ["events", `事件 ${ann.events.length || ""}`],
                ...(gold ? [["milestones", "里程碑"], ["masks", "分割"]] : []),
                ["checks", errors ? `检查 (${errors})` : "检查"],
                ["history", "历史"],
              ] as [Tab, string][]
            ).map(([k, l]) => (
              <button key={k} className={tab === k ? "tab active" : "tab"} onClick={() => setTab(k)}>
                {l}
              </button>
            ))}
          </div>
          <div className="tab-body">
            {tab === "samples" && <SamplePanel ws={ws} />}
            {tab === "segments" && <SegmentsPanel ws={ws} />}
            {tab === "events" && <EventsPanel ws={ws} />}
            {tab === "milestones" && <MilestonePanel ws={ws} selected={selectedMilestone} setSelected={setSelectedMilestone} />}
            {tab === "masks" && (
              <MaskPanel
                ws={ws}
                canVerify={(user.role === "admin" || user.role === "reviewer") && payload.case.status !== "locked"}
                frame={frame}
                objects={objects}
                reload={loadObjects}
                activeObject={activeObject}
                setActiveObject={setActiveObject}
                tool={tool}
                setTool={setTool}
                session={session}
                setSession={setSession}
                creatingSession={creatingSession}
                onStartSession={startSamSession}
                showMasks={showMasks}
                setShowMasks={setShowMasks}
                onError={setError}
                seekFrame={seekFrame}
              />
            )}
            {tab === "checks" && <ChecksPanel ws={ws} />}
            {tab === "history" && <HistoryPanel ws={ws} version={a.version} />}
          </div>
        </div>
      </div>
      <Timeline
        ann={ann}
        duration={duration}
        currentMs={frameMs}
        protocol={protocol}
        gold={gold}
        editable={editable}
        maskObjects={objects}
        selectedSample={tab === "samples" ? currentSampleTime(ws) : null}
        activeObject={activeObject}
        onSeek={(ms) => {
          setSelectedSample(null);
          seek(ms);
        }}
        onSelectSample={(t) => {
          setSelectedSample(t);
          setTab("samples");
          seek(t);
        }}
        onChange={(n) => update(() => n)}
        snap={snap}
      />
      {showHelp && <Help onClose={() => setShowHelp(false)} />}
    </div>
  );
}

function WorkflowButtons({ payload, onAction }: { payload: AssignmentPayload; onAction: (a: string) => void }) {
  const { user } = useApp();
  const a = payload.assignment;
  const locked = payload.case.status === "locked";
  const isReviewer = user.role !== "annotator";
  const mine = a.user_id === user.id;
  if (locked) return null;
  if (a.role === "consensus") return (
    <>
      {mine && isReviewer && (a.status === "in_progress" || a.status === "returned") && (
        <button className="btn small primary" onClick={() => onAction("submit")}>提交三专家共识</button>
      )}
      {mine && isReviewer && (a.status === "submitted" || a.status === "approved") && (
        <button className="btn small" onClick={() => {
          if (confirm("重新打开将使本轮全部专家确认失效，确定？")) onAction("reopen");
        }}>重新打开共识</button>
      )}
    </>
  );
  return (
    <>
      {mine && (a.status === "in_progress" || a.status === "returned") && (
        <button className="btn small primary" onClick={() => onAction("submit")}>
          提交复核
        </button>
      )}
      {isReviewer && a.status === "submitted" && !mine && (
        <>
          <button className="btn small primary" onClick={() => onAction("approve")}>
            复核通过
          </button>
          <button className="btn small danger" onClick={() => onAction("return")}>
            退回
          </button>
        </>
      )}
      {(a.status === "approved" || (a.status === "submitted" && mine)) && (isReviewer || mine) && (
        <button className="btn small" onClick={() => onAction("reopen")}>
          重新打开
        </button>
      )}
    </>
  );
}

function Help({ onClose }: { onClose: () => void }) {
  const rows: [string, string][] = [
    ["空格", "播放 / 暂停"],
    ["← / →", "上一帧 / 下一帧"],
    ["Shift + ← / →", "后退 / 前进 1 秒"],
    ["↑ / ↓", "后退 / 前进 5 秒"],
    ["J / L", "上一个 / 下一个采样点"],
    ["K", "回看当前采样点 ±窗口"],
    ["Enter", "跳到下一个未完成的采样点"],
    ["2 / 1 / 0 / U", "SCS = 稳定 / 不稳定 / 定向丢失 / 无法判断"],
    ["Q W E R T", "选中锚点 A1–A5 行，然后按 V / N / U，自动跳到下一行；不适用锚点固定 NA"],
    ["C", "复制上一采样点的锚点"],
    ["Alt + 1…6 / Alt + 0", "从当前帧开始空间情境 C1…C6 / CU"],
    ["Alt + E / Alt + X", "从当前帧开始 E1 可评价 / E0 不可评价"],
    ["[ / ] / \\", "定向丢失开始 / 结束 / 恢复点"],
    ["M", "把选中的里程碑设为当前帧（金标准）"],
    ["P / B / H", "点提示 / 框提示 / 显示隐藏掩膜（金标准）"],
    ["Ctrl/⌘ + Z / Shift + Z", "撤销 / 重做"],
    ["Ctrl/⌘ + S", "立即保存（平时自动保存）"],
  ];
  return (
    <div className="modal-bg" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h3>快捷键</h3>
        <table className="table compact">
          <tbody>
            {rows.map(([k, v]) => (
              <tr key={k}>
                <td>
                  <kbd>{k}</kbd>
                </td>
                <td>{v}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <button className="btn" onClick={onClose}>
          关闭
        </button>
      </div>
    </div>
  );
}
