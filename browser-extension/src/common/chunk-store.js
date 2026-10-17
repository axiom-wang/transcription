// @ts-check
/**
 * IndexedDB 分块存储 —— 边录边落盘。
 *
 * ## 为什么放在 common/ 而不是 offscreen/
 *
 * 只依赖 `indexedDB` 和 `IDBKeyRange`，这两个在 **service worker 里同样可用**。
 * 放在 common 让 SW 能直接读「有哪些未保存的录音」，而不必先把 offscreen 文档拉起来。
 *
 * 这一点是必要的，不是优化：产生未保存录音的**主要原因就是扩展重载**，
 * 而那时 offscreen 文档并不存在。若把读取能力绑在 offscreen 上，
 * 「发现 N 段未保存的录音」这个提示恰好在最需要它的场景下不出现。
 *
 * 注意写入路径（begin/append）只会由 offscreen 使用 —— 只有它持有 MediaRecorder。
 *
 * ## 为什么不用纯内存
 *
 * 用户预期单次录制会持续**数十分钟到数小时**。纯内存方案在两个方面不够：
 *
 * 1. 崩溃/扩展重载会丢掉全部已录数据。录了 3 小时然后因为一次误操作全丢，
 *    是这个工具最不可接受的失败模式。
 * 2. 虽然 Chrome 的 Blob 本身由浏览器托管（大 Blob 会自动落盘，不占 JS 堆），
 *    但那些 Blob 的**生命周期绑定在 offscreen 文档上** —— 文档一死，
 *    Blob 引用全部失效，数据无法挽回。
 *
 * IDB 让数据的生命周期独立于文档，于是「扩展重载后恢复未保存的录音」成为可能。
 *
 * ## 为什么直接存 Blob 而不是 ArrayBuffer
 *
 * IDB 的结构化克隆原生支持 Blob，且 Chrome 会用它自己的 blob 存储做后端 ——
 * 存取都是引用传递，**不经过 JS 堆**。转成 ArrayBuffer 反而会把每一块
 * 都实体化进内存，那才是内存爆掉的原因。
 */

import { createLogger } from './log.js';

const log = createLogger('chunk-store');

const DB_NAME = 'tab-audio-recorder';
const DB_VERSION = 1;
/** 分块表：键为 [sessionId, tabId, seq] */
const CHUNKS = 'chunks';
/** 会话元数据表：键为 `${sessionId}:${tabId}` */
const META = 'meta';

/** @type {Promise<IDBDatabase> | null} */
let dbPromise = null;

/**
 * 打开（并在必要时升级）数据库。
 * @returns {Promise<IDBDatabase>}
 */
function openDb() {
  if (dbPromise) return dbPromise;
  dbPromise = new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);

    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(CHUNKS)) {
        // 复合键 [sessionId, tabId, seq] 天然按 seq 有序 ——
        // 取回时无需排序，IDB 的游标顺序就是正确的拼接顺序。
        db.createObjectStore(CHUNKS, { keyPath: ['sessionId', 'tabId', 'seq'] });
      }
      if (!db.objectStoreNames.contains(META)) {
        db.createObjectStore(META, { keyPath: 'key' });
      }
    };

    req.onsuccess = () => {
      const db = req.result;
      db.onversionchange = () => {
        // 另一个上下文要升级数据库 —— 让路，否则会卡住它
        db.close();
        dbPromise = null;
      };
      resolve(db);
    };
    req.onerror = () => reject(req.error ?? new Error('打开 IndexedDB 失败'));
  }).catch((err) => {
    dbPromise = null; // 允许下次重试
    throw err;
  });
  return dbPromise;
}

/**
 * 把一个 IDBRequest 包成 Promise。
 * @template T
 * @param {IDBRequest<T>} req
 * @returns {Promise<T>}
 */
function promisify(req) {
  return new Promise((resolve, reject) => {
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error ?? new Error('IndexedDB 操作失败'));
  });
}

/**
 * 等一个事务真正提交完成。
 *
 * 这一步很重要：`put` 的 onsuccess 只表示请求被接受，
 * 数据真正落盘要等 transaction 的 oncomplete。恢复场景依赖后者。
 * @param {IDBTransaction} tx
 */
function txDone(tx) {
  return new Promise((resolve, reject) => {
    tx.oncomplete = () => resolve(undefined);
    tx.onerror = () => reject(tx.error ?? new Error('事务失败'));
    tx.onabort = () => reject(tx.error ?? new Error('事务被中止'));
  });
}

/** @param {string} sessionId @param {number} tabId */
const metaKey = (sessionId, tabId) => `${sessionId}:${tabId}`;

/**
 * 覆盖某 (sessionId, tabId) 下所有 seq 的键范围。
 *
 * 用的是 IDB 复合键的标准惯用法，而不是 ±Infinity：
 * - 下界 `[s, t]` —— 相同前缀下，**短数组排在长数组之前**
 * - 上界 `[s, t, []]` —— IDB 的键类型顺序是「数字 < 日期 < 字符串 < 二进制 < 数组」，
 *   所以空数组排在任何数字之后
 *
 * 于是这个范围恰好覆盖全部 `[s, t, <任意数字>]`，且不依赖
 * 「Infinity 是否是合法键」这种边角规则。
 *
 * @param {string} sessionId
 * @param {number} tabId
 */
const chunkRange = (sessionId, tabId) =>
  IDBKeyRange.bound([sessionId, tabId], [sessionId, tabId, []]);

/**
 * @typedef {object} SessionMeta
 * @property {string} key
 * @property {string} sessionId
 * @property {number} tabId
 * @property {string} sessionStamp
 * @property {string} title
 * @property {string} url
 * @property {number} index
 * @property {number} startedAt
 * @property {number} chunks 已写入的分块数
 * @property {number} bytes 累计字节数
 * @property {'recording'|'complete'|'saved'} status
 * @property {string} mimeType
 */

/**
 * 开始一段录音：写入元数据。
 * @param {Omit<SessionMeta, 'key'|'chunks'|'bytes'|'status'>} info
 */
export async function begin(info) {
  const db = await openDb();
  const tx = db.transaction(META, 'readwrite');
  /** @type {SessionMeta} */
  const meta = {
    key: metaKey(info.sessionId, info.tabId),
    ...info,
    chunks: 0,
    bytes: 0,
    status: 'recording',
  };
  tx.objectStore(META).put(meta);
  await txDone(tx);
  log.debug(`begin tab=${info.tabId} session=${info.sessionId}`);
  return meta;
}

/**
 * 追加一个分块。
 *
 * 在一个事务里同时写分块和更新计数，保证两者不会不一致 ——
 * 恢复时靠 meta.chunks 判断该读多少块。
 *
 * @param {string} sessionId
 * @param {number} tabId
 * @param {number} seq
 * @param {Blob} blob
 * @returns {Promise<{chunks:number, bytes:number}>}
 */
export async function append(sessionId, tabId, seq, blob) {
  const db = await openDb();
  const tx = db.transaction([CHUNKS, META], 'readwrite');
  const store = tx.objectStore(CHUNKS);
  const metaStore = tx.objectStore(META);

  store.put({ sessionId, tabId, seq, blob });

  const key = metaKey(sessionId, tabId);
  const meta = /** @type {SessionMeta | undefined} */ (await promisify(metaStore.get(key)));
  const next = meta
    ? { ...meta, chunks: Math.max(meta.chunks, seq + 1), bytes: meta.bytes + blob.size }
    : null;
  if (next) metaStore.put(next);

  await txDone(tx);
  return { chunks: next?.chunks ?? seq + 1, bytes: next?.bytes ?? blob.size };
}

/**
 * 取回某段录音的所有分块，按 seq 顺序。
 *
 * 返回的是 Blob **引用**数组，不实体化数据 —— 调用方应直接
 * `new Blob(chunks)` 惰性拼接，让浏览器处理实际字节。
 *
 * @param {string} sessionId
 * @param {number} tabId
 * @returns {Promise<Blob[]>}
 */
export async function readChunks(sessionId, tabId) {
  const db = await openDb();
  const tx = db.transaction(CHUNKS, 'readonly');
  const store = tx.objectStore(CHUNKS);
  /** @type {Array<{seq:number, blob:Blob}>} */
  const rows = /** @type {any} */ (await promisify(store.getAll(chunkRange(sessionId, tabId))));
  await txDone(tx).catch(() => { /* 只读事务的完成失败无关紧要 */ });

  // getAll 已按键顺序返回，这里再排一次是廉价的保险
  rows.sort((a, b) => a.seq - b.seq);
  return rows.map((r) => r.blob);
}

/**
 * @param {string} sessionId
 * @param {number} tabId
 * @returns {Promise<SessionMeta | undefined>}
 */
export async function getMeta(sessionId, tabId) {
  const db = await openDb();
  const tx = db.transaction(META, 'readonly');
  const meta = await promisify(tx.objectStore(META).get(metaKey(sessionId, tabId)));
  return /** @type {any} */ (meta);
}

/**
 * 标记状态。
 * @param {string} sessionId
 * @param {number} tabId
 * @param {SessionMeta['status']} status
 */
export async function setStatus(sessionId, tabId, status) {
  const db = await openDb();
  const tx = db.transaction(META, 'readwrite');
  const store = tx.objectStore(META);
  const meta = /** @type {SessionMeta | undefined} */ (
    await promisify(store.get(metaKey(sessionId, tabId)))
  );
  if (meta) store.put({ ...meta, status });
  await txDone(tx);
}

/**
 * 删除某段录音的全部数据（分块 + 元数据）。下载成功后调用。
 * @param {string} sessionId
 * @param {number} tabId
 */
export async function discard(sessionId, tabId) {
  const db = await openDb();
  const tx = db.transaction([CHUNKS, META], 'readwrite');
  tx.objectStore(CHUNKS).delete(chunkRange(sessionId, tabId));
  tx.objectStore(META).delete(metaKey(sessionId, tabId));
  await txDone(tx);
  log.debug(`已清理 tab=${tabId} session=${sessionId} 的分块`);
}

/**
 * 列出所有**未保存**的录音 —— 即崩溃/重载后可恢复的那些。
 *
 * status 为 'recording' 表示录制时进程消失了（正常停止会先置为 'complete'）；
 * 'complete' 表示已收尾但下载没成功。两者都值得提示用户保存。
 *
 * @returns {Promise<SessionMeta[]>}
 */
export async function listRecoverable() {
  const db = await openDb();
  const tx = db.transaction(META, 'readonly');
  const all = /** @type {SessionMeta[]} */ (
    /** @type {any} */ (await promisify(tx.objectStore(META).getAll()))
  );
  return all.filter((m) => m.status !== 'saved' && m.chunks > 0);
}

/**
 * 清理所有已保存的残留元数据。启动时调用，保持数据库整洁。
 */
export async function pruneSaved() {
  const db = await openDb();
  const tx = db.transaction(META, 'readonly');
  const all = /** @type {SessionMeta[]} */ (
    /** @type {any} */ (await promisify(tx.objectStore(META).getAll()))
  );
  const saved = all.filter((m) => m.status === 'saved');
  for (const m of saved) {
    await discard(m.sessionId, m.tabId);
  }
  if (saved.length > 0) log.info(`清理了 ${saved.length} 条已保存的残留记录`);
}
