import { useCallback, useEffect, useState } from "react";
import { positionVideo, videoFrameTime } from "./videoPlayback";

/** Decode exact stills from the completed local file, retaining original pixels. */
export function useLocalVideoFrame(videoRef: React.RefObject<HTMLVideoElement | null>,
  source: string | null, frame: number, times: number[], enabled: boolean) {
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState<{ key: string; ready: boolean; error: string; elapsedMs: number } | null>(null);
  const key = `${source}:${frame}:${attempt}`;
  useEffect(() => {
    const video = videoRef.current;
    if (!enabled || !source || !video || !times.length) return;
    const controller = new AbortController();
    const start = performance.now();
    setState({ key, ready: false, error: "", elapsedMs: 0 });
    void positionVideo(video, videoFrameTime(times, frame), controller.signal).then(() => {
      if (!controller.signal.aborted) setState({ key, ready: true, error: "", elapsedMs: performance.now() - start });
    }).catch((error: Error) => {
      if (!controller.signal.aborted) setState({ key, ready: false, error: error.message, elapsedMs: 0 });
    });
    return () => controller.abort();
  }, [videoRef, source, frame, times, enabled, key]);
  const matched = enabled && !!source && state?.key === key;
  return { ready: !!(matched && state.ready), error: matched ? state.error : "",
    elapsedMs: matched ? state.elapsedMs : 0, retry: useCallback(() => setAttempt(n => n + 1), []) };
}
