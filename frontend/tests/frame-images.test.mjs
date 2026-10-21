import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import test from 'node:test';
import { build } from 'esbuild';

const built = await build({ entryPoints: [new URL('../src/lib/frameImages.ts', import.meta.url).pathname],
  bundle: true, write: false, platform: 'node', format: 'cjs' });
const mod = { exports: {} };
new Function('require', 'module', 'exports', built.outputFiles[0].text)(createRequire(import.meta.url), mod, mod.exports);
const { FrameImageCache, FrameRequestError, neighboringFrames } = mod.exports;
const tick = () => new Promise(resolve => setImmediate(resolve));

function delayedCache(maxEntries = 32, maxBytes = 1000) {
  const requests = [], released = [];
  const loader = (frame, signal, prefetch) => new Promise((resolve, reject) => {
    requests.push({ frame, signal, prefetch, reject,
      resolve: (bytes = 40) => resolve({ url: `blob:${frame}`, bytes, release: () => released.push(frame) }) });
  });
  return { cache: new FrameImageCache(loader, maxEntries, maxBytes), requests, released };
}

test('late response from an aborted jump is disposed and never cached over the selected frame', async () => {
  const { cache, requests, released } = delayedCache();
  const older = cache.load(10);
  const rejected = assert.rejects(older, { name: 'AbortError' });
  const latest = cache.load(100);
  assert.equal(requests[0].signal.aborted, true);
  requests[1].resolve();
  assert.equal((await latest).url, 'blob:100');
  requests[0].resolve();
  await rejected;
  assert.equal(cache.peek(10), undefined);
  assert.equal(cache.peek(100).url, 'blob:100');
  assert.deepEqual(released, [10]);
  cache.clear();
});

test('selecting a pending neighbor promotes its existing download without restarting it', async () => {
  const { cache, requests } = delayedCache();
  const first = cache.load(10);
  requests[0].resolve();
  await first;
  cache.prefetch([11, 9, 12, 8]);
  assert.equal(requests.length, 2);
  assert.equal(requests[1].prefetch, true);
  requests[1].resolve();
  await tick();
  assert.equal(requests.length, 3);
  const selected = cache.load(9);
  assert.equal(requests[2].signal.aborted, false);
  assert.equal(requests.length, 3, 'reuse the existing low-priority transfer');
  assert.equal(cache.load(9), selected, 'same target shares one promise');
  cache.setPrefetchEnabled(false);
  assert.equal(requests[2].signal.aborted, false, 'promoted request is no longer speculative');
  requests[2].resolve();
  await selected;
  await tick();
  assert.equal(requests.length, 3, 'old prefetch plan cannot restart after selecting its target');
  cache.clear();
});

test('jumping elsewhere cancels a promoted request and discards its late decode', async () => {
  const { cache, requests, released } = delayedCache();
  const first = cache.load(10); requests[0].resolve(); await first;
  cache.prefetch([11, 12]);
  const promoted = cache.load(11);
  const cancelled = assert.rejects(promoted, { name: 'AbortError' });
  const selected = cache.load(100);
  assert.equal(requests[1].signal.aborted, true);
  requests[1].resolve(); await cancelled;
  assert.equal(cache.peek(11), undefined);
  assert.deepEqual(released, [11]);
  requests[2].resolve(); await selected;
  await tick();
  assert.equal(requests.length, 3);
  assert.equal(requests[2].prefetch, false);
  cache.clear();
});

test('a promoted busy prefetch retries once in the foreground', async () => {
  for (const status of [429, 503]) {
    const { cache, requests } = delayedCache();
    const first = cache.load(0); requests[0].resolve(); await first;
    cache.prefetch([1, 2]);
    const selected = cache.load(1);
    requests[1].reject(new FrameRequestError(status, 'busy'));
    await tick();
    assert.equal(requests.length, 3);
    assert.equal(requests[2].frame, 1);
    assert.equal(requests[2].prefetch, false);
    assert.equal(requests[2].signal, requests[1].signal);
    requests[2].resolve();
    assert.equal((await selected).url, 'blob:1');
    cache.clear();
  }
});

test('promotion never loops on repeated busy responses or retries permission/media errors', async () => {
  for (const status of [400, 401, 403, 409, 429]) {
    const { cache, requests } = delayedCache();
    const first = cache.load(0); requests[0].resolve(); await first;
    cache.prefetch([1]);
    const selected = cache.load(1);
    const rejected = assert.rejects(selected, { status });
    requests[1].reject(new FrameRequestError(status, 'refused'));
    if (status === 429) {
      await tick();
      requests[2].reject(new FrameRequestError(status, 'still busy'));
    }
    await rejected;
    assert.equal(requests.length, status === 429 ? 3 : 2);
    cache.clear();
  }
});

test('clear cancels a promoted flight; only the latest post-promotion prefetch plan can run', async () => {
  const { cache, requests } = delayedCache();
  const first = cache.load(0); requests[0].resolve(); await first;
  cache.prefetch([1, 2]);
  const selected = cache.load(1);
  cache.prefetch([100, 101]);
  requests[1].resolve(); await selected;
  assert.equal(requests[2].frame, 100);
  const promoted = cache.load(100);
  const cancelled = assert.rejects(promoted, { name: 'AbortError' });
  cache.clear();
  assert.equal(requests[2].signal.aborted, true);
  requests[2].resolve(); await cancelled; await tick();
  assert.equal(requests.length, 3);
  assert.equal(cache.peek(100), undefined);
});

test('suspending speculation preserves the foreground request and decoded image, then resumes queued neighbors', async () => {
  const { cache, requests, released } = delayedCache();
  const selected = cache.load(10);
  cache.setPrefetchEnabled(false);
  cache.prefetch([11, 12]);
  assert.equal(requests[0].signal.aborted, false);
  assert.equal(requests.length, 1);
  requests[0].resolve();
  assert.equal((await selected).url, 'blob:10');
  await tick();
  assert.equal(requests.length, 1, 'no speculation begins after foreground completion while suspended');
  assert.equal(cache.peek(10).url, 'blob:10');
  assert.deepEqual(released, []);
  cache.setPrefetchEnabled(true);
  assert.equal(requests[1].frame, 11);
  assert.equal(requests[1].prefetch, true);
  requests[1].resolve(); await tick();
  assert.equal(requests[2].frame, 12);
  requests[2].resolve(); await tick();
  cache.clear();
});

test('suspending an active prefetch aborts only speculation and ignores its late completion after resuming', async () => {
  const { cache, requests, released } = delayedCache();
  const selected = cache.load(10); requests[0].resolve(); await selected;
  cache.prefetch([11, 12]);
  cache.setPrefetchEnabled(false);
  assert.equal(requests[1].signal.aborted, true);
  assert.equal(cache.peek(10).url, 'blob:10');
  assert.deepEqual(released, []);
  cache.setPrefetchEnabled(true);
  assert.equal(requests[2].frame, 11, 'retry the interrupted candidate');
  requests[1].resolve(); await tick();
  assert.equal(cache.peek(11), undefined, 'late aborted decode is discarded');
  assert.equal(requests.length, 3, 'old completion cannot advance the resumed queue');
  requests[2].resolve(); await tick();
  assert.equal(requests[3].frame, 12);
  requests[3].resolve(); await tick();
  cache.clear();
});

test('resuming during a foreground load waits for it, using only the latest speculative plan', async () => {
  const { cache, requests } = delayedCache();
  cache.setPrefetchEnabled(false);
  const selected = cache.load(50);
  cache.prefetch([51, 49]);
  cache.prefetch([100, 0]);
  cache.setPrefetchEnabled(true);
  assert.equal(requests.length, 1, 'foreground retains priority when speculation is resumed');
  assert.equal(requests[0].signal.aborted, false);
  requests[0].resolve(); await selected;
  assert.equal(requests[1].frame, 100);
  requests[1].resolve(); await tick();
  assert.equal(requests[2].frame, 0);
  requests[2].resolve(); await tick();
  assert.deepEqual(requests.map(request => request.frame), [50, 100, 0]);
  cache.clear();
});

test('decoded byte budget evicts least recently used images, retaining the displayed image', async () => {
  const { cache, requests, released } = delayedCache(32, 100);
  for (const f of [1, 2]) { const loaded = cache.load(f); requests.at(-1).resolve(); await loaded; }
  await cache.load(1); // refresh LRU without another request
  const loaded = cache.load(3); requests.at(-1).resolve(); await loaded;
  assert.equal(requests.length, 3);
  assert.equal(cache.peek(2), undefined);
  assert.deepEqual(released, [2]);
  assert.ok(cache.peek(1));
  assert.ok(cache.peek(3));
  cache.clear();
  assert.deepEqual(released.sort(), [1, 2, 3]);
});

test('entry count also caps memory for tiny images and one oversized current frame is usable', async () => {
  const { cache, requests } = delayedCache(2, 1000);
  for (const f of [1, 2, 3]) { const loaded = cache.load(f); requests.at(-1).resolve(1); await loaded; }
  assert.equal(cache.peek(1), undefined);
  const loaded = cache.load(4); requests.at(-1).resolve(2000); await loaded;
  assert.equal(cache.peek(2), undefined);
  assert.equal(cache.peek(3), undefined);
  assert.ok(cache.peek(4));
  cache.clear();
});

test('assignment switch or logout clears URLs and aborts outstanding decoded images', async () => {
  const { cache, requests, released } = delayedCache();
  const first = cache.load(0); requests[0].resolve(); await first;
  cache.prefetch([1]);
  cache.clear();
  assert.equal(requests[1].signal.aborted, true);
  assert.equal(cache.peek(0), undefined);
  requests[1].resolve();
  await tick();
  assert.deepEqual(released, [0, 1]);
  // React StrictMode may reuse the instance after effect cleanup.
  const again = cache.load(0); requests[2].resolve(); await again;
  assert.ok(cache.peek(0));
  cache.clear();
});

test('prefetch uses exact variable-rate timestamps for sample neighbors and clamps boundaries', () => {
  const times = [0, 14, 48, 125, 160, 280, 305, 399, 500];
  assert.deepEqual(neighboringFrames(times, 4, 200), [5, 3, 6, 2, 7, 0]);
  assert.deepEqual(neighboringFrames(times, 0, 200), [1, 2, 4]);
  assert.ok(neighboringFrames(times, 8, 200).every(f => f >= 0 && f < times.length && f !== 8));
});

test('sampling workflow prepares next sample before spending requests on adjacent frames', () => {
  const times = Array.from({ length: 900 }, (_, frame) => frame * 1000 / 30);
  assert.deepEqual(neighboringFrames(times, 150, 5000, true), [300, 0, 151, 149, 152, 148]);
  assert.deepEqual(neighboringFrames(times, 150, 5000, false), [151, 149, 152, 148, 300, 0]);
});

test('relative five-second jumps predict the off-grid target in navigation direction with at most six requests', () => {
  const times = Array.from({ length: 1500 }, (_, frame) => frame * 20);
  assert.deepEqual(neighboringFrames(times, 255, 5000, false, 5000), [505, 5, 256, 254, 257, 253]);
  assert.deepEqual(neighboringFrames(times, 255, 5000, false, -5000), [5, 505, 256, 254, 257, 253]);
  // In the sampling panel canonical sample targets retain first priority.
  assert.deepEqual(neighboringFrames(times, 255, 5000, true, 5000), [500, 0, 505, 5, 256, 254]);
  const variable = [0, 200, 4990, 5051, 5100, 9998, 10082, 10101, 15080];
  assert.deepEqual(neighboringFrames(variable, 4, 5000, false, 5000).slice(0, 2), [6, 0]);
});

test('expanded predictions preserve previous priorities and add bounded unique farther targets', () => {
  const times = Array.from({ length: 1500 }, (_, frame) => frame * 20);
  for (const frame of [0, 255, 750, 1499]) {
    for (const preferSamples of [false, true]) {
      for (const relativeStep of [null, -5000, 5000]) {
        const original = neighboringFrames(times, frame, 5000, preferSamples, relativeStep);
        const expanded = neighboringFrames(times, frame, 5000, preferSamples, relativeStep, 12);
        assert.deepEqual(expanded.slice(0, original.length), original);
        assert.ok(expanded.length <= 12);
        assert.equal(new Set(expanded).size, expanded.length);
        assert.ok(expanded.every(f => f >= 0 && f < times.length && f !== frame));
      }
    }
  }
  const expanded = neighboringFrames(times, 750, 5000, false, 5000, 12);
  assert.ok(expanded.includes(1250) && expanded.includes(250), 'prepares two subsequent relative jumps');
  assert.equal(expanded.length, 12);
});

test('high resolution prefetch shrinks to the byte budget without evicting its first priority target', async () => {
  const MiB = 1024 * 1024;
  for (const [budget, bytes, expected] of [[256, 8.2, 12], [256, 32.2, 5], [96, 32.2, 0]]) {
    const { cache, requests } = delayedCache(128, budget * MiB);
    const first = cache.load(0); requests[0].resolve(bytes * MiB); await first;
    cache.prefetch(Array.from({ length: 12 }, (_, i) => i + 1));
    for (let i = 1; i <= expected; i++) {
      assert.equal(requests[i].frame, i);
      requests[i].resolve(bytes * MiB); await tick();
    }
    assert.equal(requests.length, expected + 1);
    assert.ok(cache.peek(0));
    if (expected) assert.ok(cache.peek(1), 'later speculation cannot evict its own highest priority target');
    cache.clear();
  }
});

test('cached priority targets count toward the prefetch plan and survive adding farther frames', async () => {
  const { cache, requests } = delayedCache(6, 240);
  for (const frame of [1, 10, 11, 12, 13, 0]) {
    const loaded = cache.load(frame); requests.at(-1).resolve(); await loaded;
  }
  cache.prefetch([1, 2, 3, 4, 5, 6]); // Only four candidates fit, including cached frame 1.
  for (let frame = 2; frame <= 4; frame++) {
    assert.equal(requests.at(-1).frame, frame);
    requests.at(-1).resolve(); await tick();
  }
  assert.equal(requests.length, 9);
  assert.ok(cache.peek(0));
  assert.ok(cache.peek(1), 'the oldest cached priority frame was refreshed before farther speculation');
  assert.equal(cache.peek(5), undefined);
  cache.clear();
});
