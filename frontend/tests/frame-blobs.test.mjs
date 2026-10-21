import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import test from 'node:test';
import { build } from 'esbuild';

const built = await build({ entryPoints: [new URL('../src/lib/frameBlobs.ts', import.meta.url).pathname],
  bundle: true, write: false, platform: 'node', format: 'cjs' });
const mod = { exports: {} };
new Function('require', 'module', 'exports', built.outputFiles[0].text)(createRequire(import.meta.url), mod, mod.exports);
const { FrameBlobCache, FrameRequestError } = mod.exports;
const tick = () => new Promise(resolve => setImmediate(resolve));
const signal = () => new AbortController().signal;
const jpeg = (size = 4) => new Blob([new Uint8Array(size)], { type: 'image/jpeg' });
const response = (blob = jpeg(), status = 200) => ({ ok: status === 200, status, blob: async () => blob });

function browser(t) {
  const state = { requests: [], images: [], created: [], revoked: [], events: [], autoDecode: true,
    fetch: async () => response() };
  for (const name of ['fetch', 'Image', 'window']) {
    const descriptor = Object.getOwnPropertyDescriptor(globalThis, name);
    t.after(() => descriptor ? Object.defineProperty(globalThis, name, descriptor) : delete globalThis[name]);
  }
  const create = URL.createObjectURL, revoke = URL.revokeObjectURL;
  t.after(() => { URL.createObjectURL = create; URL.revokeObjectURL = revoke; });
  globalThis.fetch = (url, options) => { state.requests.push({ url, options }); return state.fetch(url, options); };
  globalThis.window = { dispatchEvent: event => state.events.push(event.type) };
  URL.createObjectURL = blob => {
    const url = `blob:test-${state.created.length}`;
    state.created.push({ blob, url });
    return url;
  };
  URL.revokeObjectURL = url => state.revoked.push(url);
  globalThis.Image = class {
    naturalWidth = 200;
    naturalHeight = 100;
    src = '';
    constructor() { state.images.push(this); }
    decode() {
      if (state.autoDecode) return Promise.resolve();
      return new Promise((resolve, reject) => { this.resolve = resolve; this.reject = reject; });
    }
  };
  return state;
}

async function released(cache, frame) {
  const image = await cache.load(frame, `/frame/${frame}?revision=test`, signal(), false);
  image.release();
  return image;
}

test('released decoded images can be recreated from original JPEG without a second HTTP request', async t => {
  const ui = browser(t);
  const cache = new FrameBlobCache();
  const first = await released(cache, 9);
  first.release();
  assert.equal(cache.has(9), true);
  assert.equal(first.bytes, 200 * 100 * 4 + 4, 'decoded URL pins its source JPEG even after compressed LRU eviction');
  const second = await cache.load(9, '/frame/9?revision=test', signal(), false);
  assert.notEqual(second.url, first.url);
  assert.equal(ui.requests.length, 1);
  assert.equal(ui.created[0].blob, ui.created[1].blob, 'no JPEG re-encoding or quality conversion');
  assert.deepEqual(ui.revoked, [first.url]);
  cache.clear();
  assert.equal(cache.has(9), false);
  assert.equal(ui.images[1].src, second.url, 'the compressed tier does not own returned decoded images');
  second.release();
  assert.deepEqual(ui.revoked, [first.url, second.url]);
});

test('compressed byte budget evicts least recently read JPEGs, while oversized JPEGs remain displayable', async t => {
  const ui = browser(t);
  ui.fetch = async url => response(jpeg(url.includes('/99?') ? 9 : 4));
  const cache = new FrameBlobCache(8, 10);
  await released(cache, 1);
  await released(cache, 2);
  await released(cache, 1); // refresh JPEG LRU, with no network request
  await released(cache, 3);
  assert.equal(cache.has(1), true);
  assert.equal(cache.has(2), false);
  assert.equal(cache.has(3), true);
  assert.equal(ui.requests.length, 3);
  await released(cache, 99);
  assert.equal(cache.has(99), false);
  assert.equal(cache.has(1), true, 'oversized entries do not empty the bounded cache');
  assert.equal(cache.has(3), true);
  await released(cache, 99);
  assert.equal(ui.requests.length, 5, 'uncached oversized JPEG is downloaded again');
  cache.clear();
});

test('entry count also bounds tiny JPEGs, and read access updates eviction order', async t => {
  const ui = browser(t);
  const cache = new FrameBlobCache(100, 2);
  for (const frame of [4, 5, 4, 6]) await released(cache, frame);
  assert.equal(cache.has(4), true);
  assert.equal(cache.has(5), false);
  assert.equal(cache.has(6), true);
  assert.equal(ui.requests.length, 3);
  cache.clear();
  assert.equal(cache.has(4), false);
  await released(cache, 4);
  assert.equal(ui.requests.length, 4, 'cleared instances may be reused by React StrictMode');
  cache.clear();
});

test('a zero byte or entry limit disables compressed retention while decoded images still work', async t => {
  const ui = browser(t);
  for (const cache of [new FrameBlobCache(0, 10), new FrameBlobCache(100, 0)]) {
    await released(cache, 1);
    assert.equal(cache.has(1), false);
    await released(cache, 1);
    assert.equal(cache.has(1), false);
    cache.clear();
  }
  assert.equal(ui.requests.length, 4);
  assert.equal(ui.revoked.length, 4);
});

test('already cancelled requests never fetch or decode', async t => {
  const ui = browser(t);
  const controller = new AbortController(); controller.abort();
  const cache = new FrameBlobCache();
  await assert.rejects(cache.load(1, '/frame/1', controller.signal, false), { name: 'AbortError' });
  assert.equal(ui.requests.length, 0);
  assert.equal(ui.images.length, 0);
});

test('aborted fetches reject promptly and ignored late responses never enter either tier', async t => {
  const ui = browser(t);
  let resolve;
  ui.fetch = () => new Promise(done => { resolve = done; });
  const controller = new AbortController();
  const cache = new FrameBlobCache();
  const pending = cache.load(2, '/frame/2', controller.signal, false);
  const rejected = assert.rejects(pending, { name: 'AbortError' });
  controller.abort();
  await rejected;
  assert.equal(ui.requests[0].options.signal.aborted, true);
  resolve(response());
  await tick();
  assert.equal(cache.has(2), false);
  assert.equal(ui.images.length, 0);
});

test('abort while reading response body does not retain a late Blob', async t => {
  const ui = browser(t);
  let finishBody;
  ui.fetch = async () => ({ ok: true, status: 200, blob: () => new Promise(resolve => { finishBody = resolve; }) });
  const cache = new FrameBlobCache();
  const controller = new AbortController();
  const pending = cache.load(2, '/frame/2', controller.signal, false);
  const rejected = assert.rejects(pending, { name: 'AbortError' });
  await tick();
  controller.abort(); await rejected;
  finishBody(jpeg()); await tick();
  assert.equal(cache.has(2), false);
  assert.equal(ui.images.length, 0);
});

test('abort during decoding promptly revokes only the decoded URL and preserves a valid cached JPEG', async t => {
  const ui = browser(t);
  const cache = new FrameBlobCache();
  await released(cache, 7);
  ui.autoDecode = false;
  const controller = new AbortController();
  const pending = cache.load(7, '/frame/7', controller.signal, false);
  const rejected = assert.rejects(pending, { name: 'AbortError' });
  await tick();
  controller.abort(); await rejected;
  assert.equal(cache.has(7), true);
  assert.equal(ui.images[1].src, '');
  assert.deepEqual(ui.revoked, ['blob:test-0', 'blob:test-1']);
  ui.images[1].resolve(); await tick();
  assert.equal(ui.revoked.length, 2, 'late decoder success cannot double-release or restore stale data');
  ui.autoDecode = true;
  await released(cache, 7);
  assert.equal(ui.requests.length, 1);
  cache.clear();
});

test('clear aborts active fetches and generation guards ignore their late responses after reuse', async t => {
  const ui = browser(t);
  let oldResponse;
  ui.fetch = () => new Promise(resolve => { oldResponse = resolve; });
  const cache = new FrameBlobCache();
  const old = cache.load(1, '/frame/1', signal(), true);
  const rejected = assert.rejects(old, { name: 'AbortError' });
  cache.clear(); await rejected;
  assert.equal(ui.requests[0].options.signal.aborted, true);
  ui.fetch = async () => response(jpeg(8));
  await released(cache, 2);
  oldResponse(response(jpeg(4))); await tick();
  assert.equal(cache.has(1), false);
  assert.equal(cache.has(2), true);
  assert.equal(ui.created.length, 1);
  cache.clear();
});

test('clear during decoding frees its URL and cannot cache a late successful decode', async t => {
  const ui = browser(t); ui.autoDecode = false;
  const cache = new FrameBlobCache();
  const pending = cache.load(6, '/frame/6', signal(), false);
  const rejected = assert.rejects(pending, { name: 'AbortError' });
  await tick();
  cache.clear(); await rejected;
  assert.equal(ui.images[0].src, '');
  assert.deepEqual(ui.revoked, ['blob:test-0']);
  ui.images[0].resolve(); await tick();
  assert.equal(cache.has(6), false);
});

test('invalid JPEGs are not cached, and a corrupt cached JPEG is removed so retry refetches', async t => {
  const ui = browser(t); ui.autoDecode = false;
  const cache = new FrameBlobCache();
  const invalid = cache.load(3, '/frame/3', signal(), false);
  const rejected = assert.rejects(invalid, /画面解码失败/);
  await tick(); ui.images[0].reject(new Error('bad jpeg')); await rejected;
  assert.equal(cache.has(3), false);
  ui.autoDecode = true;
  await released(cache, 3);
  ui.autoDecode = false;
  const corrupt = cache.load(3, '/frame/3', signal(), false);
  const corruptRejected = assert.rejects(corrupt, /画面解码失败/);
  await tick(); ui.images[2].reject(new Error('bad jpeg')); await corruptRejected;
  assert.equal(cache.has(3), false);
  ui.autoDecode = true;
  await released(cache, 3);
  assert.equal(ui.requests.length, 3, 'both initial and cached decode failures allow a fresh download');
  cache.clear();
});

test('HTTP errors preserve status, authentication event and useful Chinese explanations', async t => {
  const ui = browser(t);
  const cache = new FrameBlobCache();
  for (const [status, message] of [[401, '401'], [403, '权限'], [409, '视频已更新'], [429, '429'], [503, '503']]) {
    ui.fetch = async () => response(undefined, status);
    await assert.rejects(cache.load(5, '/frame/5?revision=test', signal(), false), error =>
      error instanceof FrameRequestError && error.status === status && error.message.includes(message));
    assert.equal(cache.has(5), false);
  }
  assert.deepEqual(ui.events, ['ssi-unauthorized']);
  assert.equal(ui.images.length, 0);
});

test('speculation uses original JPEG credentials and low priority while foreground uses high priority', async t => {
  const ui = browser(t);
  const cache = new FrameBlobCache();
  (await cache.load(1, '/frame/1?revision=abc', signal(), true)).release();
  (await cache.load(2, '/frame/2', signal(), true)).release();
  await released(cache, 3);
  assert.equal(ui.requests[0].url, '/frame/1?revision=abc&prefetch=true');
  assert.equal(ui.requests[1].url, '/frame/2?prefetch=true');
  assert.equal(ui.requests[0].options.credentials, 'same-origin');
  assert.equal(ui.requests[0].options.priority, 'low');
  assert.equal(ui.requests[2].options.priority, 'high');
  cache.clear();
});

test('network errors have a retry message and do not create cached or decoded images', async t => {
  const ui = browser(t);
  ui.fetch = async () => { throw new TypeError('network unavailable'); };
  const cache = new FrameBlobCache();
  await assert.rejects(cache.load(1, '/frame/1', signal(), false), /画面下载失败/);
  assert.equal(cache.has(1), false);
  assert.equal(ui.images.length, 0);
});
