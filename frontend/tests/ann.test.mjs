import assert from 'node:assert/strict';
import test from 'node:test';
import { normalizeAnchorSamples, normalizeSampleAnchors, copyApplicableAnchors, moveBoundary, removeBoundary, startSegment } from '../src/lib/ann.ts';

const settings = { anchor_applicability: { C1: ['lamina', 'facet', 'LF_window'], C4: ['lamina', 'facet', 'LF_window', 'dura', 'root'] } };
const annotation = () => ({
  evaluable: [{ start_ms: 0, label: 'E1' }],
  context: [{ start_ms: 0, label: 'C1' }, { start_ms: 10000, label: 'C4' }],
  samples: { '5000': { scs: '2', lamina: 'V', dura: 'NA', root: 'NA', note: '保留备注' }, '10000': { scs: '1', dura: 'V', root: 'N' } },
  events: [], milestones: {},
});

test('不适用锚点固定 NA，适用锚点清除自动 NA，保留 SCS 与备注', () => {
  const sample = { scs: '2', note: '备注', lamina: 'V', dura: 'V', root: 'U' };
  const c1 = normalizeSampleAnchors(sample, 'C1', settings);
  assert.equal(c1.dura, 'NA'); assert.equal(c1.root, 'NA');
  assert.equal(sample.dura, 'V');
  const c4 = normalizeSampleAnchors(c1, 'C4', settings);
  assert.equal(c4.dura, undefined); assert.equal(c4.root, undefined);
  assert.equal(c4.lamina, 'V'); assert.equal(c4.scs, '2'); assert.equal(c4.note, '备注');
});

test('新建情境、移动或删除边界时更新所有受影响采样点', () => {
  const ann = annotation();
  const changed = normalizeAnchorSamples({ ...ann, context: startSegment(ann.context, 5000, 'C4') }, settings);
  assert.equal(changed.samples['5000'].dura, undefined);
  assert.equal(changed.samples['5000'].note, '保留备注');
  assert.equal(changed.samples['10000'].dura, 'V');
  const moved = normalizeAnchorSamples({ ...ann, context: moveBoundary(ann.context, 1, 12000) }, settings);
  assert.equal(moved.samples['10000'].dura, 'NA');
  const removed = normalizeAnchorSamples({ ...ann, context: removeBoundary(ann.context, 1) }, settings);
  assert.equal(removed.samples['10000'].root, 'NA');
  assert.equal(ann.samples['10000'].root, 'N');
});

test('跨情境复制不将 NA 带入适用锚点，不将 V/N 带入不适用锚点', () => {
  const previous = { lamina: 'V', dura: 'NA', root: 'NA' };
  const c4 = copyApplicableAnchors({ scs: '1', note: '备注' }, previous, 'C4', settings);
  assert.equal(c4.lamina, 'V'); assert.equal(c4.dura, undefined);
  const c1 = copyApplicableAnchors({}, { dura: 'V', root: 'N' }, 'C1', settings);
  assert.equal(c1.dura, 'NA'); assert.equal(c1.root, 'NA');
  assert.equal(c4.scs, '1'); assert.equal(c4.note, '备注');
});

test('取消情境后清除锚点；没有变化时保持对象引用', () => {
  const sample = { scs: '2', lamina: 'V', dura: 'NA' };
  assert.deepEqual(normalizeSampleAnchors(sample, null, settings), { scs: '2' });
  const ann = normalizeAnchorSamples(annotation(), settings);
  assert.equal(normalizeAnchorSamples(ann, settings), ann);
});

test('NA 锁定遵循配置，不硬编码 C1', () => {
  const custom = { anchor_applicability: { C1: ['dura'] } };
  const result = normalizeSampleAnchors({ dura: 'NA', lamina: 'V' }, 'C1', custom);
  assert.equal(result.dura, undefined); assert.equal(result.lamina, 'NA');
});
