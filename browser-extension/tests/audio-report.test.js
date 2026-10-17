// @ts-check
/**
 * 音频活性心跳的透传链路测试：offscreen 上报 → SW → registry → storage.session。
 *
 * 顺带守住一个曾真实存在的缺陷：录制中的进度心跳是 `recording → recording`
 * 的同态上报，而状态机转移表一度不允许这个自环 —— 于是每次心跳都被
 * setState 静默拒绝，bytes 永远停在 0。现在 audio 走同一条链路，
 * 这条测试同时锁住两者。
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { installChromeStub } from './helpers/chrome-stub.js';
import { installIndexedDbStub } from './helpers/idb-stub.js';

const stub = installChromeStub();
installIndexedDbStub();
await import('../src/sw/service-worker.js');

/** 取 storage.session 里最后一次写入的 tabRecords */
function lastRecords() {
  const writes = stub.callsTo('storage.session.set');
  if (writes.length === 0) return [];
  return writes[writes.length - 1].args[0].tabRecords ?? [];
}

/** 触发 SW 的消息处理器并等它回复 + 给 60ms 的合并写盘定时器留出时间 */
/** @param {any} message */
async function send(message) {
  const handlers = stub.listeners['runtime.onMessage'];
  /** @type {any} */
  let reply = null;
  handlers[0](message, {}, (/** @type {any} */ r) => { reply = r; });
  await new Promise((r) => setTimeout(r, 100));
  return reply;
}

/** 授权一个标签页并启动录制（桩的 offscreen ack 立即成功，直达 recording 态） */
/** @param {number} tabId */
async function armAndStart(tabId) {
  stub.fire('commands.onCommand', 'arm-current-tab', {
    id: tabId, windowId: 1, url: 'https://example.com/', title: '测试',
  });
  await new Promise((r) => setTimeout(r, 40));
  await send({ v: 1, to: 'sw', type: 'UI_START', tabIds: [tabId] });
}

test('★ 录制中的心跳（bytes + audio）被接受并透传进 storage 记录', async () => {
  stub.reset();
  await armAndStart(301);
  stub.reset();

  const audio = { level: 0.02, hasSound: true, silentMs: 0 };
  await send({
    v: 1, to: 'sw', type: 'SW_EVENT_TAB_STATE',
    tabId: 301, state: 'recording', bytes: 12345, audio,
  });

  const record = lastRecords().find((/** @type {any} */ r) => r.tabId === 301);
  assert.ok(record, '应有该标签页的记录');
  assert.equal(record.state, 'recording');
  assert.equal(
    record.bytes, 12345,
    '录制中的 bytes 心跳必须被接受 —— 曾被状态机拒绝而自环修复',
  );
  assert.deepEqual(record.audio, audio, '音频活性快照应随记录镜像进 storage');
});

test('畸形的 audio 字段被丢弃，不污染 storage 镜像', async () => {
  stub.reset();
  await armAndStart(302);
  stub.reset();

  await send({
    v: 1, to: 'sw', type: 'SW_EVENT_TAB_STATE',
    tabId: 302, state: 'recording', bytes: 100,
    audio: { silentMs: 'not-a-number' }, // 结构不完整
  });

  const record = lastRecords().find((/** @type {any} */ r) => r.tabId === 302);
  assert.ok(record);
  assert.equal(record.bytes, 100, '合法字段不受影响');
  assert.equal(record.audio, undefined, '畸形 audio 不应进入记录');
});

test('★ 重新开始录制时清空上一轮的活性数据', async () => {
  stub.reset();
  await armAndStart(303);

  // 上一轮：有心跳，audio 已就位
  await send({
    v: 1, to: 'sw', type: 'SW_EVENT_TAB_STATE',
    tabId: 303, state: 'recording', bytes: 100,
    audio: { level: 0.02, hasSound: true, silentMs: 0 },
  });
  // 出错 → 重试进入新一轮（error → starting 是合法转移）
  await send({
    v: 1, to: 'sw', type: 'SW_EVENT_TAB_STATE',
    tabId: 303, state: 'error', error: { code: 'E_RECORDER', message: '模拟失败' },
  });
  stub.reset();
  await send({ v: 1, to: 'sw', type: 'UI_RETRY', tabId: 303 });

  const record = lastRecords().find((/** @type {any} */ r) => r.tabId === 303);
  assert.ok(record);
  assert.equal(record.state, 'recording', '重试应直达录制中（桩的 ack 立即成功）');
  assert.equal(
    record.audio, undefined,
    '新一轮录制不应沿用上一轮的活性状态 —— 否则面板头几秒会显示陈旧指示',
  );
});
