import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import test from 'node:test';
import { build } from 'esbuild';

const built = await build({ entryPoints: [new URL('../src/lib/frameCachePolicy.ts', import.meta.url).pathname],
  bundle: true, write: false, platform: 'node', format: 'cjs' });
const mod = { exports: {} };
new Function('require', 'module', 'exports', built.outputFiles[0].text)(createRequire(import.meta.url), mod, mod.exports);
const { frameCachePolicy } = mod.exports;
const MiB = 1024 * 1024;

test('browser memory hint selects bounded desktop caches while small devices keep the conservative tier', () => {
  for (const memory of [0.25, 1, 2, 4]) {
    const policy = frameCachePolicy(memory);
    assert.equal(policy.decodedBytes, 96 * MiB);
    assert.equal(policy.compressedBytes, 64 * MiB);
    assert.equal(policy.prefetchFrames, 6);
  }
  for (const memory of [8, 16, 128]) {
    const policy = frameCachePolicy(memory);
    assert.equal(policy.decodedBytes, 256 * MiB);
    assert.equal(policy.compressedBytes, 256 * MiB);
    assert.equal(policy.prefetchFrames, 12);
    assert.equal(policy.decodedEntries, 128);
    assert.equal(policy.compressedEntries, 4096);
  }
});

test('missing, invalid and intermediate memory hints cannot opt into the largest cache', () => {
  for (const memory of [undefined, NaN, Infinity, -Infinity, -1, 0, '8', null, 6]) {
    const policy = frameCachePolicy(memory);
    assert.equal(policy.decodedBytes, 128 * MiB);
    assert.equal(policy.compressedBytes, 128 * MiB);
    assert.equal(policy.prefetchFrames, 6);
  }
});
