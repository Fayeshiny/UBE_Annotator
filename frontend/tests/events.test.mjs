import assert from 'node:assert/strict';
import test from 'node:test';
import { mergeEvents } from '../src/lib/ann.ts';

test('一键合并保留首次起点、末次结束与恢复点，原始事件不被直接修改', () => {
  const event = (id, onset_ms, offset_ms, recovery_ms, confidence = 'high') =>
    ({ id, onset_ms, offset_ms, recovery_ms, confidence, disturbance: 'none', note: '' });
  const events = [event('OL003', 20000, 26000, 28000), event('OL002', 8000, 14000, 16000, 'low'), event('OL001', 1000, 6000, 7000)];
  const before = structuredClone(events);
  const merged = mergeEvents(events, ['OL001', 'OL002']);
  assert.equal(merged.length, 2);
  assert.deepEqual(merged[0], { ...before[2], offset_ms: 14000, recovery_ms: 16000, confidence: 'low', note: '合并自 OL001+OL002' });
  assert.deepEqual(merged[1], before[0]);
  assert.deepEqual(events, before);
});
