import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import test from 'node:test';
import { build } from 'esbuild';

const built = await build({ entryPoints: [new URL('../src/lib/videoPlayback.ts', import.meta.url).pathname],
  bundle: true, write: false, platform: 'node', format: 'cjs' });
const mod = { exports: {} };
new Function('require', 'module', 'exports', built.outputFiles[0].text)(createRequire(import.meta.url), mod, mod.exports);
const { startVideoPlayback, positionVideo, videoFrameTime, VideoPreparation } = mod.exports;
const tick = () => new Promise(resolve => setImmediate(resolve));

class Video extends EventTarget {
  readyState = 0; seeking = false; paused = true; ended = false;
  writes = []; plays = 0; pauses = 0; seconds = 0; callbacks = new Map();
  get currentTime() { return this.seconds; }
  set currentTime(value) { this.seconds = value; this.writes.push(value); this.seeking = true; }
  play() { this.plays++; this.paused = false; return Promise.resolve(); }
  pause() { this.pauses++; this.paused = true; }
  metadata() { this.readyState = 1; this.dispatchEvent(new Event('loadedmetadata')); }
  seeked() { this.readyState = 2; this.seeking = false; this.dispatchEvent(new Event('seeked')); }
  requestVideoFrameCallback(cb) { this.callbacks.set(1, cb); return 1; }
  cancelVideoFrameCallback(id) { this.callbacks.delete(id); }
  present(mediaTime = this.seconds) { const cb = this.callbacks.get(1); cb?.(0, { mediaTime }); }
}

test('playback waits for metadata, latest seek and first presented frame before revealing video', async () => {
  const v = new Video(), controller = new AbortController();
  let revealed = false;
  const presented = [];
  const play = startVideoPlayback(v, 91.25, controller.signal, time => {
    assert.equal(revealed, false, 'presented timestamp is known before video is revealed');
    presented.push(time);
  }).then(() => { revealed = true; });
  assert.deepEqual(v.writes, []);
  assert.equal(v.plays, 0);
  v.metadata();
  assert.deepEqual(v.writes, [91.25]);
  assert.equal(v.plays, 0);
  v.seeked(); await tick();
  assert.equal(v.plays, 1);
  assert.equal(revealed, false);
  assert.deepEqual(presented, []);
  v.present(91.233333); await play;
  assert.equal(revealed, true);
  assert.deepEqual(presented, [91.233333], 'propagate actual presentation PTS, not currentTime');
});

test('new navigation cancels pending playback and stale seek events cannot play the old target', async () => {
  const v = new Video(); v.readyState = 2;
  const old = new AbortController();
  const cancelled = assert.rejects(startVideoPlayback(v, 9, old.signal), { name: 'AbortError' });
  old.abort();
  const latest = new AbortController();
  const play = startVideoPlayback(v, 45, latest.signal);
  assert.deepEqual(v.writes, [9]);
  v.seeked(); // completion of the obsolete seek starts only the latest target
  assert.deepEqual(v.writes, [9, 45]);
  assert.equal(v.plays, 0);
  v.seeked(); await tick();
  assert.equal(v.plays, 1);
  v.present(); await play; await cancelled;
});

test('already-aborted request never changes video position', async () => {
  const v = new Video(); v.readyState = 2;
  const controller = new AbortController(); controller.abort();
  await assert.rejects(positionVideo(v, 3, controller.signal), { name: 'AbortError' });
  assert.deepEqual(v.writes, []);
});

test('cancelling during presentation wait cleans callbacks and keeps playback paused', async () => {
  const v = new Video(); v.readyState = 2;
  const controller = new AbortController();
  const cancelled = assert.rejects(startVideoPlayback(v, 2, controller.signal), { name: 'AbortError' });
  v.seeked(); await tick();
  assert.equal(v.callbacks.size, 1);
  controller.abort(); await cancelled;
  assert.equal(v.callbacks.size, 0);
  assert.equal(v.paused, true);
});

test('playback near the last frame completes handoff even when ended occurs before a new callback', async () => {
  for (const beforePlayResolves of [true, false]) {
    const v = new Video(); v.readyState = 2;
    const controller = new AbortController();
    const presented = [];
    if (beforePlayResolves) v.play = () => { v.ended = true; v.paused = true; return Promise.resolve(); };
    const play = startVideoPlayback(v, 999, controller.signal, time => presented.push(time));
    v.seeked(); await tick();
    if (!beforePlayResolves) { v.ended = true; v.dispatchEvent(new Event('ended')); }
    await play;
    assert.equal(v.callbacks.size, 0);
    assert.deepEqual(presented, [], 'ended completion has no presented-frame timestamp');
  }
});

test('animation-frame fallback reveals playback without inventing a presentation timestamp', async () => {
  const originalRequest = globalThis.requestAnimationFrame;
  const originalCancel = globalThis.cancelAnimationFrame;
  let callback;
  globalThis.requestAnimationFrame = cb => { callback = cb; return 1; };
  globalThis.cancelAnimationFrame = () => {};
  try {
    const v = new Video(); v.readyState = 2;
    v.requestVideoFrameCallback = undefined;
    const presented = [];
    const play = startVideoPlayback(v, 2, new AbortController().signal, time => presented.push(time));
    v.seeked(); await tick();
    callback(1234);
    await play;
    assert.deepEqual(presented, []);
  } finally {
    if (originalRequest) globalThis.requestAnimationFrame = originalRequest;
    else delete globalThis.requestAnimationFrame;
    if (originalCancel) globalThis.cancelAnimationFrame = originalCancel;
    else delete globalThis.cancelAnimationFrame;
  }
});

test('a final-frame seek marked ended never calls play and rewinds to frame zero', async () => {
  const v = new Video(); v.readyState = 2;
  const play = startVideoPlayback(v, 29.984, new AbortController().signal);
  v.ended = true;
  v.seeked();
  await play;
  assert.equal(v.plays, 0);
  assert.equal(v.paused, true);
  assert.deepEqual(v.writes, [29.984]);
});

test('seek positions use presentation intervals, including variable-rate and final frames', () => {
  assert.equal(videoFrameTime([0, 33, 117, 150], 1), 0.075);
  assert.equal(videoFrameTime([0, 33, 117, 150], 3), 0.1665);
});

test('idle preparation positions without playing, and Play reuses the ready frame', async () => {
  const v = new Video(); v.readyState = 2;
  const preparation = new VideoPreparation();
  const warm = preparation.prepare(v, 60);
  assert.equal(preparation.prepare(v, 60), warm, 'identical pending work is shared');
  assert.deepEqual(v.writes, [60]);
  v.seeked(); await warm;
  assert.equal(v.plays, 0, 'idle preparation must never start video playback');
  const presented = [];
  const play = preparation.start(v, 60, new AbortController().signal, time => presented.push(time));
  await tick();
  assert.deepEqual(v.writes, [60], 'ready preparation must not seek the same position again');
  assert.equal(v.plays, 1);
  assert.deepEqual(presented, []);
  v.present(59.99); await play;
  assert.deepEqual(presented, [59.99]);
});

test('Play adopts an in-flight preparation and still waits for video presentation', async () => {
  const v = new Video(); v.readyState = 2;
  const preparation = new VideoPreparation();
  const warm = preparation.prepare(v, 60);
  let revealed = false;
  const play = preparation.start(v, 60, new AbortController().signal).then(() => { revealed = true; });
  assert.deepEqual(v.writes, [60]);
  assert.equal(v.plays, 0);
  v.seeked(); await warm; await tick();
  assert.deepEqual(v.writes, [60]);
  assert.equal(v.plays, 1);
  assert.equal(revealed, false);
  v.present(); await play;
  assert.equal(revealed, true);
});

test('new navigation cancels old preparation and a stale Play signal cannot cancel the new target', async () => {
  const v = new Video(); v.readyState = 2;
  const preparation = new VideoPreparation();
  const oldSignal = new AbortController();
  const old = assert.rejects(preparation.start(v, 30, oldSignal.signal), { name: 'AbortError' });
  const latest = preparation.prepare(v, 70);
  oldSignal.abort();
  v.seeked();
  assert.deepEqual(v.writes, [30, 70]);
  v.seeked(); await latest; await old;
  assert.equal(v.plays, 0, 'completion of obsolete positioning must never start playback');
  const play = preparation.start(v, 70, new AbortController().signal);
  await tick(); v.present(); await play;
  assert.equal(v.plays, 1);
  assert.deepEqual(v.writes, [30, 70]);
});

test('cancelling a foreground Play that adopted preparation releases it and permits retry', async () => {
  const v = new Video(); v.readyState = 2;
  const preparation = new VideoPreparation();
  const warm = assert.rejects(preparation.prepare(v, 10), { name: 'AbortError' });
  const controller = new AbortController();
  const play = assert.rejects(preparation.start(v, 10, controller.signal), { name: 'AbortError' });
  controller.abort();
  await warm; await play;
  const retry = preparation.prepare(v, 10);
  v.seeked(); v.seeked(); await retry;
  assert.deepEqual(v.writes, [10, 10]);
  assert.equal(v.plays, 0);
});

test('readiness is invalidated if video position, source, seeking state or decode readiness changed', async () => {
  for (const invalidate of [
    v => { v.seconds = 1; },
    v => { v.src = 'another-video.mp4'; },
    v => { v.seeking = true; },
    v => { v.readyState = 1; },
  ]) {
    const v = new Video(); v.readyState = 2; v.src = 'video.mp4';
    const preparation = new VideoPreparation();
    const warm = preparation.prepare(v, 50);
    v.seeked(); await warm;
    invalidate(v);
    const play = preparation.start(v, 50, new AbortController().signal);
    assert.equal(v.plays, 0);
    v.seeked(); v.seeked(); await tick();
    assert.deepEqual(v.writes, [50, 50]);
    v.present(); await play;
    assert.equal(v.plays, 1);
  }
});

test('a consumed preparation is not reused after playback and pause', async () => {
  const v = new Video(); v.readyState = 2;
  const preparation = new VideoPreparation();
  const play = preparation.start(v, 15, new AbortController().signal);
  v.seeked(); await tick(); v.present(); await play;
  v.seconds = 20; v.pause();
  const again = preparation.start(v, 15, new AbortController().signal);
  assert.deepEqual(v.writes, [15, 15]);
  v.seeked(); await tick(); v.present(); await again;
  assert.equal(v.plays, 2);
});

test('changing target during presentation wait pauses obsolete playback and ignores stale callbacks', async () => {
  const v = new Video(); v.readyState = 2;
  const preparation = new VideoPreparation();
  const presented = [];
  const old = assert.rejects(preparation.start(v, 5, new AbortController().signal,
    time => presented.push(time)), { name: 'AbortError' });
  v.seeked(); await tick();
  const obsoleteCallback = v.callbacks.get(1);
  const next = preparation.prepare(v, 50);
  assert.equal(v.paused, true);
  obsoleteCallback(0, { mediaTime: 5 });
  assert.deepEqual(presented, []);
  v.seeked(); await next; await old;
  assert.equal(v.plays, 1, 'new warmup remains paused');
});

test('prepared final-frame handoff never plays and rewinds to the beginning', async () => {
  const v = new Video(); v.readyState = 2;
  const preparation = new VideoPreparation();
  const warm = preparation.prepare(v, 999);
  v.ended = true; v.seeked(); await warm;
  await preparation.start(v, 999, new AbortController().signal);
  assert.deepEqual(v.writes, [999]);
  assert.equal(v.plays, 0);
});

test('cancel and already-aborted starts do not leave positioning listeners behind', async () => {
  const v = new Video();
  const preparation = new VideoPreparation();
  const warm = assert.rejects(preparation.prepare(v, 30), { name: 'AbortError' });
  preparation.cancel(); await warm;
  v.metadata();
  assert.deepEqual(v.writes, []);
  const signal = new AbortController(); signal.abort();
  await assert.rejects(preparation.start(v, 60, signal.signal), { name: 'AbortError' });
  assert.deepEqual(v.writes, []);
});
