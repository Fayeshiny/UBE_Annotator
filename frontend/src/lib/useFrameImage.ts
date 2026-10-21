import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { FrameBlobCache, FrameImageCache, neighboringFrames } from "./frameImages";
import { fetchFrameChunk } from "./frameChunks";
import { frameCachePolicy } from "./frameCachePolicy";

type ImageState = { identity: string; frame: number; url: string | null; error: string; elapsedMs: number; cached: boolean };

export function useFrameImage(identity: string, caseId: string | undefined, revision: string, frame: number,
  times: number[], interval: number, enabled: boolean, preferSamples = false, relativeStepMs: number | null = null,
  prefetchEnabled = true, firstFrame = 0, lastFrame = times.length - 1) {
  const { cache, blobs, policy } = useMemo(() => {
    const policy = frameCachePolicy(typeof navigator === "undefined" ? undefined
      : (navigator as Navigator & { deviceMemory?: number }).deviceMemory);
    const blobs = new FrameBlobCache(policy.compressedBytes, policy.compressedEntries);
    const cache = new FrameImageCache((f, signal, prefetch) => blobs.load(f,
      `/api/cases/${encodeURIComponent(caseId ?? "")}/frame/${f}.jpg?revision=${encodeURIComponent(revision)}`, signal, prefetch),
      policy.decodedEntries, policy.decodedBytes);
    return { cache, blobs, policy };
  }, [identity, caseId, revision]);
  const [state, setState] = useState<ImageState | null>(null);
  const [chunkState, setChunkState] = useState<{ identity: string; frame: number; busy: boolean } | null>(null);
  const chunkRequest = useRef<AbortController | null>(null);
  const [retry, setRetry] = useState(0);
  const previous = useRef<{ identity: string; ms: number } | null>(null);
  useEffect(() => () => { cache.clear(); blobs.clear(); }, [cache, blobs]);
  // Playback preparation may retain the still image while using the network
  // for video. Changing speculation must not reset the foreground image state.
  useEffect(() => { cache.setPrefetchEnabled(prefetchEnabled); }, [cache, prefetchEnabled]);
  useEffect(() => {
    chunkRequest.current?.abort();
    chunkRequest.current = null;
    setChunkState(null);
    if (!enabled || !caseId || !times.length) { cache.pause(); return; }
    let disposed = false;
    let chunkTimer: ReturnType<typeof setTimeout> | undefined;
    // At a sample-sized jump, prepare the next sample before nearby frames.
    // The tolerance accounts for samples snapping to variable-rate timestamps.
    const sampleJump = previous.current?.identity === identity
      && Math.abs(times[frame] - previous.current.ms) >= interval * 0.9;
    previous.current = { identity, ms: times[frame] };
    const started = performance.now();
    const decoded = cache.peek(frame);
    const cached = !!decoded || blobs.has(frame);
    setState({ identity, frame, url: decoded?.url ?? null, error: "", elapsedMs: 0, cached });
    void cache.load(frame).then((entry) => {
      if (disposed) return;
      const elapsedMs = performance.now() - started;
      setState({ identity, frame, url: entry.url, error: "", elapsedMs, cached });
      performance.measure("ssi-frame-ready", { start: started, end: performance.now(), detail: { frame, cached } });
      // Bound User Timing storage in workspaces left open for many hours.
      if (performance.getEntriesByName("ssi-frame-ready").length > 200) performance.clearMeasures("ssi-frame-ready");
      const predictions = neighboringFrames(times, frame, interval, preferSamples || (!relativeStepMs && sampleJump),
        relativeStepMs, policy.prefetchFrames).filter(f => f >= firstFrame && f <= lastFrame);
      if (!prefetchEnabled) return;
      // Deliver the selected frame before any block fetch. A new foreground
      // selection aborts this low priority block rather than waiting for it.
      chunkTimer = setTimeout(() => {
        if (disposed) return;
        const start = Math.max(firstFrame, Math.floor(frame / 5) * 5);
        const count = Math.min(5, lastFrame - start + 1);
        const missing = Array.from({ length: count }, (_, i) => start + i)
          .some(f => f !== frame && !blobs.has(f));
        if (!missing) { cache.prefetch(predictions); return; }
        const controller = new AbortController();
        chunkRequest.current = controller;
        setChunkState({ identity, frame, busy: true });
        const url = `/api/cases/${encodeURIComponent(caseId)}/frame-chunk?start_frame=${start}&count=${count}&exclude_frame=${frame}&revision=${encodeURIComponent(revision)}`;
        void fetchFrameChunk(url, revision, start, count, frame, controller.signal, entry => {
          if (!disposed && !controller.signal.aborted) blobs.seed([entry]);
        }).catch(() => { /* Individual frame requests remain the fallback. */ }).finally(() => {
          if (!disposed && !controller.signal.aborted) {
            setChunkState({ identity, frame, busy: false });
            cache.prefetch(predictions);
          }
        });
      }, 200);
    }).catch((error: Error) => {
      if (!disposed && error.name !== "AbortError") setState({ identity, frame, url: null, error: error.message, elapsedMs: 0, cached: false });
    });
    // The next image load decides whether to promote its single-frame request.
    // Only this obsolete neighborhood stream is cancelled by cleanup.
    return () => { disposed = true; clearTimeout(chunkTimer); chunkRequest.current?.abort(); };
  }, [cache, blobs, policy, identity, caseId, frame, times, interval, enabled, preferSamples, relativeStepMs, retry, prefetchEnabled, firstFrame, lastFrame]);
  const matched = enabled && state?.identity === identity && state.frame === frame;
  return {
    url: matched ? state.url : null,
    ready: !!(matched && state.url),
    error: matched ? state.error : "",
    elapsedMs: matched ? state.elapsedMs : 0,
    cached: matched ? state.cached : false,
    prefetching: !!(enabled && chunkState?.identity === identity && chunkState.frame === frame && chunkState.busy),
    retry: useCallback(() => setRetry((n) => n + 1), []),
  };
}
