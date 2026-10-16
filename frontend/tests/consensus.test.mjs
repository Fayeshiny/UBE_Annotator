import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import test from 'node:test';
import { build } from 'esbuild';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

// 使用真实组件做服务端渲染，仅替换登录上下文，不依赖浏览器或线上账号。
const built = await build({
  entryPoints: [new URL('../src/components/ConsensusPanel.tsx', import.meta.url).pathname],
  bundle: true, write: false, platform: 'node', format: 'cjs', jsx: 'automatic',
  external: ['react', 'react/jsx-runtime'],
  plugins: [{ name: 'test-user', setup(b) {
    b.onResolve({ filter: /^\.\.\/App$/ }, () => ({ path: 'user', namespace: 'test-user' }));
    b.onLoad({ filter: /.*/, namespace: 'test-user' }, () => ({ contents: 'export function useApp(){return {user:globalThis.__consensusTestUser}}' }));
  } }],
});
const mod = { exports: {} };
new Function('require', 'module', 'exports', built.outputFiles[0].text)(createRequire(import.meta.url), mod, mod.exports);
const Panel = mod.exports.default;
function render(user, changes = {}) {
  globalThis.__consensusTestUser = user;
  const payload = {
    assignment: { id: 1, user_id: 1, status: 'submitted' },
    case: { status: 'open' }, editable: false,
    consensus: { current: true, complete: false, round: 1, revision: 2, approved_count: 0, required: 3, reviews: [] },
    ...changes,
  };
  return renderToStaticMarkup(React.createElement(Panel, { payload, onAction() {} }));
}

test('创建者不能看到独立确认按钮，其他专家可以', () => {
  assert.doesNotMatch(render({ id: 1, role: 'admin' }), /同意本轮共识/);
  assert.match(render({ id: 2, role: 'reviewer' }), /同意本轮共识/);
  assert.doesNotMatch(render({ id: 3, role: 'annotator' }), /同意本轮共识/);
});

test('已签署专家显示撤回按钮；锁定后不能操作', () => {
  const consensus = { current: true, complete: true, round: 1, revision: 2, approved_count: 3, required: 3,
    reviews: [{ id: 1, reviewer_id: 2, username: 'R1', display_name: '专家', decision: 'approve', comment: '同意' }] };
  const html = render({ id: 2, role: 'reviewer' }, { consensus });
  assert.match(html, /撤回我的确认/); assert.doesNotMatch(html, /同意本轮共识/);
  const locked = render({ id: 2, role: 'reviewer' }, { consensus, case: { status: 'locked' } });
  assert.doesNotMatch(locked, /撤回我的确认|退回修改/);
  assert.match(locked, /独立确认 3\/3/);
});
