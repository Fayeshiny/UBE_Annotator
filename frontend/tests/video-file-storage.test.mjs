import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import test from 'node:test';
import { build } from 'esbuild';

const built = await build({ entryPoints: [new URL('../src/lib/videoFileStorage.ts', import.meta.url).pathname],
  bundle: true, write: false, platform: 'node', format: 'cjs' });
const mod = { exports: {} };
new Function('require', 'module', 'exports', built.outputFiles[0].text)(createRequire(import.meta.url), mod, mod.exports);
const { createVideoFileStorage } = mod.exports;
const tick = () => new Promise(resolve => setImmediate(resolve));

function globals(t, navigator, indexedDB) {
  for (const [name, value] of Object.entries({ navigator, indexedDB })) {
    const descriptor = Object.getOwnPropertyDescriptor(globalThis, name);
    Object.defineProperty(globalThis, name, { configurable: true, value });
    t.after(() => descriptor ? Object.defineProperty(globalThis, name, descriptor) : delete globalThis[name]);
  }
}

function opfs() {
  const state = { parts: [], files: [], removed: [], writes: 0, closes: 0, aborts: 0, reads: 0,
    write: async () => {}, close: async () => {}, abort: async () => {} };
  const directory = {
    async getFileHandle(name, options) {
      assert.equal(options.create, true);
      state.files.push(name);
      return {
        async createWritable() {
          return {
            async write(blob) { state.writes++; await state.write(blob); state.parts.push(blob); },
            async close() { state.closes++; await state.close(); },
            async abort() { state.aborts++; await state.abort(); },
          };
        },
        async getFile() { state.reads++; return new Blob(state.parts, { type: 'video/mp4' }); },
      };
    },
    async removeEntry(name) { state.removed.push(name); },
  };
  state.navigator = { storage: { async getDirectory() {
    return { async getDirectoryHandle(name, options) {
      assert.equal(name, 'ssi-annotator-video-cache');
      assert.equal(options.create, true);
      return directory;
    } };
  } } };
  return state;
}

function idb() {
  const state = { opened: [], deleted: [], parts: [], closed: 0, transactions: [], quota: false, hold: false };
  const databases = new Map();
  state.factory = {
    open(name) {
      const request = {};
      const records = new Map();
      const db = {
        createObjectStore(store) { assert.equal(store, 'chunks'); },
        close() { state.closed++; },
        transaction(store, mode) {
          assert.equal(store, 'chunks');
          let ended = false;
          const tx = {
            error: null,
            abort() {
              if (ended) throw new DOMException('Done', 'InvalidStateError');
              ended = true;
              queueMicrotask(() => tx.onabort?.());
            },
            objectStore() {
              return {
                put(blob, key) {
                  if (state.quota) throw new DOMException('Storage full', 'QuotaExceededError');
                  assert.ok(blob instanceof Blob);
                  state.parts.push(blob);
                  records.set(key, blob);
                },
                openCursor() {
                  const request = {};
                  const entries = [...records.entries()].sort((a, b) => a[0] - b[0]);
                  let position = 0;
                  const next = () => queueMicrotask(() => {
                    if (ended) return;
                    const entry = entries[position++];
                    request.result = entry ? { key: entry[0], value: entry[1], continue: next } : null;
                    request.onsuccess?.();
                  });
                  next();
                  return request;
                },
              };
            },
          };
          state.transactions.push({ mode, tx });
          setImmediate(() => { if (!ended && !state.hold) { ended = true; tx.oncomplete?.(); } });
          return tx;
        },
      };
      databases.set(name, db);
      state.opened.push(name);
      queueMicrotask(() => {
        request.result = db;
        request.onupgradeneeded?.();
        request.onsuccess?.();
      });
      return request;
    },
    deleteDatabase(name) {
      const request = {};
      state.deleted.push(name);
      databases.delete(name);
      queueMicrotask(() => request.onsuccess?.());
      return request;
    },
  };
  return state;
}

test('OPFS streams original chunks sequentially and returns a file only after closing', async t => {
  const fs = opfs(); globals(t, fs.navigator, undefined);
  const storage = await createVideoFileStorage();
  const a = new Blob(['first']), b = new Blob(['-second']);
  // Prevent an accidental future implementation from collecting whole-video buffers.
  a.arrayBuffer = b.arrayBuffer = () => { throw new Error('Do not materialize the full video'); };
  await Promise.all([storage.append(a), storage.append(b)]);
  const completion = storage.complete();
  assert.equal(storage.complete(), completion);
  const video = await completion;
  assert.equal(video.size, a.size + b.size);
  assert.equal(await video.text(), 'first-second');
  assert.deepEqual(fs.parts, [a, b]);
  assert.equal(fs.closes, 1);
  assert.equal(fs.reads, 1);
  await assert.rejects(storage.append(new Blob(['late'])), { name: 'InvalidStateError' });
  await storage.dispose(); await storage.dispose();
  assert.deepEqual(fs.removed, fs.files);
  assert.equal(fs.aborts, 0);
});

test('dispose interrupts the active OPFS write and cancels queued work without recreating files', async t => {
  const fs = opfs(); globals(t, fs.navigator, undefined);
  let failWrite;
  fs.write = () => new Promise((_, reject) => { failWrite = reject; });
  fs.abort = async () => failWrite(new DOMException('Cancelled', 'AbortError'));
  const storage = await createVideoFileStorage();
  const first = storage.append(new Blob(['first']));
  const second = storage.append(new Blob(['never-written']));
  const failures = Promise.all([
    assert.rejects(first, { name: 'AbortError' }), assert.rejects(second, { name: 'AbortError' }),
  ]);
  await tick();
  await storage.dispose();
  await failures;
  assert.equal(fs.writes, 1);
  assert.equal(fs.aborts, 1);
  assert.deepEqual(fs.removed, fs.files);
  await assert.rejects(storage.complete(), { name: 'AbortError' });
  await assert.rejects(storage.append(new Blob()), { name: 'AbortError' });
  assert.equal(fs.files.length, 1);
});

test('an OPFS quota error is preserved and automatically removes the partial file', async t => {
  const fs = opfs(); globals(t, fs.navigator, undefined);
  fs.write = async () => { throw new DOMException('Storage full', 'QuotaExceededError'); };
  const storage = await createVideoFileStorage();
  await assert.rejects(storage.append(new Blob(['overflow'])), { name: 'QuotaExceededError' });
  await storage.dispose();
  assert.deepEqual(fs.removed, fs.files);
  assert.equal(fs.reads, 0);
});

test('remote HTTP falls back to isolated IndexedDB Blob chunks with stable ordering', async t => {
  const db = idb(); globals(t, { storage: {} }, db.factory);
  const storage = await createVideoFileStorage();
  const other = await createVideoFileStorage();
  const chunks = [new Blob(['one']), new Blob(['-two']), new Blob(['-three'])];
  for (const blob of chunks) blob.arrayBuffer = () => { throw new Error('No whole-file buffers'); };
  await Promise.all(chunks.map(chunk => storage.append(chunk)));
  const video = await storage.complete();
  assert.equal(video.type, 'video/mp4');
  assert.equal(await video.text(), 'one-two-three');
  assert.deepEqual(db.parts, chunks);
  assert.notEqual(db.opened[0], db.opened[1]);
  await storage.dispose();
  assert.deepEqual(db.deleted, [db.opened[0]], 'only the owning cache is removed');
  await other.append(new Blob(['still-active']));
  assert.equal(await (await other.complete()).text(), 'still-active');
  await other.dispose();
  assert.deepEqual(db.deleted, db.opened);
  assert.equal(db.closed, 2);
});

test('IndexedDB dispose aborts active and queued transactions before deleting its database', async t => {
  const db = idb(); db.hold = true; globals(t, {}, db.factory);
  const storage = await createVideoFileStorage();
  const first = storage.append(new Blob(['partial']));
  const second = storage.append(new Blob(['queued']));
  const failures = Promise.all([
    assert.rejects(first, { name: 'AbortError' }), assert.rejects(second, { name: 'AbortError' }),
  ]);
  await tick();
  await storage.dispose(); await failures;
  assert.equal(db.transactions.length, 1);
  assert.deepEqual(db.deleted, db.opened);
  await storage.dispose();
  assert.equal(db.deleted.length, 1);
});

test('IndexedDB quota failures retain their status and clean the partial database', async t => {
  const db = idb(); db.quota = true; globals(t, {}, db.factory);
  const storage = await createVideoFileStorage();
  await assert.rejects(storage.append(new Blob(['overflow'])), { name: 'QuotaExceededError' });
  await storage.dispose();
  assert.deepEqual(db.deleted, db.opened);
});

test('unavailable local storage fails with an actionable message', async t => {
  globals(t, {}, undefined);
  await assert.rejects(createVideoFileStorage(), /存储权限|HTTPS/);
});
test('IndexedDB random writes compose ranges in physical offset order',async t=>{
 const db=idb();globals(t,{},db.factory);const storage=await createVideoFileStorage();
 await storage.write(4,new Blob(['efgh']));await storage.write(0,new Blob(['abcd']));await storage.write(8,new Blob(['ij']));
 assert.equal(await(await storage.complete()).text(),'abcdefghij');await storage.dispose();assert.deepEqual(db.deleted,db.opened);
});
