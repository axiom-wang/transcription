// @ts-check
/**
 * Tab 注册表 —— SW 侧的每 Tab 状态，并镜像进 chrome.storage.session。
 *
 * ## 为什么要镜像到 storage.session
 *
 * service worker 随时可能被杀。镜像让两件事成为可能：
 * 1. SW 重启后能读回「上次的信念」，再与 offscreen 对账（offscreen 赢冲突）。
 * 2. panel **只读 storage**、订阅 onChanged，完全不接收推送 —— 这消除了
 *    「panel 关闭时 Receiving end does not exist」这一整类错误。
 *
 * ## 为什么用 session 而不是 local
 *
 * tabCapture 授权本身活不过浏览器重启，所以持久化到 local 只会留下一堆
 * 早已失效的「已授权」记录，比没有更糟。session 的生命周期正好匹配授权的生命周期。
 */

import { S, canTransition } from '../common/states.js';
import { createLogger } from '../common/log.js';

const log = createLogger('registry');

const STORAGE_KEY = 'tabRecords';
const SESSION_KEY = 'session';
const RECOVERABLE_KEY = 'recoverable';

/**
 * 内存中的权威副本。SW 存活期间以它为准，写入时同步刷进 storage。
 * @type {Map<number, import('../common/protocol.js').TabRecord>}
 */
let records = new Map();

/** 是否已从 storage 载入过。SW 冷启动后第一次访问要先 hydrate。 */
let hydrated = false;

/** 合并写入的待处理定时器 —— 避免每次状态变化都打一次 storage。 */
/** @type {ReturnType<typeof setTimeout> | null} */
let flushTimer = null;

/** @type {{id:string, stamp:string, startedAt:number} | null} */
let currentSession = null;

/**
 * 从 storage.session 恢复内存副本。幂等，可重复调用。
 */
export async function hydrate() {
  if (hydrated) return;
  try {
    const got = await chrome.storage.session.get([STORAGE_KEY, SESSION_KEY, RECOVERABLE_KEY]);
    const arr = got[STORAGE_KEY];
    if (Array.isArray(arr)) {
      records = new Map(arr.map((r) => [r.tabId, r]));
      log.info(`从 storage 恢复了 ${records.size} 条 Tab 记录`);
    }
    if (got[SESSION_KEY]) currentSession = got[SESSION_KEY];
    if (Array.isArray(got[RECOVERABLE_KEY])) recoverable = got[RECOVERABLE_KEY];
  } catch (err) {
    log.warn('恢复 storage 失败，以空状态继续', err);
  }
  hydrated = true;
}

/** 立即把内存副本写进 storage.session。 */
async function flushNow() {
  flushTimer = null;
  try {
    await chrome.storage.session.set({
      [STORAGE_KEY]: Array.from(records.values()),
      [SESSION_KEY]: currentSession,
      [RECOVERABLE_KEY]: recoverable,
    });
  } catch (err) {
    log.warn('写入 storage 失败', err);
  }
}

/**
 * 安排一次合并写入。
 *
 * 60ms 的延迟足以把「顺序铸造循环里连续几个状态变化」合并成一次写入，
 * 又短到用户感知不到 panel 的更新延迟。
 */
function scheduleFlush() {
  if (flushTimer !== null) return;
  flushTimer = setTimeout(() => { void flushNow(); }, 60);
}

/** 状态变化后立刻刷盘（用于关键节点，如录制开始/结束）。 */
export async function flush() {
  if (flushTimer !== null) { clearTimeout(flushTimer); flushTimer = null; }
  await flushNow();
}

/** @returns {import('../common/protocol.js').TabRecord[]} */
export function all() {
  return Array.from(records.values());
}

/**
 * @param {number} tabId
 * @returns {import('../common/protocol.js').TabRecord | undefined}
 */
export function get(tabId) {
  return records.get(tabId);
}

/** @param {number} tabId */
export function has(tabId) {
  return records.has(tabId);
}

/**
 * 新增或更新一条记录（浅合并）。
 * @param {number} tabId
 * @param {Partial<import('../common/protocol.js').TabRecord>} patch
 * @returns {import('../common/protocol.js').TabRecord}
 */
export function upsert(tabId, patch) {
  const prev = records.get(tabId);
  /** @type {import('../common/protocol.js').TabRecord} */
  const next = {
    tabId,
    state: S.IDLE,
    title: '',
    url: '',
    origin: '',
    index: 0,
    ...(prev ?? {}),
    ...patch,
  };
  records.set(tabId, next);
  scheduleFlush();
  return next;
}

/**
 * 状态转移。非法转移会被**记录并拒绝**，而不是抛错。
 *
 * 这里刻意与 assertTransition 的严厉程度不同：SW 里的状态变化常常由
 * 竞态事件驱动（比如 tabs.onRemoved 和 track ended 几乎同时到达），
 * 一个迟到的重复事件不该让整个消息处理器崩掉。记录下来足以在开发期发现问题。
 *
 * @param {number} tabId
 * @param {import('../common/states.js').TabState} to
 * @param {Partial<import('../common/protocol.js').TabRecord>} [extra]
 * @returns {import('../common/protocol.js').TabRecord | null} 转移被拒时返回 null
 */
export function setState(tabId, to, extra = {}) {
  const cur = records.get(tabId);
  const from = cur?.state ?? S.IDLE;

  if (cur && !canTransition(from, to)) {
    log.warn(`拒绝非法状态转移 tab=${tabId}: ${from} → ${to}`);
    return null;
  }

  // 进入 error 之外的状态时清掉旧的 error，避免 UI 显示过期的错误信息
  /** @type {Partial<import('../common/protocol.js').TabRecord>} */
  const cleanup = to === S.ERROR ? {} : { error: undefined };

  // 重新开始录制时清掉上一轮的活性心跳 —— 否则新会话的头几秒里，
  // 面板会显示上一段录音的陈旧「有声/静音」状态
  if (to === S.STARTING) cleanup.audio = undefined;

  const next = upsert(tabId, { ...cleanup, ...extra, state: to });
  log.debug(`tab=${tabId} ${from} → ${to}`);
  return next;
}

/** @param {number} tabId */
export function remove(tabId) {
  const existed = records.delete(tabId);
  if (existed) scheduleFlush();
  return existed;
}

/** 清空全部记录（扩展重载时授权全失效，见 service-worker.js 的 onInstalled）。 */
export function clear() {
  records.clear();
  currentSession = null;
  // 刻意**不**清空 recoverable —— 扩展重载正是产生可恢复录音的主要原因，
  // 在这里连带清掉就等于把用户刚刚丢失的录音提示也一起抹掉了。
  scheduleFlush();
}

/**
 * 已授权（信念上）的 Tab —— 即可以直接开录的那些。
 * 包含 done/error 状态：停止捕获不会撤销授权，所以它们仍可再次录制。
 * origin 为空表示授权已知失效（跨源跳转、标签页已关闭、恢复保存的旧录音），不算在内。
 */
export function armedTabIds() {
  return all()
    .filter((r) => r.origin && (r.state === S.ARMED || r.state === S.DONE || r.state === S.ERROR))
    .sort((a, b) => a.index - b.index)
    .map((r) => r.tabId);
}

/** 正在录制（含启动/收尾中）的 Tab */
export function activeTabIds() {
  return all()
    .filter((r) => [S.STARTING, S.RECORDING, S.STOPPING, S.FINALIZING, S.SAVING].includes(r.state))
    .map((r) => r.tabId);
}

/** 下一个授权顺序号（从 1 开始，决定文件名前缀） */
export function nextIndex() {
  const max = all().reduce((m, r) => Math.max(m, r.index), 0);
  return max + 1;
}

// ───────────────────────────── 会话 ─────────────────────────────

/**
 * 当前「开始录制」会话。同一批文件会落到同一个子目录里。
 * @param {{id:string, stamp:string, startedAt:number} | null} s
 */
export function setSession(s) {
  currentSession = s;
  scheduleFlush();
}

export function getSession() {
  return currentSession;
}

// ─────────────────────── 可恢复录音（崩溃补救） ───────────────────────

/**
 * offscreen 上报的「IDB 里有分块但未保存」的录音列表。
 *
 * 存在 SW 内存 + storage 镜像里，这样 panel 打开时无论 offscreen 是否还活着
 * 都能看到提示 —— 恢复场景的典型触发原因正是 offscreen 已经死过一次。
 * @type {any[]}
 */
let recoverable = [];

/** @param {any[]} list */
export function setRecoverable(list) {
  recoverable = Array.isArray(list) ? list : [];
  scheduleFlush();
}

export function getRecoverable() {
  return recoverable;
}

/**
 * 某段录音已处理（保存或丢弃），从列表移除。
 * @param {string} sessionId
 * @param {number} tabId
 */
export function removeRecoverable(sessionId, tabId) {
  const before = recoverable.length;
  recoverable = recoverable.filter(
    (r) => !(r.sessionId === sessionId && r.tabId === tabId),
  );
  if (recoverable.length !== before) scheduleFlush();
}
