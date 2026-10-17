import test from 'node:test';
import assert from 'node:assert/strict';
import { installChromeStub } from './helpers/chrome-stub.js';
import { installIndexedDbStub } from './helpers/idb-stub.js';

const stub = installChromeStub();
installIndexedDbStub();
const registry = await import('../src/sw/registry.js');
const { UI, EV, msg } = await import('../src/common/protocol.js');
const { importDownload, sendToTingji, HOST } = await import('../src/sw/tingji.js');
await import('../src/sw/service-worker.js');
const native = stub.chrome.runtime.sendNativeMessage;
const sleep = () => new Promise((resolve) => setTimeout(resolve, 25));
const downloads = new Map();
stub.chrome.downloads.search = async ({ id }) => downloads.has(id) ? [downloads.get(id)] : [];
let nextId = 500;

function send(type, payload = {}) {
  return new Promise((resolve) => {
    const handler = stub.listeners['runtime.onMessage'][0];
    assert.equal(handler(msg('sw', type, payload), {}, resolve), true);
  });
}

async function saved({ complete = true, recover = false } = {}) {
  const tabId = nextId++;
  const downloadId = tabId + 1000;
  downloads.set(downloadId, {
    id: downloadId, state: complete ? 'complete' : 'in_progress', exists: true,
    filename: `/Users/test/Downloads/标签页录音/中文录音 (${tabId}).mkv`,
  });
  registry.upsert(tabId, { state: 'saving', title: '测试录音' });
  stub.chrome.downloads.download = (opts, callback) => callback(downloadId);
  const reply = await send(EV.REQUEST_DOWNLOAD, {
    tabId, blobUrl: `blob:stub/${tabId}`, filename: '标签页录音/中文录音.mkv',
    fallbackFilename: 'tab-recordings/recording.mkv',
  });
  assert.equal(reply.downloadId, downloadId);
  if (!recover) await send(EV.SAVED, { tabId, downloadId, filename: reply.filename, bytes: 80 });
  await sleep();
  return { tabId, downloadId };
}

test('面板 ping 通过原生宿主，不发送录音', async () => {
  const response = await send(UI.PING_TINGJI);
  assert.equal(response.ok, true);
  assert.deepEqual(stub.callsTo('runtime.sendNativeMessage').at(-1).args, [HOST, { type: 'ping' }]);
});

test('自动发送默认关闭，手动发送使用 Chrome 返回的最终绝对路径', async () => {
  const { tabId, downloadId } = await saved();
  assert.equal((await send(UI.GET_PREFS)).prefs.autoTingji, false);
  assert.equal(registry.get(tabId).downloadId, downloadId);
  assert.equal(registry.get(tabId).tingji, undefined);
  assert.equal((await send(UI.SEND_TO_TINGJI, { tabId })).ok, true);
  assert.deepEqual(stub.callsTo('runtime.sendNativeMessage').at(-1).args,
    [HOST, { type: 'import', path: downloads.get(downloadId).filename }]);
  assert.equal(registry.get(tabId).tingji, 'queued');
});

test('重复手动发送显示复用；打开听记经宿主处理', async () => {
  const { tabId } = await saved();
  stub.chrome.runtime.sendNativeMessage = async () => ({ ok: true, reused: true, id: 'job' });
  await send(UI.SEND_TO_TINGJI, { tabId });
  assert.equal(registry.get(tabId).tingji, 'reused');
  assert.match(registry.get(tabId).tingjiMessage, /已有相同录音/);
  stub.chrome.runtime.sendNativeMessage = native;
  await send(UI.OPEN_TINGJI);
  assert.deepEqual(stub.callsTo('runtime.sendNativeMessage').at(-1).args, [HOST, { type: 'open' }]);
});

test('未安装宿主时显示中文安装提示，恢复连接后可重试', async () => {
  const { tabId } = await saved();
  stub.chrome.runtime.sendNativeMessage = async () => {
    throw new Error('Specified native messaging host not found.');
  };
  const result = await send(UI.SEND_TO_TINGJI, { tabId });
  assert.equal(result.ok, false);
  assert.match(result.error, /未安装听记连接器.*install_browser_extension.py/);
  assert.equal(registry.get(tabId).tingji, 'error');
  stub.chrome.runtime.sendNativeMessage = native;
  await send(UI.SEND_TO_TINGJI, { tabId });
  assert.equal(registry.get(tabId).tingji, 'queued');
});

test('下载未完成、文件被删或下载记录不存在时拒绝发送', async () => {
  const { downloadId } = await saved({ complete: false });
  stub.reset();
  assert.match((await importDownload(downloadId)).error, /尚未下载完成/);
  downloads.get(downloadId).state = 'complete';
  downloads.get(downloadId).exists = false;
  assert.match((await importDownload(downloadId)).error, /找不到/);
  assert.match((await importDownload(-1)).error, /找不到/);
  assert.equal(stub.callsTo('runtime.sendNativeMessage').length, 0);
});

test('自动发送只在下载 complete 后发生，重复终态事件不重复提交', async () => {
  await send(UI.SET_PREF, { key: 'autoTingji', value: true });
  assert.equal((await stub.chrome.storage.local.get()).autoTingji, true);
  const { tabId, downloadId } = await saved({ complete: false });
  stub.reset();
  assert.equal(registry.get(tabId).tingji, undefined);
  stub.fire('downloads.onChanged', { id: downloadId, state: { current: 'in_progress' } });
  await sleep();
  assert.equal(stub.callsTo('runtime.sendNativeMessage').length, 0);
  downloads.get(downloadId).state = 'complete';
  stub.fire('downloads.onChanged', { id: downloadId, state: { current: 'complete' } });
  stub.fire('downloads.onChanged', { id: downloadId, state: { current: 'complete' } });
  await sleep();
  assert.equal(registry.get(tabId).tingji, 'queued');
  assert.equal(stub.callsTo('runtime.sendNativeMessage').length, 1);
  await send(UI.SET_PREF, { key: 'autoTingji', value: false });
});

test('发送中状态立即可见，并发点击合并成一次原生调用', async () => {
  const { tabId } = await saved();
  let finish;
  let count = 0;
  stub.chrome.runtime.sendNativeMessage = () => {
    count++;
    return new Promise((resolve) => { finish = resolve; });
  };
  const first = send(UI.SEND_TO_TINGJI, { tabId });
  await sleep();
  assert.equal(registry.get(tabId).tingji, 'sending');
  const second = send(UI.SEND_TO_TINGJI, { tabId });
  await sleep();
  assert.equal(count, 1);
  finish({ ok: true, reused: false });
  assert.equal((await first).ok, true);
  assert.equal((await second).ok, true);
  stub.chrome.runtime.sendNativeMessage = native;
});

test('恢复保存使用相同下载路径，并能自动送入听记', async () => {
  await send(UI.SET_PREF, { key: 'autoTingji', value: true });
  const sendMessage = stub.chrome.runtime.sendMessage;
  let recovered;
  stub.chrome.runtime.sendMessage = async (message) => {
    if (message.type === 'OFFSCREEN_RECOVER_SAVE') {
      recovered = await saved({ recover: true });
      return { ok: true, filename: '恢复录音.mkv', downloadId: recovered.downloadId };
    }
    return { ok: true };
  };
  // The original tab can already be closed; recover creates a DONE row for its recording.
  const reply = await send(UI.RECOVER_SAVE, { tabId: nextId, sessionId: 'recovered-session' });
  await sleep();
  assert.equal(reply.ok, true);
  assert.equal(registry.get(recovered.tabId).downloadId, recovered.downloadId);
  assert.equal(registry.get(recovered.tabId).state, 'done');
  assert.equal(registry.get(recovered.tabId).tingji, 'queued');
  stub.chrome.runtime.sendMessage = sendMessage;
  await send(UI.SET_PREF, { key: 'autoTingji', value: false });
});

test('worker 丢失内存下载表后，持久记录仍能在下载完成时自动发送', async () => {
  await send(UI.SET_PREF, { key: 'autoTingji', value: true });
  const tabId = nextId++;
  const downloadId = tabId + 1000;
  registry.upsert(tabId, { state: 'done', downloadId, downloadComplete: false });
  downloads.set(downloadId, { filename: '/Users/test/Downloads/唤醒.mkv', state: 'complete' });
  stub.fire('downloads.onChanged', { id: downloadId, state: { current: 'complete' } });
  await sleep();
  assert.equal(registry.get(tabId).tingji, 'queued');
  stub.reset();
  stub.fire('downloads.onChanged', { id: 999999, state: { current: 'complete' } });
  await sleep();
  assert.equal(stub.callsTo('runtime.sendNativeMessage').length, 0);
  await send(UI.SET_PREF, { key: 'autoTingji', value: false });
});

test('下载中断时不发送，关闭来源标签页后仍保留已保存录音', async () => {
  const { tabId, downloadId } = await saved({ complete: false });
  stub.fire('downloads.onChanged', { id: downloadId, state: { current: 'interrupted' } });
  await sleep();
  assert.equal(registry.get(tabId).tingji, 'error');
  await Promise.all(stub.fire('tabs.onRemoved', tabId));
  assert.equal(registry.get(tabId).downloadId, downloadId);
});

test('原生回复无效和连接故障返回中文错误', async () => {
  stub.chrome.runtime.sendNativeMessage = async () => undefined;
  assert.match((await sendToTingji({ type: 'ping' })).error, /回复无效/);
  stub.chrome.runtime.sendNativeMessage = async () => { throw new Error('broken pipe'); };
  assert.match((await sendToTingji({ type: 'ping' })).error, /无法连接听记/);
  stub.chrome.runtime.sendNativeMessage = native;
});


test('再次点击拓展图标或来源跨源跳转，仍保留录音发送入口', async () => {
  const { tabId } = await saved();
  await Promise.all(stub.fire('action.onClicked', {
    id: tabId, windowId: 1, title: '测试录音', url: 'https://example.com/',
  }));
  await sleep();
  assert.equal(registry.get(tabId).state, 'done');
  await Promise.all(stub.fire('tabs.onUpdated', tabId, { url: 'https://other.example/' }, {}));
  assert.equal(registry.get(tabId).state, 'done');
  assert.equal(registry.get(tabId).origin, '');
  assert.equal((await send(UI.SEND_TO_TINGJI, { tabId })).ok, true);
});

test('来源标签页已关闭时，全部开始不会抹掉已保存录音的发送入口', async () => {
  const { tabId, downloadId } = await saved();
  // The worker slept through tabs.onRemoved: only tabs.get knows the tab is gone.
  registry.upsert(tabId, { origin: 'https://example.com' });
  const tabsGet = stub.chrome.tabs.get;
  stub.chrome.tabs.get = async (id) => {
    if (id === tabId) throw new Error(`No tab with id: ${id}.`);
    return tabsGet(id);
  };
  try {
    await send(UI.START_ALL);
    await send(UI.START, { tabIds: [tabId] });
  } finally {
    stub.chrome.tabs.get = tabsGet;
  }
  assert.equal(registry.get(tabId).state, 'done');
  assert.equal(registry.get(tabId).downloadId, downloadId);
  assert.equal((await send(UI.SEND_TO_TINGJI, { tabId })).ok, true);
  await Promise.all(stub.fire('tabs.onRemoved', tabId));
  assert.equal(registry.get(tabId).origin, '');
  assert.equal(registry.armedTabIds().includes(tabId), false);
  assert.equal(registry.get(tabId).downloadId, downloadId);
});

test('授权已失效的已保存录音不参与全部开始，单独开始也不清掉录音', async () => {
  const { tabId, downloadId } = await saved();
  registry.upsert(tabId, { origin: 'https://example.com' });
  await Promise.all(stub.fire('tabs.onUpdated', tabId, { url: 'https://other.example/' }, {}));
  assert.equal(registry.armedTabIds().includes(tabId), false);
  const reply = await send(UI.START, { tabIds: [tabId] });
  assert.equal(reply.results[0].ok, false);
  assert.equal(reply.results[0].error.code, 'E_GRANT_LOST');
  assert.equal(registry.get(tabId).state, 'done');
  assert.equal(registry.get(tabId).downloadId, downloadId);
});

test('正在录制的标签页不会出现在「未保存的录音」恢复列表里', async () => {
  const store = await import('../src/common/chunk-store.js');
  const live = nextId++;
  const crashed = nextId++;
  for (const tabId of [live, crashed]) {
    await store.begin({ sessionId: `s-${tabId}`, tabId, sessionStamp: 'stamp', title: '录音',
      url: 'https://example.com/', index: 1, startedAt: Date.now(), mimeType: 'audio/webm' });
    await store.append(`s-${tabId}`, tabId, 0, new Blob(['header']));
  }
  registry.upsert(live, { state: 'recording', origin: 'https://example.com' });
  const listed = (await send(UI.GET_SNAPSHOT)).snapshot.recoverable.map((r) => r.tabId);
  assert.equal(listed.includes(live), false);
  assert.equal(listed.includes(crashed), true);
});
