export const VIDEO_CHUNK_BYTES = 4 * 1024 * 1024;
export type ByteRange = [number, number];
export type VideoByteIndex = [number, number, number, number][];

/** Distinct, fixed byte blocks may arrive in any order; holes are never published. */
export class VideoDownloadPlan {
  private committed = new Set<number>();
  private priority: number[] = [];
  private cursor = 0;
  constructor(private total: number) {}
  prioritize(ranges: ByteRange[]) {
    this.priority = [];
    for (const [first, last] of ranges) {
      if (!Number.isSafeInteger(first) || !Number.isSafeInteger(last) || first < 0 || last < first || first >= this.total) continue;
      for (let start = Math.floor(first / VIDEO_CHUNK_BYTES) * VIDEO_CHUNK_BYTES;
        start <= Math.min(last, this.total - 1); start += VIDEO_CHUNK_BYTES) {
        if (!this.priority.includes(start)) this.priority.push(start);
      }
    }
  }
  next(): number {
    for (const start of this.priority) if (!this.committed.has(start)) return start;
    while (this.committed.has(this.cursor)) this.cursor += VIDEO_CHUNK_BYTES;
    return this.cursor;
  }
  commit(start: number) { this.committed.add(start); }
}

/** Actual packet byte bounds, never a duration/bitrate estimate. */
export function rangesForFrames(index: VideoByteIndex, frames: number[]): ByteRange[] {
  const ranges: ByteRange[] = [];
  for (const frame of frames) {
    for (const [first, last, start, end] of index) {
      if (first <= frame && frame <= last && !ranges.some(r => r[0] === start && r[1] === end)) ranges.push([start, end]);
    }
  }
  return ranges;
}
