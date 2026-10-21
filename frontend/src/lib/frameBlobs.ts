import type { FrameImage } from "./frameImages";

const cancelled = () => new DOMException("Frame request cancelled", "AbortError");

/** Preserve HTTP status so a promoted speculative request can retry as foreground. */
export class FrameRequestError extends Error {
  constructor(public readonly status: number, message: string) {
    super(message);
    this.name = "FrameRequestError";
  }
}

function abortable<T>(pending: Promise<T>, signal: AbortSignal): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const onAbort = () => reject(cancelled());
    // Attach to the underlying promise even when already cancelled, so a late
    // decoder/fetch rejection cannot become an unhandled rejection.
    pending.then((value) => {
      signal.removeEventListener("abort", onAbort);
      if (signal.aborted) reject(cancelled());
      else resolve(value);
    }, (error) => {
      signal.removeEventListener("abort", onAbort);
      reject(signal.aborted ? cancelled() : error);
    });
    if (signal.aborted) onAbort();
    else signal.addEventListener("abort", onAbort, { once: true });
  });
}

/** A compressed tier independent from the much smaller decoded-image cache.
 * Each instance belongs to one case/media revision and stores original JPEGs.
 * It never retains object URLs or Image elements after their caller releases them.
 */
export class FrameBlobCache {
  private entries = new Map<number, Blob>();
  private bytes = 0;
  private generation = 0;
  private active = new Set<AbortController>();

  constructor(private maxBytes = 64 * 1024 * 1024, private maxEntries = 1024) {}

  seed(entries: [number, Blob][]): void {
    for (const [frame, blob] of entries) this.retain(frame, blob);
  }

  has(frame: number): boolean { return this.entries.has(frame); }

  clear(): void {
    this.generation += 1;
    for (const controller of this.active) controller.abort();
    this.active.clear();
    this.entries.clear();
    this.bytes = 0;
  }

  async load(frame: number, url: string, signal: AbortSignal, prefetch: boolean): Promise<FrameImage> {
    if (signal.aborted) throw cancelled();
    const generation = this.generation;
    const controller = new AbortController();
    const onAbort = () => controller.abort();
    signal.addEventListener("abort", onAbort, { once: true });
    this.active.add(controller);
    const current = () => {
      if (controller.signal.aborted || this.generation !== generation) throw cancelled();
    };
    let blob: Blob | undefined;
    try {
      blob = this.entries.get(frame);
      if (blob) {
        this.entries.delete(frame);
        this.entries.set(frame, blob);
      } else {
        let response: Response;
        try {
          response = await abortable(fetch(`${url}${prefetch ? `${url.includes("?") ? "&" : "?"}prefetch=true` : ""}`, {
            credentials: "same-origin", signal: controller.signal, priority: prefetch ? "low" : "high",
          } as RequestInit), controller.signal);
        } catch {
          current();
          throw new Error("画面下载失败，请检查网络后重试。");
        }
        current();
        if (!response.ok) {
          if (response.status === 401) window.dispatchEvent(new Event("ssi-unauthorized"));
          const message = response.status === 409 ? "视频已更新，请刷新页面后继续标注。"
            : response.status === 403 ? "没有查看此帧的权限，请联系管理员。"
              : `画面加载失败（${response.status}），请重试。`;
          throw new FrameRequestError(response.status, message);
        }
        blob = await abortable(response.blob(), controller.signal);
        current();
      }
      const decoded = await this.decode(blob, controller.signal);
      try { current(); }
      catch (error) { decoded.release(); throw error; }
      this.retain(frame, blob);
      return decoded;
    } catch (error) {
      // A bad cached JPEG must be refetched on retry. Cancellation leaves a
      // valid existing Blob available for later navigation.
      if (!controller.signal.aborted && blob && this.entries.get(frame) === blob) {
        this.entries.delete(frame);
        this.bytes -= blob.size;
      }
      throw error;
    } finally {
      signal.removeEventListener("abort", onAbort);
      this.active.delete(controller);
    }
  }

  private retain(frame: number, blob: Blob): void {
    const old = this.entries.get(frame);
    if (old) { this.entries.delete(frame); this.bytes -= old.size; }
    // One oversized JPEG remains usable by the caller without displacing the
    // entire compressed cache or exceeding its budget.
    if (blob.size > this.maxBytes || this.maxEntries < 1) return;
    this.entries.set(frame, blob);
    this.bytes += blob.size;
    for (const [key, entry] of this.entries) {
      if (this.entries.size <= this.maxEntries && this.bytes <= this.maxBytes) break;
      this.entries.delete(key);
      this.bytes -= entry.size;
    }
  }

  private async decode(blob: Blob, signal: AbortSignal): Promise<FrameImage> {
    const image = new Image();
    const objectUrl = URL.createObjectURL(blob);
    let released = false;
    const release = () => {
      if (released) return;
      released = true;
      image.src = "";
      URL.revokeObjectURL(objectUrl);
    };
    try {
      image.src = objectUrl;
      await abortable(image.decode(), signal);
      if (signal.aborted) throw cancelled();
      // The decoded URL also pins its source Blob, even if the compressed LRU
      // later evicts it. Count that source conservatively in both budgets.
      return { url: objectUrl, bytes: image.naturalWidth * image.naturalHeight * 4 + blob.size, release };
    } catch {
      release();
      if (signal.aborted) throw cancelled();
      throw new Error("画面解码失败，请重试加载；若仍失败，请联系管理员。");
    }
  }
}
