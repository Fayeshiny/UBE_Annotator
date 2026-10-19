import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import test from 'node:test';
import { build } from 'esbuild';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

const built = await build({
  entryPoints: [new URL('../src/components/MaskPanel.tsx', import.meta.url).pathname],
  bundle: true, write: false, platform: 'node', format: 'cjs', jsx: 'automatic',
  external: ['react', 'react/jsx-runtime'],
});
const mod = { exports: {} };
new Function('require', 'module', 'exports', built.outputFiles[0].text)(createRequire(import.meta.url), mod, mod.exports);
const Panel = mod.exports.default;
const ready = { id: 'session', status: 'ready', frames: [0, 5, 10], start_ms: 0, end_ms: 2000, progress: 0 };

function render(session, wsChanges = {}) {
  return renderToStaticMarkup(React.createElement(Panel, {
    ws: { ann: { context: [{ start_ms: 0, label: 'C1' }] }, currentMs: 1000, duration: 4000, editable: true,
      protocol: { mask_classes: [], sam_backend: 'sam3.1' }, ...wsChanges },
    objects: [], activeObject: null, session, creatingSession: false, tool: 'none', frame: 30,
    onCandidatePreview() {},
  }));
}

function candidateButton(html) {
  return html.match(/<button[^>]*>自动生成当前帧候选<\/button>/)?.[0];
}

test('建立片段后无需结构目标或人工提示即可请求候选', () => {
  const html = render(ready);
  assert.ok(candidateButton(html));
  assert.doesNotMatch(candidateButton(html), /disabled/);
  assert.match(html, /无需添加目标或手动提示/);
  assert.match(html, /不会保存为正式标注/);
});

test('片段未就绪、正在传播、当前帧在片段外或只读时禁用候选按钮', () => {
  for (const [session, ws] of [[null, {}], [{ ...ready, status: 'loading' }, {}],
    [{ ...ready, status: 'propagating' }, {}], [ready, { currentMs: 2500 }], [ready, { editable: false }]]) {
    assert.match(candidateButton(render(session, ws)), /disabled/);
  }
});

test('mock 后端明确标示候选为模拟结果', () => {
  const html = render(ready, { protocol: { mask_classes: [], sam_backend: 'mock' } });
  assert.match(html, /候选是模拟结果/);
});
