import type { Annotation, AnchorField, OLEvent, Part, Sample, Settings } from "./types";

export const ANCHOR_FIELDS: AnchorField[] = ["lamina", "facet", "LF_window", "dura", "root"];

export function sortParts(parts: Part[]): Part[] {
  const s = [...parts].sort((a, b) => a.start_ms - b.start_ms);
  if (!s.length || s[0].start_ms !== 0) s.unshift({ start_ms: 0, label: null });
  return s;
}

export function partIndexAt(parts: Part[], t: number): number {
  let idx = 0;
  for (let i = 0; i < parts.length; i++) if (parts[i].start_ms <= t) idx = i;
  return idx;
}

export function labelAt(parts: Part[], t: number): string | null {
  return parts.length ? parts[partIndexAt(parts, t)].label : null;
}

/** 从 t 开始一段新标签（拆分当前段）；t 恰为已有分段起点时直接改标签。相邻同标签合并。 */
export function startSegment(parts: Part[], t: number, label: string): Part[] {
  const out = sortParts(parts).filter((p) => p.start_ms !== t);
  out.push({ start_ms: t, label });
  return mergeSame(sortParts(out));
}

export function setSegmentLabel(parts: Part[], idx: number, label: string | null): Part[] {
  const out = parts.map((p, i) => (i === idx ? { ...p, label } : p));
  return mergeSame(out);
}

export function removeBoundary(parts: Part[], idx: number): Part[] {
  if (idx === 0) return parts;
  return mergeSame(parts.filter((_, i) => i !== idx));
}

export function moveBoundary(parts: Part[], idx: number, t: number): Part[] {
  if (idx === 0) return parts;
  const lo = parts[idx - 1].start_ms + 1;
  const hi = idx + 1 < parts.length ? parts[idx + 1].start_ms - 1 : Infinity;
  const nt = Math.max(lo, Math.min(hi, Math.round(t)));
  return parts.map((p, i) => (i === idx ? { ...p, start_ms: nt } : p));
}

function mergeSame(parts: Part[]): Part[] {
  const out: Part[] = [];
  for (const p of parts) {
    if (out.length && out[out.length - 1].label === p.label) continue;
    out.push(p);
  }
  return out;
}

export function segmentEnd(parts: Part[], idx: number, duration: number): number {
  return idx + 1 < parts.length ? parts[idx + 1].start_ms : duration;
}

export function sampleTimes(duration: number, interval: number): number[] {
  const out: number[] = [];
  for (let t = 0; t < duration; t += interval) out.push(t);
  return out;
}

export function sampleComplete(s: Sample | undefined): boolean {
  return !!s && !!s.scs && ANCHOR_FIELDS.every((f) => !!s[f]);
}

/** 新采样点按情境预填 NA（不适用的锚点）。 */
export function defaultSample(ctx: string | null, settings: Settings): Sample {
  const s: Sample = {};
  const applic = ctx ? settings.anchor_applicability[ctx] : undefined;
  if (applic) for (const f of ANCHOR_FIELDS) if (!applic.includes(f)) s[f] = "NA";
  return s;
}

/** 适用锚点只能填 V/N/U；不适用锚点固定 NA。未选择情境时清除锚点值。 */
export function normalizeSampleAnchors(sample: Sample, ctx: string | null, settings: Settings): Sample {
  const applic = ctx ? settings.anchor_applicability[ctx] : undefined;
  const next = { ...sample };
  for (const f of ANCHOR_FIELDS) {
    if (!applic) delete next[f];
    else if (!applic.includes(f)) next[f] = "NA";
    else if (next[f] === "NA") delete next[f];
  }
  return ANCHOR_FIELDS.every((f) => next[f] === sample[f]) ? sample : next;
}

/** 所有编辑入口统一处理，包括改情境、移动/删除边界和恢复版本。 */
export function normalizeAnchorSamples(ann: Annotation, settings: Settings): Annotation {
  const samples = { ...ann.samples };
  let changed = false;
  for (const [key, sample] of Object.entries(samples)) {
    const next = normalizeSampleAnchors(sample, labelAt(ann.context, Number(key)), settings);
    if (next !== sample) {
      samples[key] = next;
      changed = true;
    }
  }
  return changed ? { ...ann, samples } : ann;
}

/** 跨情境复制时只复制仍适用的 V/N/U，不将上一情境的 NA 带入适用锚点。 */
export function copyApplicableAnchors(current: Sample, previous: Sample, ctx: string | null, settings: Settings): Sample {
  const next = { ...current };
  const applic = ctx ? settings.anchor_applicability[ctx] : undefined;
  for (const f of applic ?? []) {
    if (["V", "N", "U"].includes(previous[f] ?? "")) next[f] = previous[f];
  }
  return normalizeSampleAnchors(next, ctx, settings);
}

export function nextEventId(events: OLEvent[]): string {
  const n = events.reduce((m, e) => Math.max(m, parseInt(e.id.replace(/\D/g, ""), 10) || 0), 0);
  return `OL${String(n + 1).padStart(3, "0")}`;
}

export function sortEvents(events: OLEvent[]): OLEvent[] {
  return [...events].sort((a, b) => a.onset_ms - b.onset_ms);
}

export function mergeEvents(events: OLEvent[], ids: string[]): OLEvent[] {
  const sel = sortEvents(events.filter((e) => ids.includes(e.id)));
  if (sel.length < 2) return events;
  const first = sel[0];
  const last = sel[sel.length - 1];
  const rank = { high: 3, medium: 2, low: 1 } as Record<string, number>;
  const conf = sel.reduce<string | null>((c, e) => (!c || (e.confidence && rank[e.confidence] < rank[c]) ? e.confidence : c), null);
  const merged: OLEvent = {
    ...first,
    offset_ms: last.offset_ms,
    recovery_ms: last.recovery_ms,
    confidence: conf,
    note: [first.note, `合并自 ${sel.map((e) => e.id).join("+")}`].filter(Boolean).join("；"),
  };
  return sortEvents([...events.filter((e) => !ids.includes(e.id)), merged]);
}

export function emptyAnnotation(): Annotation {
  return { evaluable: [{ start_ms: 0, label: null }], context: [{ start_ms: 0, label: null }], samples: {}, events: [], milestones: {} };
}
