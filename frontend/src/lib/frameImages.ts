import { frameAt } from "./time";
import { FrameBlobCache, FrameRequestError } from "./frameBlobs";

export { FrameBlobCache, FrameRequestError } from "./frameBlobs";

export interface FrameImage {
  url: string;
  bytes: number;
  release: () => void;
}

export type FrameImageLoader = (frame: number, signal: AbortSignal, prefetch: boolean) => Promise<FrameImage>;
type Flight = { frame: number; controller: AbortController; promise: Promise<FrameImage>; promoted: boolean };
const cancelled = () => new DOMException("Frame request cancelled", "AbortError");

/** Decoded images have a much larger footprint than their compressed JPEGs. */
export class FrameImageCache {
  private entries = new Map<number, FrameImage>();
  private bytes = 0;
  private selected: number | null = null;
  private foreground: Flight | null = null;
  private background: Flight | null = null;
  private queue: number[] = [];
  private prefetchEnabled = true;

  constructor(private loader: FrameImageLoader, private maxEntries = 32, private maxBytes = 96 * 1024 * 1024) {}

  peek(frame: number): FrameImage | undefined { return this.entries.get(frame); }

  load(frame: number): Promise<FrameImage> {
    this.selected = frame;
    this.queue = [];
    if (this.foreground?.frame === frame) return this.foreground.promise;
    this.foreground?.controller.abort();
    this.foreground = null;
    if (this.background?.frame === frame) {
      // Keep the bytes already downloaded for exactly this target. The same
      // controller and promise now belong to the foreground selection.
      const flight = this.background;
      this.background = null;
      flight.promoted = true;
      this.foreground = flight;
      return flight.promise;
    }
    // Foreground requests must not wait behind a speculative HTTP request.
    this.background?.controller.abort();
    this.background = null;
    const entry = this.entries.get(frame);
    if (entry) {
      this.entries.delete(frame);
      this.entries.set(frame, entry);
      return Promise.resolve(entry);
    }
    const flight = this.start(frame, false);
    this.foreground = flight;
    return flight.promise;
  }

  prefetch(frames: number[]) {
    // Keep cached candidates in the plan so they count toward its budget and
    // can be promoted in the LRU before farther candidates displace them.
    this.queue = [...new Set(frames)].filter((f) => f !== this.selected);
    this.pump();
  }

  /** Suspend speculative traffic without disturbing the displayed/selected frame. */
  setPrefetchEnabled(enabled: boolean) {
    if (this.prefetchEnabled === enabled) return;
    this.prefetchEnabled = enabled;
    if (!enabled && this.background) {
      const flight = this.background;
      this.background = null;
      // Resume the interrupted candidate first unless a newer prefetch plan
      // replaces this queue while speculation is suspended.
      this.queue = [flight.frame, ...this.queue.filter((frame) => frame !== flight.frame)];
      flight.controller.abort();
    }
    if (enabled) this.pump();
  }

  pause() {
    this.queue = [];
    this.foreground?.controller.abort();
    this.background?.controller.abort();
    this.foreground = null;
    this.background = null;
  }

  clear() {
    this.pause();
    for (const entry of this.entries.values()) entry.release();
    this.entries.clear();
    this.bytes = 0;
    this.selected = null;
  }

  private start(frame: number, prefetch: boolean): Flight {
    const controller = new AbortController();
    const load = (speculative: boolean) => {
      try { return this.loader(frame, controller.signal, speculative); }
      catch (error) { return Promise.reject(error); }
    };
    const promise = load(prefetch).catch((error: unknown) => {
      // A low-priority request can be refused while the server is busy. Once
      // selected, retry through the foreground queue, at most once.
      if (prefetch && flight.promoted && this.foreground === flight && !controller.signal.aborted
        && error instanceof FrameRequestError && (error.status === 429 || error.status === 503))
        return load(false);
      throw error;
    }).then((entry) => {
      // Some decoders finish despite abort; never retain their stale results.
      if (controller.signal.aborted) { entry.release(); throw cancelled(); }
      const old = this.entries.get(frame);
      if (old) { this.bytes -= old.bytes; old.release(); this.entries.delete(frame); }
      this.entries.set(frame, entry);
      this.bytes += entry.bytes;
      for (const [key, value] of this.entries) {
        if (this.entries.size <= this.maxEntries && this.bytes <= this.maxBytes) break;
        // A single image larger than the budget is allowed while displayed.
        if (key === this.selected) continue;
        this.entries.delete(key);
        this.bytes -= value.bytes;
        value.release();
      }
      return entry;
    }).finally(() => {
      if (this.foreground === flight) {
        this.foreground = null;
        this.pump();
      } else if (this.background === flight) {
        this.background = null;
        this.pump();
      }
    });
    const flight: Flight = { frame, controller, promise, promoted: false };
    return flight;
  }

  private pump() {
    if (!this.prefetchEnabled || this.background || this.foreground) return;
    const selected = this.selected === null ? undefined : this.entries.get(this.selected);
    // Do not decode more predictions than can coexist with the displayed
    // image. Reserve one extra image of headroom for JPEG size differences.
    // This also prevents 4K prefetch from evicting its own priority targets.
    const capacity = selected && selected.bytes > 0
      ? Math.max(0, Math.min(this.maxEntries - 1, Math.floor(this.maxBytes / selected.bytes) - 2)) : 0;
    this.queue = this.queue.slice(0, capacity);
    for (const candidate of this.queue) {
      const entry = this.entries.get(candidate);
      if (entry) { this.entries.delete(candidate); this.entries.set(candidate, entry); }
    }
    const frame = this.queue.shift();
    if (frame === undefined) return;
    if (this.entries.has(frame)) { this.pump(); return; }
    const flight = this.start(frame, true);
    this.background = flight;
    void flight.promise.catch(() => { /* Speculation is best effort; foreground retries normally. */ });
  }
}

export function neighboringFrames(times: number[], frame: number, samplingInterval: number, preferSamples = false,
  relativeStepMs: number | null = null, limit = 6): number[] {
  if (!times.length) return [];
  const sample = Math.round(times[frame] / samplingInterval) * samplingInterval;
  const adjacent = [frame + 1, frame - 1, frame + 2, frame - 2];
  const samples = [frameAt(times, sample + samplingInterval), frameAt(times, Math.max(0, sample - samplingInterval))];
  // Arrow jumps are relative to the visible frame, which may be off the sample
  // grid. Predict with the same timestamp lookup used by the navigation action.
  const relative = relativeStepMs ? [frameAt(times, Math.max(0, times[frame] + relativeStepMs)),
    frameAt(times, Math.max(0, times[frame] - relativeStepMs))] : [];
  const fartherRelative = relativeStepMs ? [frameAt(times, Math.max(0, times[frame] + relativeStepMs * 2)),
    frameAt(times, Math.max(0, times[frame] - relativeStepMs * 2))] : [];
  const fartherSamples = [frameAt(times, sample + samplingInterval * 2),
    frameAt(times, Math.max(0, sample - samplingInterval * 2))];
  const priority = preferSamples ? [...samples, ...relative, ...adjacent] : [...relative, ...adjacent, ...samples];
  const count = Number.isFinite(limit) ? Math.max(0, Math.min(12, Math.floor(limit))) : 6;
  if (count <= 6) return [...new Set(priority)].filter((f) => f >= 0 && f < times.length && f !== frame).slice(0, count);
  return [...new Set([...priority, ...fartherRelative, ...fartherSamples, frame + 3, frame - 3, frame + 4, frame - 4])]
    .filter((f) => f >= 0 && f < times.length && f !== frame).slice(0, count);
}

export async function fetchFrameImage(url: string, signal: AbortSignal, prefetch: boolean): Promise<FrameImage> {
  return new FrameBlobCache(0, 0).load(0, url, signal, prefetch);
}
