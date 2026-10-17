// @ts-check
/**
 * chunk-store 的测试 —— 守住「录了几小时的数据不会丢」这件事。
 *
 * 这个模块此前完全没有测试，而它承载的恰恰是最不可接受丢失的东西。
 * 重点验证三件事：
 *
 * 1. **分块顺序**：读回的顺序必须与写入的 seq 顺序一致，否则拼接出的音频是乱的。
 * 2. **键范围隔离**：清理某个标签页的分块，绝不能误删相邻 tabId / sessionId 的数据。
 * 3. **可恢复判定**：什么样的记录算「未保存、可恢复」。
 *
 * 第 2 条依赖 IDB 的键序规则（数组排在数字之后），所以 tests/helpers/idb-stub.js
 * 实现了真实的键比较算法 —— 否则测试会通过而线上失败。
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { installIndexedDbStub, compareKeys } from './helpers/idb-stub.js';

const idb = installIndexedDbStub();

const store = await import('../src/common/chunk-store.js');

/** @param {number} size */
const blob = (size) => new Blob([new Uint8Array(size)]);

/** @param {string} sessionId @param {number} tabId */
async function beginOne(sessionId, tabId, title = '测试') {
  return store.begin({
    sessionId, tabId, sessionStamp: '2026-08-28_14-30',
    title, url: 'https://example.com/', index: 1,
    startedAt: Date.now(), mimeType: 'audio/webm;codecs=opus',
  });
}

// ───────────────────────── 键序（桩的正确性前提） ─────────────────────────

test('键序规则：短数组在前，数组排在数字之后', () => {
  // 这两条是 chunk-store 的删除范围 [s,t] .. [s,t,[]] 能覆盖所有 seq 的依据
  assert.ok(compareKeys(['s', 1], ['s', 1, 0]) < 0, '短数组应在前');
  assert.ok(compareKeys(['s', 1, 999999], ['s', 1, []]) < 0, '数字应排在数组之前');
  assert.ok(compareKeys(['s', 1, 0], ['s', 1, 1]) < 0, 'seq 应按数值排序');
  assert.equal(compareKeys(['s', 1, 5], ['s', 1, 5]), 0);
});

// ───────────────────────────── 基本读写 ─────────────────────────────

test('begin 建立元数据，状态为 recording', async () => {
  idb.reset();
  const meta = await beginOne('sess-a', 1);
  assert.equal(meta.status, 'recording');
  assert.equal(meta.chunks, 0);
  assert.equal(meta.bytes, 0);

  const read = await store.getMeta('sess-a', 1);
  assert.ok(read);
  assert.equal(read.title, '测试');
});

test('★ 分块按 seq 顺序读回（顺序错了音频就是乱的）', async () => {
  idb.reset();
  await beginOne('sess-b', 1);

  // 刻意乱序写入，模拟并发写落定顺序不确定的情况
  await store.append('sess-b', 1, 2, blob(30));
  await store.append('sess-b', 1, 0, blob(10));
  await store.append('sess-b', 1, 1, blob(20));

  const chunks = await store.readChunks('sess-b', 1);
  assert.equal(chunks.length, 3);
  // 大小按 10/20/30 递增，可用来断言顺序
  assert.deepEqual(chunks.map((c) => c.size), [10, 20, 30], '必须按 seq 升序返回');
});

test('append 累加字节数与分块数', async () => {
  idb.reset();
  await beginOne('sess-c', 1);
  const a = await store.append('sess-c', 1, 0, blob(100));
  assert.equal(a.chunks, 1);
  assert.equal(a.bytes, 100);

  const b = await store.append('sess-c', 1, 1, blob(50));
  assert.equal(b.chunks, 2);
  assert.equal(b.bytes, 150);

  const meta = await store.getMeta('sess-c', 1);
  assert.equal(meta?.bytes, 150);
});

// ─────────────────── 键范围隔离（误删是灾难性的） ───────────────────

test('★ discard 只删目标标签页，不碰相邻 tabId', async () => {
  idb.reset();
  await beginOne('sess-d', 1);
  await beginOne('sess-d', 2);
  await store.append('sess-d', 1, 0, blob(10));
  await store.append('sess-d', 1, 1, blob(10));
  await store.append('sess-d', 2, 0, blob(99));

  await store.discard('sess-d', 1);

  assert.equal((await store.readChunks('sess-d', 1)).length, 0, 'tab 1 应被清空');
  assert.equal(await store.getMeta('sess-d', 1), undefined, 'tab 1 元数据应被删');

  const kept = await store.readChunks('sess-d', 2);
  assert.equal(kept.length, 1, 'tab 2 的分块必须完好');
  assert.equal(kept[0].size, 99);
  assert.ok(await store.getMeta('sess-d', 2), 'tab 2 元数据必须完好');
});

test('★ discard 不跨 sessionId 误删', async () => {
  idb.reset();
  await beginOne('sess-e1', 1);
  await beginOne('sess-e2', 1);
  await store.append('sess-e1', 1, 0, blob(10));
  await store.append('sess-e2', 1, 0, blob(77));

  await store.discard('sess-e1', 1);

  assert.equal((await store.readChunks('sess-e1', 1)).length, 0);
  const kept = await store.readChunks('sess-e2', 1);
  assert.equal(kept.length, 1, '另一个会话的数据必须完好');
  assert.equal(kept[0].size, 77);
});

test('discard 覆盖大 seq（键范围上界必须包含所有数字）', async () => {
  idb.reset();
  await beginOne('sess-f', 1);
  // 数小时录制、1 秒一块 → seq 可达上万
  for (const seq of [0, 1, 3600, 99999]) {
    await store.append('sess-f', 1, seq, blob(1));
  }
  assert.equal((await store.readChunks('sess-f', 1)).length, 4);

  await store.discard('sess-f', 1);
  assert.equal((await store.readChunks('sess-f', 1)).length, 0, '大 seq 也必须被清理');
});

// ───────────────────────── 可恢复判定 ─────────────────────────

test('★ 有分块且未保存 → 判定为可恢复', async () => {
  idb.reset();
  await beginOne('sess-g', 1, '未保存的录音');
  await store.append('sess-g', 1, 0, blob(1000));

  const list = await store.listRecoverable();
  assert.equal(list.length, 1);
  assert.equal(list[0].tabId, 1);
  assert.equal(list[0].title, '未保存的录音');
  assert.equal(list[0].bytes, 1000);
});

test('已标记 saved 的不算可恢复', async () => {
  idb.reset();
  await beginOne('sess-h', 1);
  await store.append('sess-h', 1, 0, blob(10));
  await store.setStatus('sess-h', 1, 'saved');

  assert.equal((await store.listRecoverable()).length, 0);
});

test('complete 但未保存的仍算可恢复（下载失败的情形）', async () => {
  // 这正是「保存报错」后的状态：finalize 置为 complete，但下载没成功
  idb.reset();
  await beginOne('sess-i', 1);
  await store.append('sess-i', 1, 0, blob(10));
  await store.setStatus('sess-i', 1, 'complete');

  const list = await store.listRecoverable();
  assert.equal(list.length, 1, '收尾完成但未保存的录音必须可恢复');
  assert.equal(list[0].status, 'complete');
});

test('零分块的记录不算可恢复（没有内容可救）', async () => {
  idb.reset();
  await beginOne('sess-j', 1);
  assert.equal((await store.listRecoverable()).length, 0);
});

test('pruneSaved 清理已保存的残留，保留未保存的', async () => {
  idb.reset();
  await beginOne('sess-k', 1);
  await store.append('sess-k', 1, 0, blob(10));
  await store.setStatus('sess-k', 1, 'saved');

  await beginOne('sess-k', 2);
  await store.append('sess-k', 2, 0, blob(20));

  await store.pruneSaved();

  assert.equal(await store.getMeta('sess-k', 1), undefined, '已保存的应被清理');
  assert.ok(await store.getMeta('sess-k', 2), '未保存的必须保留');
});

test('多个会话的可恢复记录都能列出', async () => {
  idb.reset();
  await beginOne('sess-m1', 1, '录音A');
  await store.append('sess-m1', 1, 0, blob(10));
  await beginOne('sess-m2', 5, '录音B');
  await store.append('sess-m2', 5, 0, blob(20));

  const list = await store.listRecoverable();
  assert.equal(list.length, 2);
  assert.deepEqual(
    list.map((m) => m.title).sort(),
    ['录音A', '录音B'],
  );
});
