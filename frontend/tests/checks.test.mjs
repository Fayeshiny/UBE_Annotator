import assert from 'node:assert/strict';
import test from 'node:test';
import { mergeMaskIssues } from '../src/lib/checks.ts';

const missing = { level: 'error', track: 'masks', message: '尚无关键结构分割' };
const unverified = { level: 'error', track: 'masks', message: 'O001 尚未经专家复核' };
const shortEvent = { level: 'error', track: 'events', message: '事件不足 5 秒' };
const recovery = { level: 'error', track: 'recovery', message: '缺少恢复点' };

test('确认掩膜后清除过期的缺少分割错误，保留其他轨道错误', () => {
  const current = [shortEvent, recovery, missing];
  const refreshed = [{ level: 'info', track: 'events', message: '旧的服务器时序检查' }];
  assert.deepEqual(mergeMaskIssues(current, refreshed), [shortEvent, recovery]);
  assert.equal(current.length, 3);
});

test('创建未确认结构后更新为未复核提示', () => {
  assert.deepEqual(mergeMaskIssues([missing], [shortEvent, unverified]), [unverified]);
});

test('撤销确认或删除最后一个结构后恢复对应错误', () => {
  assert.deepEqual(mergeMaskIssues([shortEvent], [unverified]), [shortEvent, unverified]);
  assert.deepEqual(mergeMaskIssues([shortEvent, unverified], [missing]), [shortEvent, missing]);
});
