// @ts-check
/**
 * 下载委托链路的测试。
 *
 * ## 来由
 *
 * 线上故障：每次录制的保存都失败，报
 * `Cannot read properties of undefined (reading 'onChanged')`。
 * 根因是 `chrome.downloads` 在 offscreen 文档里不存在。
 *
 * 修复后的分工是两个上下文限制夹出来的唯一解：
 *
 * ```
 *   offscreen: URL.createObjectURL ──REQUEST_DOWNLOAD──> SW: downloads.download
 *   offscreen: URL.revokeObjectURL <──REVOKE_URL──────── SW: 下载到达终态
 * ```
 *
 * 这里验证 SW 侧那一半：收到委托后确实调了 downloads.download、
 * 参数正确、文件名被拒时会用 ASCII 兜底名重试、终态后会通知回收。
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { installChromeStub } from './helpers/chrome-stub.js';

const stub = installChromeStub(); // SW 上下文：完整 API
await import('../src/sw/service-worker.js');

/** @param {any} message */
async function send(message) {
  const handlers = stub.listeners['runtime.onMessage'];
  /** @type {any} */
  let reply = null;
  handlers[0](message, {}, (/** @type {any} */ r) => { reply = r; });
  await new Promise((r) => setTimeout(r, 60));
  return reply;
}

const BLOB_URL = 'blob:chrome-extension://stub/abc-123';
const FILENAME = '标签页录音/2026-08-28_14-30/01-测试标题-143052.mkv';
const FALLBACK = 'tab-recordings/2026-08-28_14-30/01-rec-143052.mkv';

/** @param {object} [over] */
function downloadRequest(over = {}) {
  return {
    v: 1, to: 'sw', type: 'SW_EVENT_REQUEST_DOWNLOAD',
    blobUrl: BLOB_URL, filename: FILENAME, fallbackFilename: FALLBACK,
    tabId: 7, bytes: 12345,
    ...over,
  };
}

test('★ SW 收到下载委托后会调用 chrome.downloads.download', async () => {
  stub.reset();
  const reply = await send(downloadRequest());

  assert.equal(reply?.ok, true, `下载应成功启动，实际 ${JSON.stringify(reply)}`);
  assert.equal(typeof reply.downloadId, 'number');

  const calls = stub.callsTo('downloads.download');
  assert.equal(calls.length, 1, '应恰好调用一次 downloads.download');

  const opts = calls[0].args[0];
  assert.equal(opts.url, BLOB_URL, '必须使用 offscreen 传来的 blob URL');
  assert.equal(opts.filename, FILENAME);
  assert.equal(opts.saveAs, false, 'saveAs 必须为 false —— 否则 N 个文件弹 N 次对话框');
  assert.equal(opts.conflictAction, 'uniquify', '重名应交给 Chrome 去重');
});

test('缺少 blob URL 时返回结构化错误而不是抛出', async () => {
  stub.reset();
  const reply = await send(downloadRequest({ blobUrl: undefined }));
  assert.equal(reply?.ok, false);
  assert.equal(reply?.error?.code, 'E_DOWNLOAD');
});

test('★ 主文件名被拒时用 ASCII 兜底名重试（绝不因文件名丢音频）', async () => {
  stub.reset();

  // 模拟 Chrome 拒绝含 CJK 的主文件名
  const original = stub.chrome.downloads.download;
  let attempt = 0;
  // @ts-ignore —— 测试期替换
  stub.chrome.downloads.download = (/** @type {any} */ o, /** @type {Function} */ cb) => {
    attempt += 1;
    stub.calls.push({ api: 'downloads.download', args: [o] });
    if (attempt === 1) {
      // 真实 API 是通过 lastError 报错的
      stub.chrome.runtime.lastError = { message: 'Invalid filename' };
      cb(undefined);
      stub.chrome.runtime.lastError = undefined;
    } else {
      cb(99);
    }
  };

  const reply = await send(downloadRequest());

  // @ts-ignore —— 还原
  stub.chrome.downloads.download = original;

  assert.equal(reply?.ok, true, '兜底名应能成功');
  assert.equal(reply?.usedFallback, true, '应标记使用了兜底名');
  assert.equal(reply?.filename, FALLBACK);

  const calls = stub.callsTo('downloads.download');
  assert.equal(calls.length, 2, '应尝试两次：主名 + 兜底名');
  assert.equal(calls[0].args[0].filename, FILENAME);
  assert.equal(calls[1].args[0].filename, FALLBACK);
});

test('两次都失败时返回错误（并且不会静默）', async () => {
  stub.reset();
  const original = stub.chrome.downloads.download;
  // @ts-ignore
  stub.chrome.downloads.download = (/** @type {any} */ o, /** @type {Function} */ cb) => {
    stub.calls.push({ api: 'downloads.download', args: [o] });
    stub.chrome.runtime.lastError = { message: 'Disk full' };
    cb(undefined);
    stub.chrome.runtime.lastError = undefined;
  };

  const reply = await send(downloadRequest());

  // @ts-ignore
  stub.chrome.downloads.download = original;

  assert.equal(reply?.ok, false);
  assert.equal(reply?.error?.code, 'E_DOWNLOAD');
  assert.match(String(reply?.error?.message), /Disk full/);
});

// ─────────────── blob URL 回收时机（提前回收会截断文件） ───────────────

test('★ 下载完成后才通知 offscreen 回收 blob URL', async () => {
  stub.reset();
  const reply = await send(downloadRequest());
  const downloadId = reply.downloadId;

  // 下载进行中：**绝不能**回收，否则文件被截断
  stub.reset();
  stub.fire('downloads.onChanged', { id: downloadId, state: { current: 'in_progress' } });
  await new Promise((r) => setTimeout(r, 20));
  let revokes = stub.callsTo('runtime.sendMessage')
    .filter((c) => c.args[0]?.type === 'OFFSCREEN_REVOKE_URL');
  assert.equal(revokes.length, 0, '下载进行中绝不能回收 —— 会截断文件');

  // 到达终态：应通知回收
  stub.reset();
  stub.fire('downloads.onChanged', { id: downloadId, state: { current: 'complete' } });
  await new Promise((r) => setTimeout(r, 20));
  revokes = stub.callsTo('runtime.sendMessage')
    .filter((c) => c.args[0]?.type === 'OFFSCREEN_REVOKE_URL');
  assert.equal(revokes.length, 1, '完成后应通知 offscreen 回收');
  assert.equal(revokes[0].args[0].blobUrl, BLOB_URL, '应回收正确的那个 URL');
});

test('下载被中断也要回收（否则 URL 永久泄漏）', async () => {
  stub.reset();
  const reply = await send(downloadRequest());

  stub.reset();
  stub.fire('downloads.onChanged', {
    id: reply.downloadId,
    state: { current: 'interrupted' },
    error: { current: 'USER_CANCELED' },
  });
  await new Promise((r) => setTimeout(r, 20));

  const revokes = stub.callsTo('runtime.sendMessage')
    .filter((c) => c.args[0]?.type === 'OFFSCREEN_REVOKE_URL');
  assert.equal(revokes.length, 1, '中断同样要回收');
});

test('不认识的 downloadId 不触发回收（别人的下载与我们无关）', async () => {
  stub.reset();
  stub.fire('downloads.onChanged', { id: 999999, state: { current: 'complete' } });
  await new Promise((r) => setTimeout(r, 20));

  const revokes = stub.callsTo('runtime.sendMessage')
    .filter((c) => c.args[0]?.type === 'OFFSCREEN_REVOKE_URL');
  assert.equal(revokes.length, 0, '不应对无关下载做任何动作');
});

test('同一个 downloadId 的终态事件重复到达时只回收一次', async () => {
  stub.reset();
  const reply = await send(downloadRequest());

  stub.reset();
  stub.fire('downloads.onChanged', { id: reply.downloadId, state: { current: 'complete' } });
  stub.fire('downloads.onChanged', { id: reply.downloadId, state: { current: 'complete' } });
  await new Promise((r) => setTimeout(r, 20));

  const revokes = stub.callsTo('runtime.sendMessage')
    .filter((c) => c.args[0]?.type === 'OFFSCREEN_REVOKE_URL');
  assert.equal(revokes.length, 1, '重复事件不应重复回收');
});
