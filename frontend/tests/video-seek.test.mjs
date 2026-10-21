import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import test from 'node:test';
import { build } from 'esbuild';
const built = await build({ entryPoints: [new URL('../src/lib/videoSeek.ts', import.meta.url).pathname], bundle: true, write: false, platform: 'node', format: 'cjs' });
const mod = { exports: {} };
new Function('require', 'module', 'exports', built.outputFiles[0].text)(createRequire(import.meta.url), mod, mod.exports);
const { VideoSeekQueue } = mod.exports;
function video(readyState = 4) {
  return { readyState, seeking: false, writes: [], set currentTime(t) { this.writes.push(t); this.seeking = true; } };
}
test('rapid scrubbing finishes the active seek then decodes only the latest target', () => {
  const queue = new VideoSeekQueue(), v = video();
  queue.request(v, 900);
  for (let t = 901; t <= 1900; t++) queue.request(v, t);
  assert.deepEqual(v.writes, [900]);
  v.seeking = false;
  assert.equal(queue.flush(v), true);
  assert.deepEqual(v.writes, [900, 1900]);
  v.seeking = false;
  assert.equal(queue.flush(v), false);
});
test('jumps requested before metadata survive until loadedmetadata', () => {
  const queue = new VideoSeekQueue(), v = video(0);
  queue.request(v, 900);
  queue.request(v, 1000);
  assert.deepEqual(v.writes, []);
  v.readyState = 1;
  queue.flush(v);
  assert.deepEqual(v.writes, [1000]);
});
test('changing assignments drops pending jumps from the previous video', () => {
  const queue = new VideoSeekQueue(), v = video(0);
  queue.request(v, 1900);
  queue.reset();
  v.readyState = 1;
  assert.equal(queue.flush(v), false);
  assert.deepEqual(v.writes, []);
});
