import { useCallback, useEffect, useRef, useState } from "react";
import { rangesForFrames, type VideoByteIndex } from "./videoDownloadPlan";
import { WholeVideoCache, type VideoCacheState } from "./wholeVideoCache";

const initial: VideoCacheState = { status: "checking", loaded: 0, total: 0, url: null, error: "" };

export function useWholeVideoCache(identity: string, source: string, revision: string, enabled: boolean, busy: boolean, priorityFrames: number[] = []) {
  const [attempt, setAttempt] = useState(0);
  const [manualPaused, setManualPaused] = useState(false);
  const [foregroundBusy, setForegroundBusy] = useState(true);
  const [stopped, setStopped] = useState(false);
  const [hidden, setHidden] = useState(document.visibilityState !== "visible");
  const [state, setState] = useState<{ key: string; value: VideoCacheState } | null>(null);
  const [index, setIndex] = useState<{ source: string; ranges: VideoByteIndex } | null>(null);
  const cache = useRef<WholeVideoCache | null>(null);
  const paused = manualPaused || busy || hidden || foregroundBusy;
  const effectivePaused = paused || index?.source !== source;
  const pausedRef = useRef(effectivePaused);
  pausedRef.current = effectivePaused;
  const key = `${identity}:${attempt}:${stopped}`;

  useEffect(() => { setStopped(false); setManualPaused(false); }, [identity]);
  useEffect(() => {
    const changed = () => setHidden(document.visibilityState !== "visible");
    document.addEventListener("visibilitychange", changed);
    return () => document.removeEventListener("visibilitychange", changed);
  }, []);
  useEffect(() => {
    if (!enabled || stopped) return;
    let disposed = false;
    const current = new WholeVideoCache(source, revision, (value) => {
      if (!disposed) setState({ key, value });
    });
    cache.current = current;
    current.setPaused(pausedRef.current);
    void current.start();
    return () => {
      disposed = true;
      if (cache.current === current) cache.current = null;
      void current.dispose();
    };
  }, [identity, source, revision, enabled, stopped, attempt, key]);
  useEffect(() => { cache.current?.setPaused(effectivePaused); }, [effectivePaused]);
  useEffect(() => {
    if (!enabled || stopped || paused || index?.source === source) return;
    const controller = new AbortController();
    void fetch(source.replace("/video?", "/video-index?"), {
      credentials: "same-origin", signal: controller.signal, priority: "low",
    } as RequestInit).then(async response => {
      if (!response.ok || response.headers.get("X-Media-Revision") !== revision) {
        if (!controller.signal.aborted) setIndex({ source, ranges: [] });
        return;
      }
      const data = await response.json();
      if (!controller.signal.aborted && data.revision === revision && Array.isArray(data.ranges)
        && data.ranges.every((r: unknown) => Array.isArray(r) && r.length === 4 && r.every(Number.isSafeInteger)))
        setIndex({ source, ranges: data.ranges });
      else if (!controller.signal.aborted) setIndex({ source, ranges: [] });
    }).catch(() => { if (!controller.signal.aborted) setIndex({ source, ranges: [] }); });
    return () => controller.abort();
  }, [source, revision, enabled, stopped, paused, index]);
  useEffect(() => {
    cache.current?.prioritize(index?.source === source ? rangesForFrames(index.ranges, priorityFrames) : []);
  }, [source, index, priorityFrames, attempt, stopped, enabled]);
  return {
    ...(stopped ? { ...initial, status: "cleared" as const } : state?.key === key ? state.value : initial),
    manualPaused,
    pauseReason: manualPaused ? "manual" : busy ? "playback" : hidden ? "hidden" : foregroundBusy ? "frame" : "index",
    // Abort synchronously in the navigation handler, before the frame's effect
    // starts fetching. React state alone would leave a competing range active.
    holdForFrame: useCallback(() => {
      cache.current?.setPaused(true);
      setForegroundBusy(true);
    }, []),
    releaseForFrame: useCallback(() => setForegroundBusy(false), []),
    togglePause: () => setManualPaused((value) => !value),
    clear: () => setStopped(true),
    retry: () => { setManualPaused(false); setStopped(false); setAttempt((value) => value + 1); },
  };
}
