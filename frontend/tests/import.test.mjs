import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import test from 'node:test';
import { build } from 'esbuild';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

const built = await build({
  entryPoints: [new URL('../src/components/VideoImportNotice.tsx', import.meta.url).pathname],
  bundle: true, write: false, platform: 'node', format: 'cjs', jsx: 'automatic',
  external: ['react', 'react/jsx-runtime'],
});
const mod = { exports: {} };
new Function('require', 'module', 'exports', built.outputFiles[0].text)(createRequire(import.meta.url), mod, mod.exports);
const Notice = mod.exports.default;
function render(status, error = null) {
  return renderToStaticMarkup(React.createElement(Notice, { video: { case_id: 'C', media_status: status, media_error: error } }));
}

test('处理中的视频明确显示等待状态，不生成视频播放器', () => {
  const html = render('processing');
  assert.match(html, /暂时不能播放或标注/);
  assert.match(html, /处理完成后会自动加载/);
  assert.doesNotMatch(html, /<video|刷新状态/);
});

test('中断后展示真实失败原因及返回入口', () => {
  const html = render('error', '服务重启，视频处理已中断');
  assert.match(html, /服务重启，视频处理已中断/);
  assert.match(html, /刷新状态/);
  assert.match(html, /返回病例/);
  assert.doesNotMatch(html, /<video/);
});

test('就绪视频不展示导入等待提示', () => {
  assert.equal(render('ready'), '');
});
