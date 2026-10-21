import { VideoSeekQueue } from "./videoSeek";

export function videoFrameTime(times: number[], frame: number): number {
  const start = times[frame] ?? 0;
  const next = times[frame + 1] ?? start + (frame > 0 ? start - times[frame - 1] : 33);
  return (start + (next - start) / 2) / 1000;
}

/** Wait for the target decoded frame before starting playback or exposing video. */
export async function startVideoPlayback(video: HTMLVideoElement, seconds: number, signal: AbortSignal,
  onPresented?: (mediaTimeSeconds: number) => void): Promise<void> {
  if (signal.aborted) throw new DOMException("Playback cancelled", "AbortError");
  await positionVideo(video, seconds, signal);
  await playPositionedVideo(video, signal, onPresented);
}

type PreparedVideo = {
  video: HTMLVideoElement;
  source: string;
  seconds: number;
  controller: AbortController;
  promise: Promise<void>;
  state: "positioning" | "positioned" | "playing";
};

/** Share idle positioning with a subsequent Play action without starting playback. */
export class VideoPreparation {
  private current: PreparedVideo | null = null;

  prepare(video: HTMLVideoElement, seconds: number): Promise<void> {
    return this.entry(video, seconds).promise;
  }

  async start(video: HTMLVideoElement, seconds: number, signal: AbortSignal,
    onPresented?: (mediaTimeSeconds: number) => void): Promise<void> {
    if (signal.aborted) throw new DOMException("Playback cancelled", "AbortError");
    const entry = this.entry(video, seconds);
    // Cancel this operation only: a late abort from an obsolete Play action must
    // never cancel a newer target owned by the same preparation manager.
    const abort = () => entry.controller.abort();
    signal.addEventListener("abort", abort, { once: true });
    try {
      await entry.promise;
      if (entry.controller.signal.aborted || this.current !== entry) {
        throw new DOMException("Playback cancelled", "AbortError");
      }
      entry.state = "playing";
      await playPositionedVideo(video, entry.controller.signal, onPresented);
    } finally {
      signal.removeEventListener("abort", abort);
    }
  }

  cancel(): void {
    const entry = this.current;
    this.current = null;
    entry?.controller.abort();
  }

  private entry(video: HTMLVideoElement, seconds: number): PreparedVideo {
    // The src attribute changes before currentSrc when the same element is
    // assigned a new resource, so prefer it when present.
    const source = video.src || video.currentSrc;
    const existing = this.current;
    if (existing && !existing.controller.signal.aborted && existing.video === video
      && existing.source === source && existing.seconds === seconds
      && (existing.state === "positioning" || (existing.state === "positioned"
        && video.paused && !video.seeking && video.readyState >= 2
        && Math.abs(video.currentTime - seconds) < 0.0001))) {
      return existing;
    }
    this.cancel();
    const entry: PreparedVideo = {
      video, source, seconds, controller: new AbortController(),
      promise: Promise.resolve(), state: "positioning",
    };
    this.current = entry;
    entry.promise = positionVideo(video, seconds, entry.controller.signal).then(() => {
      if (entry.controller.signal.aborted) throw new DOMException("Playback cancelled", "AbortError");
      entry.state = "positioned";
    }).catch((error: unknown) => {
      if (this.current === entry) this.current = null;
      throw error;
    });
    return entry;
  }
}

async function playPositionedVideo(video: HTMLVideoElement, signal: AbortSignal,
  onPresented?: (mediaTimeSeconds: number) => void): Promise<void> {
  if (signal.aborted) throw new DOMException("Playback cancelled", "AbortError");
  const abort = () => video.pause();
  signal.addEventListener("abort", abort, { once: true });
  try {
    // Chrome can mark a seek into the final frame as ended even before duration.
    // Calling play() then silently rewinds to frame zero. Keep the requested
    // final still; the workspace returns to paused mode for this handoff.
    if (video.ended) { video.pause(); return; }
    await video.play();
    if (signal.aborted) throw new DOMException("Playback cancelled", "AbortError");
    // The seek has decoded the requested frame already. Reveal on the next
    // presentation callback so a stale pre-seek video surface cannot flash.
    await new Promise<void>((resolve, reject) => {
      let handle = 0;
      const rvfc = typeof video.requestVideoFrameCallback === "function";
      const cleanup = () => {
        clearTimeout(timeout);
        signal.removeEventListener("abort", cancel);
        video.removeEventListener("ended", shown);
        video.removeEventListener("error", failed);
        if (rvfc) video.cancelVideoFrameCallback(handle);
        else cancelAnimationFrame(handle);
      };
      const cancel = () => { cleanup(); reject(new DOMException("Playback cancelled", "AbortError")); };
      const shown = (_?: number | Event, metadata?: { mediaTime: number }) => {
        if (signal.aborted) { cancel(); return; }
        cleanup();
        if (metadata) onPresented?.(metadata.mediaTime);
        resolve();
      };
      const failed = () => { cleanup(); reject(new Error("视频播放加载失败，请重试。")); };
      const timeout = setTimeout(() => { cleanup(); reject(new Error("视频播放准备超时，请重试。")); }, 10000);
      signal.addEventListener("abort", cancel, { once: true });
      video.addEventListener("ended", shown, { once: true });
      video.addEventListener("error", failed, { once: true });
      if (signal.aborted) { cancel(); return; }
      if (video.ended) { shown(); return; }
      handle = rvfc ? video.requestVideoFrameCallback(shown) : requestAnimationFrame(shown);
    });
  } finally { signal.removeEventListener("abort", abort); }
}

export function positionVideo(video: HTMLVideoElement, seconds: number, signal: AbortSignal): Promise<void> {
  if (signal.aborted) return Promise.reject(new DOMException("Playback cancelled", "AbortError"));
  return new Promise((resolve, reject) => {
    const queue = new VideoSeekQueue();
    const events = ["loadedmetadata", "loadeddata", "canplay", "seeked"];
    const cleanup = () => {
      clearTimeout(timeout);
      events.forEach((name) => video.removeEventListener(name, check));
      video.removeEventListener("error", failed);
      signal.removeEventListener("abort", cancel);
      queue.reset();
    };
    const cancel = () => { cleanup(); reject(new DOMException("Playback cancelled", "AbortError")); };
    const failed = () => { cleanup(); reject(new Error("视频播放加载失败，请重试。")); };
    const check = () => {
      if (signal.aborted) { cancel(); return; }
      if (queue.flush(video) || video.seeking || video.readyState < 2) return;
      cleanup();
      resolve();
    };
    const timeout = setTimeout(() => { cleanup(); reject(new Error("视频定位超时，请检查网络并重试。")); }, 30000);
    events.forEach((name) => video.addEventListener(name, check));
    video.addEventListener("error", failed, { once: true });
    signal.addEventListener("abort", cancel, { once: true });
    queue.request(video, seconds);
    check();
  });
}
