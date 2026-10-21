import { VideoDownloadPlan, VIDEO_CHUNK_BYTES, type ByteRange } from "./videoDownloadPlan";
import { createVideoFileStorage, type VideoFileStorage } from "./videoFileStorage";

export type VideoCacheState = {
  status: "checking" | "downloading" | "paused" | "complete" | "error" | "cleared";
  loaded: number;
  total: number;
  url: string | null;
  error: string;
};

type Dependencies = {
  fetch: typeof fetch;
  storage: () => Promise<VideoFileStorage>;
  estimate: () => Promise<{ quota?: number; usage?: number }>;
  createURL: (blob: Blob) => string;
  revokeURL: (url: string) => void;
  unauthorized: () => void;
};
const defaults: Dependencies = {
  fetch: (...args) => fetch(...args),
  storage: createVideoFileStorage,
  estimate: () => navigator.storage?.estimate?.() ?? Promise.resolve({}),
  createURL: (blob) => URL.createObjectURL(blob),
  revokeURL: (url) => URL.revokeObjectURL(url),
  unauthorized: () => window.dispatchEvent(new Event("ssi-unauthorized")),
};
const cancelled = () => new DOMException("Video cache cancelled", "AbortError");
const CHUNK_BYTES = VIDEO_CHUNK_BYTES;

/** One workspace's complete original MP4, written to temporary browser storage.
 * A Blob URL is exposed only after every byte has been validated and committed.
 */
export class WholeVideoCache {
  private state: VideoCacheState = { status: "checking", loaded: 0, total: 0, url: null, error: "" };
  private disposed = false;
  private started = false;
  private paused = false;
  private request: AbortController | null = null;
  private resume: (() => void) | null = null;
  private store: VideoFileStorage | null = null;
  private dependencies: Dependencies;
  private plan: VideoDownloadPlan | null = null;
  private priorities: ByteRange[] = [];

  prioritize(ranges: ByteRange[]) {
    this.priorities = ranges;
    this.plan?.prioritize(ranges);
  }

  constructor(private source: string, private revision: string,
    private changed: (state: VideoCacheState) => void, dependencies: Partial<Dependencies> = {}) {
    this.dependencies = { ...defaults, ...dependencies };
  }

  setPaused(paused: boolean) {
    this.paused = paused;
    if (paused && this.state.status === "downloading") this.request?.abort();
    if (!paused) { this.resume?.(); this.resume = null; }
  }

  async start(): Promise<void> {
    if (this.started || this.disposed) return;
    this.started = true;
    try {
      const head = await this.fetch({ method: "HEAD" });
      this.checkResponse(head);
      const total = Number(head.headers.get("Content-Length"));
      if (!Number.isSafeInteger(total) || total <= 0 || head.headers.get("Accept-Ranges") !== "bytes")
        throw new Error("服务器暂不支持完整视频缓存，可继续在线播放。");
      this.verifyRevision(head);
      this.plan = new VideoDownloadPlan(total);
      this.plan.prioritize(this.priorities);
      this.emit({ total });
      const estimate = await this.dependencies.estimate().catch(() => ({} as { quota?: number; usage?: number }));
      this.current();
      if (estimate.quota !== undefined && estimate.usage !== undefined
        && estimate.quota - estimate.usage < total + 16 * 1024 * 1024)
        throw new Error("浏览器存储空间不足，已保留在线播放。");
      const store = await this.dependencies.storage();
      if (this.disposed) { await store.dispose(); throw cancelled(); }
      this.store = store;
      while (this.state.loaded < total) {
        await this.waitUntilResumed();
        this.current();
        const start = store.write ? this.plan.next() : this.state.loaded;
        const end = Math.min(start + CHUNK_BYTES, total) - 1;
        this.emit({ status: "downloading" });
        let response: Response;
        try { response = await this.fetch({ headers: { Range: `bytes=${start}-${end}` } }); }
        catch (error) {
          this.current();
          if (this.paused || (error instanceof DOMException && error.name === "AbortError")) continue;
          throw error;
        }
        this.checkResponse(response);
        this.verifyRevision(response);
        if (response.status !== 206 || response.headers.get("Content-Range") !== `bytes ${start}-${end}/${total}`) {
          await response.body?.cancel();
          throw new Error("视频分段校验失败，请刷新页面后重试缓存。");
        }
        let part: Blob;
        try { part = await response.blob(); }
        catch (error) {
          this.current();
          if (this.paused || (error instanceof DOMException && error.name === "AbortError")) continue;
          throw error;
        }
        this.current();
        if (this.paused) continue; // Discard an interrupted part; resume at its committed boundary.
        if (part.size !== end - start + 1) throw new Error("视频下载不完整，请重试缓存。");
        if (store.write) await store.write(start, part);
        else await store.append(part);
        this.plan.commit(start);
        this.current();
        this.emit({ loaded: this.state.loaded + part.size });
      }
      await this.waitUntilResumed();
      const file = await store.complete();
      this.current();
      if (file.size !== total) throw new Error("视频缓存大小不一致，请重试缓存。");
      const url = this.dependencies.createURL(file);
      this.emit({ status: "complete", url });
    } catch (error) {
      if (!this.disposed) {
        const message = error instanceof DOMException && error.name === "QuotaExceededError"
          ? "浏览器存储空间不足，已保留在线播放。"
          : error instanceof Error ? error.message : "视频缓存失败，可继续在线播放。";
        await this.store?.dispose().catch(() => {});
        this.store = null;
        if (!this.disposed) this.emit({ status: "error", error: message });
      }
    }
  }

  async dispose(): Promise<void> {
    if (this.disposed) return;
    this.disposed = true;
    this.request?.abort();
    this.resume?.();
    this.resume = null;
    if (this.state.url) this.dependencies.revokeURL(this.state.url);
    await this.store?.dispose().catch(() => {});
    this.store = null;
  }

  private emit(changes: Partial<VideoCacheState>) {
    this.current();
    this.state = { ...this.state, ...changes };
    this.changed(this.state);
  }

  private current() { if (this.disposed) throw cancelled(); }

  private async waitUntilResumed() {
    this.current();
    while (this.paused && !this.disposed) {
      this.emit({ status: "paused" });
      await new Promise<void>((resolve) => { this.resume = resolve; });
    }
    this.current();
  }

  private async fetch(init: RequestInit): Promise<Response> {
    this.current();
    const controller = new AbortController();
    this.request = controller;
    try {
      return await this.dependencies.fetch(this.source, {
        ...init, credentials: "same-origin", cache: "no-store", signal: controller.signal, priority: "low",
      } as RequestInit);
    } catch (error) {
      if (controller.signal.aborted) throw cancelled();
      throw error instanceof Error ? new Error("视频缓存下载失败，可继续在线播放，稍后重试缓存。") : error;
    }
  }

  private checkResponse(response: Response) {
    this.current();
    if (response.status === 401) this.dependencies.unauthorized();
    if (response.status === 409) throw new Error("视频已更新，请刷新页面后继续。");
    if (!response.ok) throw new Error(`视频缓存失败（${response.status}），可继续在线播放。`);
  }

  private verifyRevision(response: Response) {
    if (!this.revision || response.headers.get("X-Media-Revision") !== this.revision)
      throw new Error("视频版本已变化，请刷新页面后重试缓存。");
  }
}
