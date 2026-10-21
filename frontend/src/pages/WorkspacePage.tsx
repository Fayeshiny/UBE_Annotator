import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
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
import VideoCacheNotice from "../components/VideoCacheNotice";
import { resetFailedVideo } from "../lib/videoErrors";
import { editSegment } from "../lib/videoSegments";
import { useWholeVideoCache } from "../lib/useWholeVideoCache";
import { useLocalVideoFrame } from "../lib/useLocalVideoFrame";
import VideoPreparationNotice, { preparationReady } from "../components/VideoPreparationNotice";
import VideoImportNotice from "../components/VideoImportNotice";
import { ANCHOR_FIELDS, normalizeAnchorSamples, startSegment } from "../lib/ann";
import { api, ApiError } from "../lib/api";
import { mergeMaskIssues } from "../lib/checks";
import { fmtTime, frameAt, presentedFrameAt } from "../lib/time";
import { useFrameImage } from "../lib/useFrameImage";
import { VideoPreparation, videoFrameTime } from "../lib/videoPlayback";
import type { Annotation, AssignmentPayload, CandidateMask, FrameMask, MaskObject, SamSession } from "../lib/types";

type Tab = "segments" | "samples" | "events" | "milestones" | "masks" | "checks" | "history";

const VIS_KEYS: Record<string, string> = { v: "V", n: "N", u: "U" };
const CONTEXT_KEYS: Record<string, string> = { Digit1: "C1", Digit2: "C2", Digit3: "C3", Digit4: "C4", Digit5: "C5", Digit6: "C6", Digit0: "CU" };

export default function WorkspacePage() {
  const { aid } = useParams();
  const assignmentId = Number(aid);
  const [searchParams, setSearchParams] = useSearchParams();
  const segmentIndex = Math.max(0, Number(searchParams.get("segment")) || 0);
  const [segmentMedia, setSegmentMedia] = useState<{ key: string; status: string; frame_times?: number[]; error?: string } | null>(null);
  const [segmentAttempt, setSegmentAttempt] = useState(0);
  const [switchingSegment, setSwitchingSegment] = useState(false);
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
  const [playbackFailure, setPlaybackFailure] = useState("");
  const [tab, setTab] = useState<Tab>("segments");
  const [currentMs, setCurrentMs] = useState(0);
  const [relativeStepMs, setRelativeStepMs] = useState<number | null>(null);
  const [playing, setPlaying] = useState(false);
  const [playbackStarting, setPlaybackStarting] = useState(false);
  const [pausedVideoFrame, setPausedVideoFrame] = useState<{ identity: string; frame: number } | null>(null);
  const presentedVideoFrame = useRef<{ identity: string; frame: number } | null>(null);
  const videoMode = useRef<"still" | "starting" | "playing">("still");
  const playbackRequest = useRef<AbortController | null>(null);
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
  const [frameMasks, setFrameMasks] = useState<{ identity: string; masks: FrameMask[] } | null>(null);
  const maskRequest = useRef(0);
  const [showMasks, setShowMasks] = useState(true);
  const [candidatePreview, setCandidatePreview] = useState<{ sessionId: string; frame: number; masks: CandidateMask[] } | null>(null);
  const showCandidates = useCallback((sessionId: string, frame: number, masks: CandidateMask[]) => {
    setCandidatePreview({ sessionId, frame, masks });
  }, []);

  const videoRef = useRef<HTMLVideoElement | null>(null);
  useEffect(() => () => { playbackRequest.current?.abort(); }, [assignmentId]);
  const undo = useRef<Annotation[]>([]);
  const redo = useRef<Annotation[]>([]);
  const versionRef = useRef(0);
  const annRef = useRef<Annotation | null>(null);
  const rangeEnd = useRef<number | null>(null);
  annRef.current = ann;

  const classColors = useMemo(() => Object.fromEntries(protocol.mask_classes.map((m) => [m.code, m.color])), [protocol.mask_classes]);
  const c = payload?.case;
  const gold = !!c?.gold_set;
  const duration = Math.round((c?.video_duration_sec ?? 0) * 1000);
  const readyForAnnotation = !!c && preparationReady(c);
  const canEditAssignment = !!payload?.editable && readyForAnnotation;
  const segmented = (payload?.segments?.length ?? 0) > 1;
  const segment = segmented ? payload?.segments.find(s => s.index === segmentIndex) ?? payload?.segments[0] : undefined;
  const coreStart = segment?.start_ms ?? 0, coreEnd = segment?.end_ms ?? duration;
  const clipFirst = segment?.clip_first_frame ?? 0;
  const clipLast = segment?.clip_last_frame ?? Math.max(0, frameTimes.length - 1);
  const inCore = !segment || currentMs >= coreStart && currentMs < coreEnd;
  const editable = canEditAssignment && inCore && !switchingSegment;
  const frame = Math.max(clipFirst, Math.min(clipLast, frameAt(frameTimes, currentMs + 0.001)));
  const mediaRevision = c?.media_revision ?? "";
  const originalIdentity = `${user.id}:${assignmentId}:${c?.case_id}:${mediaRevision || `${c?.video_file}:${c?.frame_count}:${c?.width}:${c?.height}`}`;
  const segmentKey = segment ? `${c?.case_id}:${segment.index}:${segment.revision}` : "";
  const activeSegmentMedia = segmentMedia?.key === segmentKey ? segmentMedia : null;
  const playbackReady = !segmented || activeSegmentMedia?.status === "ready";
  const playbackTimes = useMemo(() => segment ? activeSegmentMedia?.frame_times ?? [] : frameTimes,
    [segment, activeSegmentMedia?.frame_times, frameTimes]);
  const playbackRevision = segment?.revision ?? mediaRevision;
  const mediaIdentity = `${originalIdentity}:${segmentKey}`;
  const onlineVideoSource = segment
    ? `/api/cases/${encodeURIComponent(c?.case_id ?? "")}/segments/${segment.index}/video?revision=${encodeURIComponent(playbackRevision)}`
    : `/api/cases/${encodeURIComponent(c?.case_id ?? "")}/video?revision=${encodeURIComponent(mediaRevision)}`;
  useEffect(() => {
    if (!segment || !c || !readyForAnnotation) return;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    const load = async () => {
      try {
        const value = await api.get<{ status: string; frame_times?: number[]; error?: string }>(
          `/api/cases/${encodeURIComponent(c.case_id)}/segments/${segment.index}`);
        if (disposed) return;
        setSegmentMedia({ ...value, key: segmentKey });
        if (value.status === "processing") timer = setTimeout(load, 1500);
      } catch (e) {
        if (!disposed) setSegmentMedia({ key: segmentKey, status: "error", error: (e as Error).message });
      }
    };
    void load();
    return () => { disposed = true; clearTimeout(timer); };
  }, [segmentKey, segmentAttempt, readyForAnnotation]);
  useEffect(() => {
    if (!segment || !frameTimes.length) return;
    setCurrentMs(frameTimes[segment.first_frame]);
    setSelectedSample(null);
    setPausedVideoFrame(null);
    undo.current = []; redo.current = [];
  }, [segmentKey, frameTimes]);
  const priorityFrames = useMemo(() => [frame, frameAt(frameTimes, currentMs + 1000),
    frameAt(frameTimes, Math.max(0, currentMs - 1000)),
    frameAt(frameTimes, (Math.floor(currentMs / protocol.settings.sampling_interval_ms) + 1)
      * protocol.settings.sampling_interval_ms)],
    [frame, frameTimes, currentMs, protocol.settings.sampling_interval_ms]);
  const wholeVideo = useWholeVideoCache(mediaIdentity, onlineVideoSource, playbackRevision,
    readyForAnnotation && !!mediaRevision && playbackReady, playing || playbackStarting,
    segment ? priorityFrames.map(f => f - clipFirst).filter(f => f >= 0 && f < playbackTimes.length) : priorityFrames);
  // Adopt a complete file only while idle, so playback is never interrupted.
  const [localVideo, setLocalVideo] = useState<{ identity: string; url: string } | null>(null);
  const localVideoSource = localVideo?.identity === mediaIdentity && wholeVideo.url === localVideo.url
    ? localVideo.url : null;
  const streamingVideoSource = `${onlineVideoSource}&playback=true`;
  const videoSource = localVideoSource ?? streamingVideoSource;
  useEffect(() => {
    if (playing || playbackStarting) return;
    if ((localVideo?.identity === mediaIdentity ? localVideo.url : null) === wholeVideo.url) return;
    setLocalVideo(wholeVideo.url ? { identity: mediaIdentity, url: wholeVideo.url } : null);
    setPausedVideoFrame(null);
  }, [wholeVideo.url, mediaIdentity, playing, playbackStarting, localVideo]);
  const videoPreparation = useMemo(() => new VideoPreparation(), [mediaIdentity, videoSource]);
  useEffect(() => () => videoPreparation.cancel(), [videoPreparation]);
  const localFrame = useLocalVideoFrame(videoRef, localVideoSource, frame - clipFirst, playbackTimes, !playing && !playbackStarting);
  const remoteFrame = useFrameImage(originalIdentity, c?.case_id, mediaRevision, frame, frameTimes,
    protocol.settings.sampling_interval_ms, !playing && readyForAnnotation && (!localVideoSource || !!localFrame.error),
    tab === "samples", relativeStepMs, !playbackStarting, clipFirst, clipLast);
  const stillFrame = localVideoSource && !localFrame.error
    ? { ...localFrame, url: null, cached: true } : remoteFrame;
  const maskReady = stillFrame.ready && !playing && !playbackStarting;
  const showPausedVideo = !playing && !playbackStarting && (!!localVideoSource && localFrame.ready
    || !stillFrame.ready && pausedVideoFrame?.identity === mediaIdentity && pausedVideoFrame.frame === frame);
  // A hidden remote <video> can download megabytes after a speculative seek.
  // Attach it only for explicit Play or while retaining a genuine pause preview.
  const attachedVideoSource = localVideoSource ?? (playing || playbackStarting || showPausedVideo ? streamingVideoSource : undefined);
  useEffect(() => {
    if (!attachedVideoSource) videoRef.current?.load();
  }, [attachedVideoSource]);
  const { holdForFrame, releaseForFrame } = wholeVideo;
  useEffect(() => {
    // A failed remote player must not keep a stalled frame/prefetch request in
    // charge of the download forever. Let the retained full-file cache recover.
    if (playbackFailure && !playing && !playbackStarting) {
      const timer = setTimeout(releaseForFrame, 1000);
      return () => clearTimeout(timer);
    }
    if ((!maskReady && !stillFrame.error) || remoteFrame.prefetching) { holdForFrame(); return; }
    // Leave time for likely next frame requests; rapid scrubbing keeps the
    // full-file download paused instead of repeatedly restarting a 4 MiB part.
    const timer = setTimeout(releaseForFrame, 1000);
    return () => clearTimeout(timer);
  }, [frame, maskReady, stillFrame.error, remoteFrame.prefetching, holdForFrame, releaseForFrame, playbackFailure, playing, playbackStarting]);
  const displayReady = playing || maskReady;
  const currentFrameIdentity = `${mediaIdentity}:${frame}`;
  const frameIdentityRef = useRef(currentFrameIdentity);
  frameIdentityRef.current = currentFrameIdentity;
  const frameReadyRef = useRef(false);
  frameReadyRef.current = maskReady;

  useEffect(() => {
    if (c?.media_status !== "ready" || !frameTimes.length || segmented) return;
    // The server deduplicates this sparse, low priority warmup for other raters.
    void api.post(`/api/cases/${encodeURIComponent(c.case_id)}/frame-cache/warm`, {
      sampling_interval_ms: protocol.settings.sampling_interval_ms,
    }).catch(() => { /* A warmup failure never blocks interactive frame requests. */ });
  }, [c?.case_id, c?.media_status, mediaRevision, frameTimes.length, protocol.settings.sampling_interval_ms, segmented]);

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
      ? { ...current, consensus: refreshed.consensus, segments: refreshed.segments, issues: mergeMaskIssues(current.issues, refreshed.issues) }
      : current);
  }, [assignmentId]);

  useEffect(() => {
    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let waitingForMedia = false;
    const load = async () => {
      try {
        const p = await api.get<AssignmentPayload>(`/api/assignments/${assignmentId}`);
        if (disposed) return;
        setPayload(p);
        setAnn(p.data);
        versionRef.current = p.assignment.version;
        waitingForMedia = p.case.media_status === "processing" || (p.case.media_status === "ready" && !preparationReady(p.case));
        if (!preparationReady(p.case)) {
          setFrameTimes([]);
          return;
        }
        const times = await api.get<number[]>(`/api/cases/${p.case.case_id}/frames`);
        if (disposed) return;
        setFrameTimes(times);
        if (p.case.gold_set) {
          await loadObjects();
          const ss = await api.get<SamSession[]>(`/api/assignments/${assignmentId}/sam/sessions`);
          if (ss.length) setSession(ss[ss.length - 1]);
        }
      } catch (e) {
        if (!disposed) setError((e as Error).message);
      } finally {
        // 处理中遇到短暂断连也继续查询，避免等待页永久停住。
        if (!disposed && waitingForMedia) timer = setTimeout(load, 3000);
      }
    };
    load();
    return () => { disposed = true; clearTimeout(timer); };
  }, [assignmentId, loadObjects]);

  // ---------------------------------------------------------------- 编辑与自动保存
  const update = useCallback(
    (fn: (a: Annotation) => Annotation) => {
      if (!editable || !displayReady) return;
      setAnn((prev) => {
        if (!prev) return prev;
        const edited = fn(prev);
        const next = normalizeAnchorSamples(segment ? editSegment(prev, edited, segment, duration) : edited, protocol.settings);
        if (next === prev) return prev;
        undo.current.push(prev);
        if (undo.current.length > 200) undo.current.shift();
        redo.current = [];
        setSaveState("dirty");
        return next;
      });
    },
    [editable, displayReady, protocol.settings, segment, duration],
  );

  const doUndo = useCallback(() => {
    if (!editable) return;
    const prev = undo.current.pop();
    if (!prev || !annRef.current) return;
    redo.current.push(annRef.current);
    setAnn(normalizeAnchorSamples(prev, protocol.settings));
    setSaveState("dirty");
  }, [protocol.settings, editable]);

  const doRedo = useCallback(() => {
    if (!editable) return;
    const next = redo.current.pop();
    if (!next || !annRef.current) return;
    undo.current.push(annRef.current);
    setAnn(normalizeAnchorSamples(next, protocol.settings));
    setSaveState("dirty");
  }, [protocol.settings, editable]);

  const savingRef = useRef<Promise<boolean> | null>(null);
  const save = useCallback(async (): Promise<boolean> => {
    if (savingRef.current) return savingRef.current;
    const work = async () => {
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
    };
    const pending = work(); savingRef.current = pending;
    try { return await pending; } finally { if (savingRef.current === pending) savingRef.current = null; }
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
  const pausePlayback = useCallback((presentedFrame?: number) => {
    const v = videoRef.current;
    const wasPlaying = videoMode.current === "playing";
    videoMode.current = "still";
    playbackRequest.current?.abort();
    playbackRequest.current = null;
    setPlaybackStarting(false);
    setPlaying(false);
    v?.pause();
    if (wasPlaying && v) {
      // Read the stopped playback clock after pause(). The last JS presentation
      // callback can lag the video surface by a frame at the instant of a click.
      const stoppedFrame = v.ended ? clipLast
        : presentedFrame ?? clipFirst + frameAt(playbackTimes, v.currentTime * 1000 + 0.001);
      setCurrentMs(frameTimes[stoppedFrame] ?? v.currentTime * 1000);
      setPausedVideoFrame(v.readyState >= 2 && !v.seeking
        ? { identity: mediaIdentity, frame: stoppedFrame } : null);
    }
  }, [frameTimes, mediaIdentity, clipFirst, clipLast, playbackTimes]);

  const syncFromVideo = useCallback(() => {
    const v = videoRef.current;
    // Metadata and old seek/pause events must never overwrite a requested still.
    if (!v || videoMode.current !== "playing" || v.seeking) return;
    if (v.paused) pausePlayback();
  }, [pausePlayback]);

  useEffect(() => {
    const v = videoRef.current;
    if (!v || !playing) return;
    let handle = 0;
    const hasRVFC = "requestVideoFrameCallback" in v;
    const tick = (_: number, meta?: { mediaTime: number }) => {
      if (videoMode.current !== "playing") return;
      const t = meta ? meta.mediaTime * 1000 : v.currentTime * 1000;
      const shownFrame = clipFirst + (meta ? presentedFrameAt(playbackTimes, t) : frameAt(playbackTimes, t + 0.001));
      presentedVideoFrame.current = { identity: mediaIdentity, frame: shownFrame };
      setCurrentMs(frameTimes[shownFrame] ?? t);
      if (rangeEnd.current != null && (frameTimes[shownFrame] ?? 0) >= rangeEnd.current) {
        rangeEnd.current = null;
        // Here the presentation callback itself triggers the pause. At high
        // speed that image can lead currentTime, so keep its known frame.
        pausePlayback(meta ? shownFrame : undefined);
        return;
      }
      handle = hasRVFC ? v.requestVideoFrameCallback(tick) : requestAnimationFrame((n) => tick(n));
    };
    handle = hasRVFC ? v.requestVideoFrameCallback(tick) : requestAnimationFrame((n) => tick(n));
    return () => (hasRVFC ? v.cancelVideoFrameCallback(handle) : cancelAnimationFrame(handle));
  }, [playing, pausePlayback, frameTimes, mediaIdentity, clipFirst, playbackTimes]);

  const seekFrame = useCallback((f: number, prefetchStepMs: number | null = null) => {
    if (!frameTimes.length) return;
    const i = Math.max(clipFirst, Math.min(clipLast, f));
    setPlaybackFailure("");
    holdForFrame();
    pausePlayback();
    videoPreparation.cancel();
    setPausedVideoFrame(null);
    presentedVideoFrame.current = null;
    rangeEnd.current = null;
    setRelativeStepMs(prefetchStepMs);
    // Load the exact JPEG; remote video stays detached until explicit Play.
    setCurrentMs(frameTimes[i]);
  }, [frameTimes, pausePlayback, videoPreparation, holdForFrame, clipFirst, clipLast]);

  const seek = useCallback((ms: number, prefetchStepMs: number | null = null) => {
    const target = frameAt(frameTimes, Math.max(0, ms));
    if (target < clipFirst || target > clipLast) {
      setError("目标时间属于其他视频子任务，请先在顶部选择对应子任务。"); return;
    }
    seekFrame(target, prefetchStepMs);
  }, [frameTimes, seekFrame, clipFirst, clipLast]);
  const snap = useCallback((ms: number) => frameTimes[frameAt(frameTimes, ms)] ?? Math.round(ms), [frameTimes]);
  const frameMs = frameTimes[frame] ?? Math.round(currentMs);

  const beginPlayback = useCallback((target: number, end: number | null = null) => {
    const v = videoRef.current;
    if (!v || !frameTimes.length || !playbackReady || !playbackTimes.length) return;
    target = Math.max(clipFirst, Math.min(clipLast, target));
    pausePlayback();
    setPlaybackFailure("");
    setPausedVideoFrame(null);
    presentedVideoFrame.current = null;
    const controller = new AbortController();
    playbackRequest.current = controller;
    videoMode.current = "starting";
    setPlaybackStarting(true);
    rangeEnd.current = end;
    setCurrentMs(frameTimes[target]);
    if (v.getAttribute("src") !== videoSource) v.src = videoSource;
    resetFailedVideo(v);
    v.playbackRate = rate;
    void videoPreparation.start(v, videoFrameTime(playbackTimes, target - clipFirst), controller.signal, (seconds) => {
      if (controller.signal.aborted || playbackRequest.current !== controller) return;
      presentedVideoFrame.current = { identity: mediaIdentity, frame: clipFirst + presentedFrameAt(playbackTimes, seconds * 1000) };
    }).then(() => {
      if (controller.signal.aborted || playbackRequest.current !== controller) return;
      if (v.paused || v.ended) { pausePlayback(); return; }
      videoMode.current = "playing";
      const shown = presentedVideoFrame.current;
      setCurrentMs(shown?.identity === mediaIdentity ? frameTimes[shown.frame] : frameTimes[clipFirst + frameAt(playbackTimes, v.currentTime * 1000)]);
      setPlaybackStarting(false);
      setPlaying(true);
    }).catch((e: Error) => {
      if (controller.signal.aborted || playbackRequest.current !== controller) return;
      pausePlayback();
      setPlaybackFailure(e.message);
    });
  }, [frameTimes, mediaIdentity, pausePlayback, videoPreparation, videoSource, rate, playbackReady, playbackTimes, clipFirst, clipLast]);

  const togglePlay = useCallback(() => {
    if (videoMode.current !== "still") pausePlayback();
    else beginPlayback(frame);
  }, [frame, beginPlayback, pausePlayback]);

  const playRange = useCallback((from: number, to: number) => {
    beginPlayback(frameAt(frameTimes, Math.max(0, from)), to);
  }, [frameTimes, beginPlayback]);

  useEffect(() => {
    if (videoRef.current) videoRef.current.playbackRate = rate;
  }, [rate, c?.case_id]);

  // ---------------------------------------------------------------- 掩膜
  const loadMasks = useCallback(async () => {
    if (!gold || !maskReady) return;
    const request = ++maskRequest.current;
    const identity = currentFrameIdentity;
    try {
      const masks = await api.get<FrameMask[]>(`/api/assignments/${assignmentId}/masks?frame_idx=${frame}`);
      if (request === maskRequest.current && frameIdentityRef.current === identity && frameReadyRef.current)
        setFrameMasks({ identity, masks });
    } catch (e) {
      if (request === maskRequest.current && frameIdentityRef.current === identity) setError((e as Error).message);
    }
  }, [gold, maskReady, assignmentId, frame, currentFrameIdentity]);

  useEffect(() => {
    const t = setTimeout(loadMasks, 60);
    return () => { clearTimeout(t); maskRequest.current++; };
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
        setSession(await api.post<SamSession>(`/api/assignments/${assignmentId}/sam/sessions`, { start_ms: Math.max(segment ? frameTimes[segment.first_frame] : 0, start_ms), end_ms: Math.min(segment ? frameTimes[segment.last_frame] : coreEnd - 1, end_ms), stride }));
      } catch (e) {
        setError((e as Error).message);
      } finally {
        creatingSessionRef.current = false;
        setCreatingSession(false);
      }
    },
    [assignmentId, session, coreStart, coreEnd, segment, frameTimes],
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
      if (!session || activeObject == null || !editable || !frameReadyRef.current
        || segment && (session.start_ms < coreStart || session.end_ms >= coreEnd)) return;
      try {
        await api.post(`/api/sam/sessions/${session.id}/prompt`, { object_pk: activeObject, frame_idx: frame, points, labels, box });
        await Promise.all([loadMasks(), loadObjects()]);
      } catch (e) {
        setError((e as Error).message);
      }
    },
    [session, activeObject, frame, editable, loadMasks, loadObjects, segment, coreStart, coreEnd],
  );

  const onPolygon = useCallback(
    async (polygon: number[][], op: "add" | "subtract") => {
      if (activeObject == null || !editable || !frameReadyRef.current) return;
      try {
        await api.post(`/api/mask-objects/${activeObject}/edit`, { frame_idx: frame, op, polygon });
        await Promise.all([loadMasks(), loadObjects()]);
      } catch (e) {
        setError((e as Error).message);
      }
    },
    [activeObject, frame, editable, loadMasks, loadObjects],
  );

  // ---------------------------------------------------------------- 工作区上下文
  const ws: WS | null = useMemo(
    () =>
      ann && payload
        ? {
            ann,
            update,
            editable: editable && displayReady,
            currentMs: frameMs,
            duration,
            segmentStart: segment?.start_ms, segmentEnd: segment?.end_ms,
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
    [ann, payload, update, editable, displayReady, frameMs, duration, seek, playRange, protocol, gold, selectedSample, assignmentId, activeAnchor, coreStart, coreEnd, segment],
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
          seek(frameMs - 5000, -5000);
          break;
        case "ArrowDown":
          seek(frameMs + 5000, 5000);
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
      if (canEditAssignment && (saveState !== "saved" || savingRef.current) && !(await save())) return;
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

  async function switchSegment(index: number) {
    if (switchingSegment || session?.status === "loading" || session?.status === "propagating") return;
    setSwitchingSegment(true);
    pausePlayback(); videoPreparation.cancel();
    try {
      if (canEditAssignment && (saveState !== "saved" || savingRef.current) && !(await save())) return;
      setSearchParams({ segment: String(index) });
      setSegmentAttempt(0);
      setCandidatePreview(null);
      setPlaybackFailure("");
    } finally { setSwitchingSegment(false); }
  }
  async function configureSegments() {
    const value = prompt("每段时长（秒，30–1800，默认 300）。调整后重置所有子任务完成状态，已有标注保留。", "300");
    if (value == null) return;
    const seconds = Number(value);
    if (!Number.isInteger(seconds) || seconds < 30 || seconds > 1800) { setError("请输入 30–1800 的整数秒数"); return; }
    setSwitchingSegment(true); pausePlayback(); videoPreparation.cancel();
    try {
      if (canEditAssignment && saveState !== "saved" && !(await save())) return;
      await api.put(`/api/cases/${encodeURIComponent(c?.case_id ?? "")}/segment-plan`, { seconds, context_seconds: 10 });
      setPayload(await api.get<AssignmentPayload>(`/api/assignments/${assignmentId}`));
      setSearchParams({ segment: "0" });
      window.location.reload();
    } catch(e) { setError((e as Error).message); }
    finally { setSwitchingSegment(false); }
  }
  async function completeSegment() {
    if (!segment || switchingSegment) return;
    setSwitchingSegment(true);
    pausePlayback();
    try {
      if (!(await save())) return;
      const p = await api.post<AssignmentPayload>(`/api/assignments/${assignmentId}/segments/${segment.index}/completion`, {
        completed: !segment.completed, version: versionRef.current, revision: mediaRevision,
      });
      setPayload(p);
    } catch(e) { setError((e as Error).message); }
    finally { setSwitchingSegment(false); }
  }

  if (error && !payload) return <div className="page error">{error}</div>;
  if (c && c.media_status !== "ready") return <VideoImportNotice video={c} />;
  if (c && !preparationReady(c)) return <VideoPreparationNotice video={c} canRetry={user.role !== "annotator"}
    onRetry={() => { setPayload(null); window.location.reload(); }} />;
  if (!payload || !ann || !ws || !c) return <div className="center muted">加载中…</div>;

  const a = payload.assignment;
  const errors = payload.issues.filter((i) => i.level === "error").length;

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
        {!canEditAssignment && <span className="badge">只读</span>}
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
        {user.role === "admin" && c.status !== "locked" && <button className="btn small" disabled={switchingSegment || creatingSession || session?.status === "loading" || session?.status === "propagating"} onClick={() => void configureSegments()}>调整视频分段</button>}
        <WorkflowButtons payload={payload} onAction={action} />
      </div>
      {error && (
        <div className="error banner" onClick={() => setError("")}>
          {error}（点击关闭）
        </div>
      )}
      {payload.rules_warning && <div className="notice banner">{payload.rules_warning}</div>}
      {payload.consensus && <ConsensusPanel key={a.id} payload={payload} onAction={action} />}
      {segment && <div className="subtask-bar">
        <b>视频子任务</b>
        <select aria-label="选择视频子任务" value={segment.index} disabled={switchingSegment || creatingSession || session?.status === "loading" || session?.status === "propagating"}
          onChange={e => void switchSegment(Number(e.target.value))}>
          {payload.segments.map((s,i) => <option key={s.index} value={s.index}>{i+1}/{payload.segments.length} · {fmtTime(s.start_ms)}–{fmtTime(s.end_ms)}{s.completed ? " ✓ 已完成" : " · 待完成"}</option>)}
        </select>
        <span className="small">已完成 {payload.segments.filter(s => s.completed).length}/{payload.segments.length} 段</span>
        {canEditAssignment && <button className="btn small" disabled={switchingSegment || saveState === "saving" || creatingSession || session?.status === "loading" || session?.status === "propagating"} onClick={() => void completeSegment()}>{segment.completed ? "重新打开本段" : "标记本段完成"}</button>}
        <span className="muted small">段外上下文只读 · 标注使用全局时间与帧号</span>
        {!inCore && <span className="badge">正在查看只读上下文</span>}
      </div>}
      <div className="ws-main">
        <div className="ws-left">
          {playbackFailure && <div className="error banner" role="alert">
            <span>{playbackFailure}</span>{" "}
            <button className="btn small" disabled={!playbackReady || playbackStarting} onClick={() => beginPlayback(frame)}>从当前帧重试播放</button>{" "}
            <button className="btn ghost small" onClick={() => setPlaybackFailure("")}>关闭提示</button>
          </div>}
          <VideoStage
            videoRef={videoRef}
            src={attachedVideoSource}
            videoW={c.width}
            videoH={c.height}
            imageSrc={stillFrame.url}
            frameReady={maskReady}
            showVideo={playing}
            showPausedVideo={showPausedVideo}
            playbackStarting={playbackStarting}
            frameError={stillFrame.error}
            onRetryFrame={stillFrame.retry}
            frameElapsedMs={stillFrame.elapsedMs}
            frameCached={stillFrame.cached}
            masks={maskReady && frameMasks?.identity === currentFrameIdentity ? frameMasks.masks : []}
            candidates={tab === "masks" && maskReady && candidatePreview?.sessionId === session?.id
              && candidatePreview?.frame === frame ? candidatePreview.masks : undefined}
            classColors={classColors}
            activeObject={activeObject}
            tool={editable && maskReady && gold && tab === "masks" ? tool : "none"}
            frame={frame}
            showMasks={gold && showMasks}
            onPrompt={onPrompt}
            onPolygon={onPolygon}
            onVideoEvent={syncFromVideo}
            onPlaybackError={message => {
              const shown = presentedVideoFrame.current;
              pausePlayback(shown?.identity === mediaIdentity ? shown.frame : undefined);
              setPlaybackFailure(message);
            }}
          />
          {segment && !playbackReady ? <div className="notice banner" role="status">
            {activeSegmentMedia?.status === "error" ? <>子视频生成失败：{activeSegmentMedia.error} <button className="btn small" onClick={() => setSegmentAttempt(n => n+1)}>重试</button></> : "正在读取服务器已准备的子视频…"}
          </div> : <VideoCacheNotice label={segment ? "本段视频" : undefined} {...wholeVideo} activePlayback={playing || playbackStarting} usingLocal={!!localVideoSource} />}
          <div className="controls">
            <button className="btn small" onClick={() => seekFrame(frame - 1)} title="上一帧 (←)">
              ◀︎|
            </button>
            <button className="btn small primary" onClick={togglePlay} disabled={!playbackReady} title="播放/暂停 (空格)">
              {playbackStarting ? "取消播放" : playing ? "暂停" : "播放"}
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
                ["segments", "分段"],
                ["samples", "采样点"],
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
            {tab === "segments" && <SegmentsPanel ws={ws} />}
            {tab === "samples" && <SamplePanel ws={ws} />}
            {tab === "events" && <EventsPanel ws={ws} />}
            {tab === "milestones" && <MilestonePanel ws={ws} selected={selectedMilestone} setSelected={setSelectedMilestone} />}
            {tab === "masks" && (
              <MaskPanel
                key={segmentKey}
                ws={ws}
                canVerify={inCore && (user.role === "admin" || user.role === "reviewer") && payload.case.status !== "locked"}
                frame={frame}
                frameReady={maskReady}
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
                pauseVideo={() => { pausePlayback(); setTool("none"); }}
                onCandidatePreview={showCandidates}
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
        rangeStart={segment?.context_start_ms} rangeEnd={segment?.context_end_ms} coreStart={segment?.start_ms} coreEnd={segment?.end_ms}
        currentMs={frameMs}
        protocol={protocol}
        gold={gold}
        editable={editable && displayReady}
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
