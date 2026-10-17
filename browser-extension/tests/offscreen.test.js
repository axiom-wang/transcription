// @ts-check
/**
 * offscreen 文档的加载期测试。
 *
 * 和 service-worker.test.js 同源的动机：offscreen 文档一旦在模块求值期抛错，
 * 它的 onMessage 监听器就不会注册，于是 SW 发过去的 START_ONE 会一直等到超时，
 * 症状是「点了开始录制，转一会儿然后报错」，而 DevTools 里几乎没有线索
 * （offscreen 文档不可见，要专门去 chrome://extensions 点「检查视图」才看得到）。
 *
 * 这里额外需要 IndexedDB 和 MediaRecorder 的桩 —— offscreen 顶层会碰它们。
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { installChromeStub } from './helpers/chrome-stub.js';

// ★ 必须用 offscreen 上下文的桩：它只保留 chrome.runtime，其余命名空间全部删掉。
// 这样 offscreen.js 是在**真实的 API 限制**下被加载的 —— 之前用完整桩，
// 于是 saver.js 里的 chrome.downloads 调用完全没被拦住（那正是线上故障的原因）。
const stub = installChromeStub({ context: 'offscreen' });

// ── offscreen 依赖的浏览器 API 桩 ──

/** indexedDB：只需让 open() 返回一个不会 resolve 的请求即可 ——
 *  我们测的是「模块能加载、监听器已注册」，不是 IDB 语义。 */
// @ts-ignore
globalThis.indexedDB = {
  open() {
    /** @type {any} */
    const req = { result: null, error: null, onsuccess: null, onerror: null, onupgradeneeded: null };
    // 刻意不触发回调：让 openDb() 的 promise 悬着，模块加载不受影响
    return req;
  },
};
// @ts-ignore
globalThis.IDBKeyRange = { bound: (/** @type {any} */ a, /** @type {any} */ b) => ({ a, b }) };

// @ts-ignore
globalThis.MediaRecorder = class {
  static isTypeSupported() { return true; }
};

// @ts-ignore
globalThis.AudioContext = class {
  constructor() { this.state = 'running'; this.sampleRate = 48000; }
  async resume() {}
  createMediaStreamSource() { return { connect() {}, disconnect() {} }; }
  createGain() { return { gain: { value: 1 }, connect() {}, disconnect() {} }; }
  createAnalyser() {
    return { fftSize: 2048, connect() {}, disconnect() {}, getFloatTimeDomainData() {} };
  }
};

// Node 24 的 globalThis.navigator 是只读 getter，不能直接赋值，
// 必须用 defineProperty 覆盖。offscreen 只用到 mediaDevices.getUserMedia。
Object.defineProperty(globalThis, 'navigator', {
  configurable: true,
  writable: true,
  value: { mediaDevices: { getUserMedia: async () => ({ getAudioTracks: () => [] }) } },
});

// @ts-ignore
globalThis.URL.createObjectURL = globalThis.URL.createObjectURL ?? (() => 'blob:stub');
// @ts-ignore
globalThis.URL.revokeObjectURL = globalThis.URL.revokeObjectURL ?? (() => {});

// 顶层 import 本身就是测试
await import('../src/offscreen/offscreen.js');

test('offscreen 文档能被加载且不抛错', () => {
  assert.ok(true);
});

test('★ offscreen 注册了 onMessage 监听器', () => {
  // 这是最关键的一条：没有它，SW 的 START_ONE 会石沉大海直到 8 秒超时，
  // 用户看到的是「开始录制转半天然后失败」，且没有明显报错。
  assert.ok(
    (stub.listeners['runtime.onMessage'] ?? []).length > 0,
    'offscreen 未注册 onMessage —— 所有录制指令都会超时',
  );
});

test('PING 能得到回复（对账链路可用）', async () => {
  const handlers = stub.listeners['runtime.onMessage'];
  /** @type {any} */
  let reply = null;
  const returned = handlers[0](
    { v: 1, to: 'offscreen', type: 'OFFSCREEN_PING' },
    {},
    (/** @type {any} */ r) => { reply = r; },
  );

  // 异步回复的约定：监听器必须返回 true
  assert.equal(returned, true, '异步回复的监听器必须返回 true，否则回复会被丢弃');

  await new Promise((r) => setTimeout(r, 20));
  assert.ok(reply?.ok, 'PING 应返回 ok');
  assert.ok(Array.isArray(reply.tabs), 'PING 应返回 tabs 数组');
});

test('★ 不是发给 offscreen 的消息必须 return false', () => {
  const handlers = stub.listeners['runtime.onMessage'];
  const returned = handlers[0](
    { v: 1, to: 'sw', type: 'UI_GET_SNAPSHOT' }, // 发给 SW 的，不是给我们的
    {},
    () => {},
  );
  // 返回 true 会让 Chrome 一直等一个永不到来的回复，
  // 并且把真正目标上下文的回复吞掉 —— 这是本模式最常见的 bug
  assert.notEqual(
    returned, true,
    '非目标消息返回 true 会破坏 sendResponse 路由，吞掉真正处理者的回复',
  );
});

test('未知消息类型返回错误而不是抛出', async () => {
  const handlers = stub.listeners['runtime.onMessage'];
  /** @type {any} */
  let reply = null;
  handlers[0](
    { v: 1, to: 'offscreen', type: 'NOT_A_REAL_TYPE' },
    {},
    (/** @type {any} */ r) => { reply = r; },
  );
  await new Promise((r) => setTimeout(r, 20));
  assert.equal(reply?.ok, false);
  assert.ok(reply?.error, '未知消息应返回结构化错误');
});

test('STOP_ONE 对不存在的录制是安全的（幂等）', async () => {
  const handlers = stub.listeners['runtime.onMessage'];
  /** @type {any} */
  let reply = null;
  handlers[0](
    { v: 1, to: 'offscreen', type: 'OFFSCREEN_STOP_ONE', tabId: 12345, reason: 'test' },
    {},
    (/** @type {any} */ r) => { reply = r; },
  );
  await new Promise((r) => setTimeout(r, 20));
  // 重复停止、停止不存在的录制都不该报错 —— 竞态下这很常见
  assert.equal(reply?.ok, true);
});
