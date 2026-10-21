import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import test from 'node:test';
import { build } from 'esbuild';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

const built = await build({
  entryPoints: [new URL('../src/components/VideoStage.tsx', import.meta.url).pathname],
  bundle: true, write: false, platform: 'node', format: 'cjs', jsx: 'automatic',
  external: ['react', 'react/jsx-runtime'],
});
const mod = { exports: {} };
new Function('require', 'module', 'exports', built.outputFiles[0].text)(createRequire(import.meta.url), mod, mod.exports);
const Stage = mod.exports.default;

function render(changes = {}) {
  return renderToStaticMarkup(React.createElement(Stage, {
    videoRef: { current: null }, src: '/video', videoW: 640, videoH: 360,
    imageSrc: null, frameReady: false, showVideo: false, showPausedVideo: false,
    playbackStarting: false, frameError: '', onRetryFrame() {}, frameElapsedMs: 0,
    frameCached: false, masks: [], classColors: {}, activeObject: 1, tool: 'point',
    frame: 42, showMasks: true, onPrompt() {}, onPolygon() {}, onVideoEvent() {},
    ...changes,
  }));
}

test('pause retains the decoded video while still loading, with annotation blocked', () => {
  const html = render({ showPausedVideo: true });
  assert.match(html, /<video[^>]*visibility:visible/);
  assert.match(html, /data-playback="still"/);
  assert.match(html, /data-frame-ready="false"/);
  assert.match(html, /frame-status frame-status-preview/);
  assert.match(html, /画面已暂停/);
  assert.match(html, /<canvas[^>]*pointer-events:none/);
  assert.doesNotMatch(html, /class="frame-image"/);
});

test('a failed paused still retains the video and exposes retry', () => {
  const html = render({ showPausedVideo: true, frameError: '帧加载失败' });
  assert.match(html, /<video[^>]*visibility:visible/);
  assert.match(html, /frame-status-preview/);
  assert.match(html, /重试加载/);
  assert.match(html, /data-frame-ready="false"/);
});

test('a jump without a matching preview hides the old video and blocks edits', () => {
  const html = render();
  assert.match(html, /<video[^>]*visibility:hidden/);
  assert.match(html, /data-displayed-frame=""/);
  assert.match(html, /正在加载帧 42/);
  assert.doesNotMatch(html, /frame-status-preview/);
  assert.match(html, /<canvas[^>]*pointer-events:none/);
});

test('a decoded exact still replaces the paused video and enables annotation', () => {
  const html = render({ frameReady: true, imageSrc: '/frame42.jpg' });
  assert.match(html, /<video[^>]*visibility:hidden/);
  assert.match(html, /class="frame-image"/);
  assert.doesNotMatch(html, /class="frame-status/);
  assert.match(html, /<canvas[^>]*pointer-events:auto/);
});

test('starting playback keeps the selected image visible with one compact status and no editing', () => {
  const html = render({ imageSrc: '/frame42.jpg', playbackStarting: true });
  assert.match(html, /class="frame-image"/);
  assert.match(html, /data-displayed-frame="42"/);
  assert.match(html, /data-playback="starting"/);
  assert.match(html, /data-frame-ready="false"/);
  assert.match(html, /frame-status frame-status-preview/);
  assert.equal((html.match(/role="status"/g) ?? []).length, 1);
  assert.match(html, /正在准备播放/);
  assert.match(html, /<video[^>]*visibility:hidden/);
  assert.match(html, /<canvas[^>]*pointer-events:none/);
});

test('playing hands off to video without retaining an old still or status', () => {
  const html = render({ imageSrc: '/frame42.jpg', showVideo: true });
  assert.match(html, /<video[^>]*visibility:visible/);
  assert.doesNotMatch(html, /class="frame-image"/);
  assert.doesNotMatch(html, /class="frame-status/);
  assert.match(html, /<canvas[^>]*pointer-events:none/);
});
