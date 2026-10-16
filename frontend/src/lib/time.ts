export function fmtTime(ms: number | null | undefined, withMs = true): string {
  if (ms === null || ms === undefined || Number.isNaN(ms)) return "—";
  const neg = ms < 0;
  const t = Math.abs(ms);
  const h = Math.floor(t / 3600000);
  const m = Math.floor((t % 3600000) / 60000);
  const s = Math.floor((t % 60000) / 1000);
  const r = Math.round(t % 1000);
  const base = `${h > 0 ? `${h}:` : ""}${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return (neg ? "-" : "") + (withMs ? `${base}.${String(r).padStart(3, "0")}` : base);
}

export function fmtDur(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "—";
  return `${(ms / 1000).toFixed(1)}s`;
}

export function fmtPct(x: number | null | undefined): string {
  return x === null || x === undefined ? "—" : `${(x * 100).toFixed(1)}%`;
}

export function fmtNum(x: number | null | undefined, digits = 2): string {
  return x === null || x === undefined ? "—" : x.toFixed(digits);
}

/** 最后一个 times[i] <= t 的帧号 */
export function frameAt(times: number[], t: number): number {
  let lo = 0;
  let hi = times.length - 1;
  if (hi < 0) return 0;
  if (t <= times[0]) return 0;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (times[mid] <= t) lo = mid;
    else hi = mid - 1;
  }
  return lo;
}
