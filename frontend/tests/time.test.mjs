import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import test from 'node:test';
import { build } from 'esbuild';

const built = await build({ entryPoints: [new URL('../src/lib/time.ts', import.meta.url).pathname],
  bundle: true, write: false, platform: 'node', format: 'cjs' });
const mod = { exports: {} };
new Function('require', 'module', 'exports', built.outputFiles[0].text)(createRequire(import.meta.url), mod, mod.exports);
const { frameAt, presentedFrameAt } = mod.exports;

test('60fps presented timestamps match rounded map entries instead of the preceding frame', () => {
  const times = [0, 17, 33, 50, 67, 83, 100];
  times.forEach((_, frame) => assert.equal(presentedFrameAt(times, frame * 1000 / 60), frame));
  assert.equal(frameAt(times, 1000 / 60), 0, 'playback clock lookup retains its interval semantics');
  assert.equal(presentedFrameAt(times, 1000 / 60), 1);
});

test('29.97fps presentation lookup tolerates both directions of millisecond rounding', () => {
  const times = [0, 33, 67, 100, 133, 167, 200, 234];
  times.forEach((_, frame) => assert.equal(presentedFrameAt(times, frame * 1001 / 30), frame));
});

test('variable frame intervals and rounded half-millisecond timestamps select the displayed frame', () => {
  const times = [0, 12, 33, 117, 180, 301];
  const pts = [0, 12.5, 33.333333, 116.666667, 180.49, 300.51];
  pts.forEach((ms, frame) => assert.equal(presentedFrameAt(times, ms), frame));
});

test('presentation lookup clamps boundaries, handles empty maps, and resolves exact ties consistently', () => {
  assert.equal(presentedFrameAt([], 100), 0);
  assert.equal(presentedFrameAt([0], 0), 0);
  assert.equal(presentedFrameAt([0, 17, 33], -0.5), 0);
  assert.equal(presentedFrameAt([0, 17, 33], 33.333333), 2);
  assert.equal(presentedFrameAt([0, 17, 33], 1000), 2);
  assert.equal(presentedFrameAt([0, 100, 200], 50), 0);
  assert.equal(presentedFrameAt([0, 100, 200], 150), 1);
});
