// @ts-check
/**
 * 录制中的会话在 offscreen 里的保护。
 *
 * 1. 恢复保存 / 丢弃只能作用于真正中断的录音。正在录制的标签页，其分块同样以
 *    「未保存」状态留在 IndexedDB 里；误删后收尾只剩之后的分块，文件缺少 WebM 头部。
 * 2. 分块写入失败时必须停止捕获并保存已录部分，不能留下无法停止的孤儿录制。
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { installChromeStub } from './helpers/chrome-stub.js';
import { installIndexedDbStub } from './helpers/idb-stub.js';

const stub = installChromeStub({ context: 'offscreen' });
const idb = installIndexedDbStub();

// @ts-ignore
globalThis.MediaRecorder = class {
  static isTypeSupported() { return true; }
  constructor() { this.state = 'inactive'; this.mimeType = 'audio/webm;codecs=opus'; }
  start() { this.state = 'recording'; recorders.push(this); }
  requestData() {}
  stop() { this.state = 'inactive'; }
};
/** @type {any[]} */
const recorders = [];
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
let trackStops = 0;
const track = { readyState: 'live', addEventListener() {}, stop() { trackStops += 1; } };
Object.defineProperty(globalThis, 'navigator', {
  configurable: true,
  writable: true,
  value: {
    mediaDevices: {
      getUserMedia: async () => ({ getAudioTracks: () => [track], getTracks: () => [track] }),
    },
  },
});

await import('../src/offscreen/offscreen.js');
const store = await import('../src/common/chunk-store.js');

const sleep = () => new Promise((resolve) => setTimeout(resolve, 20));
const opts = { sessionStamp: 'stamp', monitor: false, timesliceMs: 1000 };
const meta = { title: '会议', url: 'https://example.com/', index: 1 };

/** @param {string} type @param {object} payload */
function send(type, payload) {
  return new Promise((resolve) => {
    stub.listeners['runtime.onMessage'][0]({ v: 1, to: 'offscreen', type, ...payload }, {}, resolve);
  });
}

test('录制中的会话拒绝恢复保存和丢弃，已录分块保持完整', async () => {
  const started = await send('OFFSCREEN_START_ONE', {
    tabId: 7, streamId: 'stream', meta, opts: { ...opts, sessionId: 'live-session' },
  });
  assert.equal(/** @type {any} */ (started).ok, true);
  recorders[0].ondataavailable({ data: new Blob(['webm-header']) });
  await sleep();

  for (const type of ['OFFSCREEN_RECOVER_DISCARD', 'OFFSCREEN_RECOVER_SAVE']) {
    const reply = /** @type {any} */ (await send(type, { sessionId: 'live-session', tabId: 7 }));
    assert.equal(reply.ok, false, type);
  }
  assert.equal((await store.readChunks('live-session', 7)).length, 1);
  assert.equal((await store.getMeta('live-session', 7))?.status, 'recording');
  await send('OFFSCREEN_DISCARD', { tabId: 7 });
});

test('分块写入失败时停止捕获并保存已录分块，而不是留下无法停止的录制', async () => {
  await send('OFFSCREEN_START_ONE', {
    tabId: 8, streamId: 'stream', meta, opts: { ...opts, sessionId: 'store-error' },
  });
  const rec = recorders.at(-1);
  rec.ondataavailable({ data: new Blob(['webm-header']) });
  await sleep();
  const before = trackStops;
  const transaction = idb.db.transaction;
  idb.db.transaction = () => {
    idb.db.transaction = transaction;
    throw new Error('QuotaExceededError');
  };
  rec.ondataavailable({ data: new Blob(['lost-chunk']) });
  await sleep();
  assert.equal(rec.state, 'inactive', 'MediaRecorder 应被停止');
  rec.onstop(); // 浏览器在 stop() 之后触发 onstop
  await sleep();
  assert.ok(trackStops > before, '捕获的音轨应被释放');
  const ping = /** @type {any} */ (await send('OFFSCREEN_PING', {}));
  assert.equal(ping.tabs.some((/** @type {any} */ t) => t.tabId === 8), false);
  const download = stub.callsTo('runtime.sendMessage')
    .find((c) => c.args[0].type === 'SW_EVENT_REQUEST_DOWNLOAD' && c.args[0].tabId === 8);
  assert.ok(download, '已录分块应被送去保存');
  // 下载终态后 SW 会通知回收 blob URL；这里同样回收，免得兜底计时器拖住测试进程
  await send('OFFSCREEN_REVOKE_URL', { blobUrl: download.args[0].blobUrl });
});
