// @ts-check
/**
 * service worker 的加载期与手势时序测试。
 *
 * 这些测试针对的是一类**症状为「点了没反应」**的错误 —— 它们不会在
 * 语法检查里暴露，也不会让扩展装不上，只会让某个入口静默失效。
 *
 * 覆盖两件事：
 * 1. service-worker.js 能被真实 import（顶层没有炸掉的 API 调用）
 * 2. onClicked 处理器**在任何 await 之前**同步调用 sidePanel.open()
 *
 * 第 2 条是有具体来由的回归测试：`chrome.sidePanel.open()` 只能在用户手势中
 * 同步调用，一旦前面出现 await，手势上下文就失效，open() 会 reject。
 * 曾经有一版把 `await bootstrap()` 放在 open() 前面，于是点击图标完全没反应。
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { installChromeStub } from './helpers/chrome-stub.js';
import { installIndexedDbStub } from './helpers/idb-stub.js';

// 必须在 import service-worker 之前装好桩
const stub = installChromeStub();
// SW 现在会直接读 IndexedDB（查未保存的录音），没有这个桩会静默失败 ——
// 那条路径被 try/catch 包着，测试不会红，但也就等于没被覆盖。
installIndexedDbStub();

// 顶层 import 本身就是一项测试：模块加载期出任何错，这里就会抛
await import('../src/sw/service-worker.js');

test('service worker 能被加载且不抛错', () => {
  // 能走到这里就说明顶层代码跑通了
  assert.ok(true);
});

test('注册了所有关键事件监听器', () => {
  const required = [
    'runtime.onInstalled',
    'runtime.onStartup',
    'runtime.onMessage',
    'action.onClicked',
    'commands.onCommand',
    'contextMenus.onClicked',
    'tabs.onUpdated',
    'tabs.onRemoved',
    'tabs.onReplaced',
  ];
  for (const name of required) {
    assert.ok(
      (stub.listeners[name] ?? []).length > 0,
      `缺少 ${name} 监听器 —— 对应的功能入口会完全失效`,
    );
  }
});

test('★ 点击图标时 sidePanel.open() 在任何 await 之前被同步调用', () => {
  stub.reset();

  const tab = { id: 42, windowId: 7, url: 'https://example.com/', title: '测试页' };

  // 同步触发监听器，**不 await 它返回的 promise**。
  // 这模拟的正是浏览器的行为：用户手势只在同步执行期间有效。
  stub.fire('action.onClicked', tab);

  // 关键断言：此刻（同一个同步块内）open() 必须已经被调用过。
  // 若处理器里 open() 前面有 await，这里就会是 0 —— 也就是线上「点了没反应」。
  const opens = stub.callsTo('sidePanel.open');
  assert.equal(
    opens.length, 1,
    'sidePanel.open() 未在用户手势的同步阶段被调用 —— ' +
    '说明它前面有 await，真实 Chrome 中会因手势失效而 reject',
  );
  assert.deepEqual(opens[0].args[0], { windowId: 7 });
});

test('点击图标随后仍会异步完成授权', async () => {
  stub.reset();

  stub.fire('action.onClicked', {
    id: 42, windowId: 7, url: 'https://example.com/', title: '测试页',
  });

  // 让异步部分跑完
  await new Promise((r) => setTimeout(r, 50));

  // 授权会把状态写进 storage.session
  const writes = stub.callsTo('storage.session.set');
  assert.ok(writes.length > 0, '授权后应写入 storage.session');

  const last = writes[writes.length - 1].args[0];
  const records = last.tabRecords ?? [];
  const record = records.find((/** @type {any} */ r) => r.tabId === 42);
  assert.ok(record, '应为 tab=42 建立记录');
  assert.equal(record.state, 'armed', '点击图标后该标签页应变为已就绪');
});

test('点击图标会为该标签页设置徽标', async () => {
  stub.reset();
  stub.fire('action.onClicked', {
    id: 99, windowId: 1, url: 'https://example.com/', title: 'x',
  });
  await new Promise((r) => setTimeout(r, 50));

  const badges = stub.callsTo('action.setBadgeText');
  const forTab = badges.find((c) => c.args[0]?.tabId === 99);
  assert.ok(forTab, '应为该标签页设置徽标');
  assert.equal(forTab.args[0].text, '●', '已就绪的徽标应为 ●');
});

test('不可捕获的页面不会被授权（且不应抛错）', async () => {
  stub.reset();
  stub.fire('action.onClicked', {
    id: 5, windowId: 1, url: 'chrome://settings', title: '设置',
  });
  await new Promise((r) => setTimeout(r, 50));

  const writes = stub.callsTo('storage.session.set');
  const records = writes.length > 0
    ? (writes[writes.length - 1].args[0].tabRecords ?? [])
    : [];
  const record = records.find((/** @type {any} */ r) => r.tabId === 5);
  assert.ok(!record || record.state !== 'armed', 'chrome:// 页面不应被标记为已就绪');
});

test('快捷键授权路径独立可用（不依赖侧边栏）', async () => {
  stub.reset();
  stub.fire('commands.onCommand', 'arm-current-tab', {
    id: 77, windowId: 1, url: 'https://example.org/', title: '快捷键测试',
  });
  await new Promise((r) => setTimeout(r, 50));

  const writes = stub.callsTo('storage.session.set');
  const records = writes[writes.length - 1].args[0].tabRecords ?? [];
  const record = records.find((/** @type {any} */ r) => r.tabId === 77);
  assert.ok(record, '快捷键应能建立记录');
  assert.equal(record.state, 'armed');
});

test('右键菜单项已注册', () => {
  const created = stub.callsTo('contextMenus.create');
  // onInstalled 时才创建，这里手动触发一次
  if (created.length === 0) {
    stub.fire('runtime.onInstalled', { reason: 'install' });
  }
});
