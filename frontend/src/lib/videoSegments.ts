import { labelAt, sortParts } from './ann';
import type { Annotation, Part, VideoSegment } from './types';

/** Keep edits inside the core while retaining one canonical event/state timeline. */
export function editSegment(before: Annotation, after: Annotation, s: VideoSegment, duration = Infinity): Annotation {
  const inside = (t: number | null | undefined) => t != null && t >= s.start_ms && t < s.end_ms;
  const parts = (key: 'context' | 'evaluable'): Part[] => {
    if (before[key] === after[key]) return before[key];
    return sortParts([
      ...before[key].filter(p => p.start_ms < s.start_ms),
      { start_ms: s.start_ms, label: labelAt(after[key], s.start_ms) },
      ...after[key].filter(p => p.start_ms > s.start_ms && p.start_ms < s.end_ms),
      ...(s.end_ms < duration ? [{ start_ms: s.end_ms, label: labelAt(before[key], s.end_ms) }] : []),
      ...before[key].filter(p => p.start_ms > s.end_ms),
    ]);
  };
  const samples = Object.fromEntries([
    ...Object.entries(before.samples).filter(([t]) => !inside(Number(t))),
    ...Object.entries(after.samples).filter(([t]) => inside(Number(t))),
  ]);
  const events = before.events.flatMap(old => {
    const next = after.events.find(e => e.id === old.id);
    if (!next) return inside(old.onset_ms) ? [] : [old];
    if ((old.onset_ms ?? 0) >= s.end_ms || (old.recovery_ms ?? Infinity) < s.start_ms) return [old];
    const result = { ...next };
    for (const key of ['onset_ms', 'offset_ms', 'recovery_ms'] as const) {
      if (next[key] !== old[key] && !(inside(next[key]) || next[key] == null && inside(old[key]))) Object.assign(result, { [key]: old[key] });
    }
    return [result];
  });
  events.push(...after.events.filter(e => !before.events.some(b => b.id === e.id) && inside(e.onset_ms)));
  const milestones = { ...before.milestones };
  for (const [key, value] of Object.entries(after.milestones)) {
    if (inside(value.time_ms) || value.time_ms == null && (before.milestones[key]?.time_ms == null || inside(before.milestones[key]?.time_ms))) milestones[key] = value;
  }
  return { ...before, context: parts('context'), evaluable: parts('evaluable'), samples, events, milestones };
}
