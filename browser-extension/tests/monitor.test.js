// @ts-check
/**
 * 回放（monitor）行为的回归测试。
 *
 * 这些测试守住一个**产品决策**：默认静默录制。
 *
 * 为什么值得专门测：`monitor` 的默认值是一个单独的布尔量，改错了不会报错、
 * 不会有任何测试变红，只会让用户下次开录时突然被声音打扰。而且这个值曾经
 * 出过一个真实缺陷 —— startMany 用写死的 DEFAULT_OPTS，导致用户的静音选择
 * 每次开录都被遗忘。下面第三组测试就是针对那个缺陷的。
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { installChromeStub } from './helpers/chrome-stub.js';

const stub = installChromeStub();
await import('../src/sw/service-worker.js');

/** 取 storage.session 里最后一次写入的 tabRecords */
function lastRecords() {
  const writes = stub.callsTo('storage.session.set');
  if (writes.length === 0) return [];
  return writes[writes.length - 1].args[0].tabRecords ?? [];
}

/** 取发往 offscreen 的最后一条指定类型的消息 */
/** @param {string} type */
function lastMessageOfType(type) {
  const msgs = stub.callsTo('runtime.sendMessage')
    .map((c) => c.args[0])
    .filter((m) => m?.type === type);
  return msgs[msgs.length - 1] ?? null;
}

/** 触发 SW 的消息处理器并等它回复 */
/** @param {any} message */
async function send(message) {
  const handlers = stub.listeners['runtime.onMessage'];
  /** @type {any} */
  let reply = null;
  handlers[0](message, {}, (/** @type {any} */ r) => { reply = r; });
  await new Promise((r) => setTimeout(r, 60));
  return reply;
}

// ─────────────────── 默认值：静默录制 ───────────────────

test('★ 默认偏好为静音（未设置过时）', async () => {
  stub.reset();
  const reply = await send({ v: 1, to: 'sw', type: 'UI_GET_PREFS' });
  assert.equal(
    reply?.prefs?.monitor, false,
    '默认必须是静音 —— 这是用户明确选择的「后台静默录制」行为',
  );
});

test('★ 开始录制时传给 offscreen 的 monitor 为 false', async () => {
  stub.reset();
  // 先授权一个标签页
  stub.fire('commands.onCommand', 'arm-current-tab', {
    id: 101, windowId: 1, url: 'https://example.com/', title: '测试',
  });
  await new Promise((r) => setTimeout(r, 40));

  stub.reset();
  await send({ v: 1, to: 'sw', type: 'UI_START', tabIds: [101] });

  const start = lastMessageOfType('OFFSCREEN_START_ONE');
  assert.ok(start, '应向 offscreen 发送 START_ONE');
  assert.equal(
    start.opts.monitor, false,
    '默认开始录制时 monitor 必须为 false（静默录制）',
  );
});

test('录制中的记录里 monitor 字段与实际一致', async () => {
  stub.reset();
  stub.fire('commands.onCommand', 'arm-current-tab', {
    id: 102, windowId: 1, url: 'https://example.com/', title: '测试2',
  });
  await new Promise((r) => setTimeout(r, 40));
  await send({ v: 1, to: 'sw', type: 'UI_START', tabIds: [102] });

  const record = lastRecords().find((/** @type {any} */ r) => r.tabId === 102);
  assert.ok(record, '应有该标签页的记录');
  assert.equal(record.monitor, false, '记录里的 monitor 应与传给 offscreen 的一致');
});

// ─────────────────── 全局开关 ───────────────────

test('全局开关能打开回放并被持久化', async () => {
  stub.reset();
  const reply = await send({ v: 1, to: 'sw', type: 'UI_SET_MONITOR_ALL', enabled: true });
  assert.equal(reply?.ok, true);
  assert.equal(reply?.enabled, true);

  // 必须写进 storage.local（跨浏览器重启保留），而不是 session
  const writes = stub.callsTo('storage.local.set');
  assert.ok(writes.length > 0, '偏好应写入 storage.local');
  const written = writes[writes.length - 1].args[0];
  assert.equal(written['pref.monitor'], true);

  // 读回来应该是 true
  const prefs = await send({ v: 1, to: 'sw', type: 'UI_GET_PREFS' });
  assert.equal(prefs?.prefs?.monitor, true, '偏好应能读回');
});

test('★ 用户的选择在下次开始录制时被沿用（修复「设置被遗忘」缺陷）', async () => {
  // 这是针对一个真实缺陷的回归测试：以前 startMany 用写死的 DEFAULT_OPTS.monitor，
  // 于是用户打开回放后，停止再录又会变回默认值 —— 界面让人以为设置保留了，实际没有。
  stub.reset();
  await send({ v: 1, to: 'sw', type: 'UI_SET_MONITOR_ALL', enabled: true });

  stub.fire('commands.onCommand', 'arm-current-tab', {
    id: 103, windowId: 1, url: 'https://example.com/', title: '测试3',
  });
  await new Promise((r) => setTimeout(r, 40));

  stub.reset();
  await send({ v: 1, to: 'sw', type: 'UI_START', tabIds: [103] });

  const start = lastMessageOfType('OFFSCREEN_START_ONE');
  assert.ok(start, '应向 offscreen 发送 START_ONE');
  assert.equal(
    start.opts.monitor, true,
    '开始录制应沿用用户上次的回放选择，而不是回到默认值',
  );

  // 复位，避免影响后续测试
  await send({ v: 1, to: 'sw', type: 'UI_SET_MONITOR_ALL', enabled: false });
});

test('全局静音会同时下发给 offscreen', async () => {
  stub.reset();
  // 先让一个标签页进入录制状态
  stub.fire('commands.onCommand', 'arm-current-tab', {
    id: 104, windowId: 1, url: 'https://example.com/', title: '测试4',
  });
  await new Promise((r) => setTimeout(r, 40));
  await send({ v: 1, to: 'sw', type: 'UI_START', tabIds: [104] });

  stub.reset();
  const reply = await send({ v: 1, to: 'sw', type: 'UI_SET_MONITOR_ALL', enabled: true });

  assert.ok(reply?.affected >= 1, `应影响至少一个录制中的标签页（实际 ${reply?.affected}）`);
  const msg = lastMessageOfType('OFFSCREEN_SET_MONITOR_ALL');
  assert.ok(msg, '应向 offscreen 下发批量切换指令');
  assert.equal(msg.enabled, true);

  await send({ v: 1, to: 'sw', type: 'UI_SET_MONITOR_ALL', enabled: false });
});

test('没有录制中的标签页时，全局开关只存偏好不发消息', async () => {
  stub.reset();
  // 全部停止，确保没有活跃录制
  await send({ v: 1, to: 'sw', type: 'UI_STOP_ALL' });
  stub.reset();

  const reply = await send({ v: 1, to: 'sw', type: 'UI_SET_MONITOR_ALL', enabled: true });
  assert.equal(reply?.ok, true);
  // 偏好仍要存
  assert.ok(stub.callsTo('storage.local.set').length > 0, '偏好仍应被保存');

  await send({ v: 1, to: 'sw', type: 'UI_SET_MONITOR_ALL', enabled: false });
});

// ─────────────────── 单个标签页开关 ───────────────────

test('单个标签页的回放开关仍然独立可用', async () => {
  stub.reset();
  const reply = await send({
    v: 1, to: 'sw', type: 'UI_SET_MONITOR', tabId: 104, enabled: true,
  });
  assert.equal(reply?.ok, true);
  const msg = lastMessageOfType('OFFSCREEN_SET_MONITOR');
  assert.ok(msg, '应向 offscreen 下发单个切换指令');
  assert.equal(msg.tabId, 104);
  assert.equal(msg.enabled, true);
});

// ─────────────────── 录制不受回放影响 ───────────────────

test('★ 静音与否都不改变录制参数（音质/容器）', async () => {
  /** @param {boolean} monitor */
  async function optsWithMonitor(monitor, tabId) {
    await send({ v: 1, to: 'sw', type: 'UI_SET_MONITOR_ALL', enabled: monitor });
    stub.fire('commands.onCommand', 'arm-current-tab', {
      id: tabId, windowId: 1, url: 'https://example.com/', title: 'x',
    });
    await new Promise((r) => setTimeout(r, 40));
    stub.reset();
    await send({ v: 1, to: 'sw', type: 'UI_START', tabIds: [tabId] });
    return lastMessageOfType('OFFSCREEN_START_ONE').opts;
  }

  const muted = await optsWithMonitor(false, 201);
  const audible = await optsWithMonitor(true, 202);

  // 回放只是一条独立的输出链路，不该影响任何录制参数。
  // 这条测试守住「静音不损音质」这个承诺。
  assert.equal(muted.audioBitsPerSecond, audible.audioBitsPerSecond, '码率不应受回放影响');
  assert.equal(muted.timesliceMs, audible.timesliceMs, 'timeslice 不应受回放影响');
  assert.equal(muted.docType, audible.docType, '容器格式不应受回放影响');
  assert.equal(muted.extension, audible.extension, '扩展名不应受回放影响');
  assert.equal(muted.docType, 'matroska', '仍应输出 matroska');

  await send({ v: 1, to: 'sw', type: 'UI_SET_MONITOR_ALL', enabled: false });
});
