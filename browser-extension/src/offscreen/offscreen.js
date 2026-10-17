// @ts-check
/**
 * offscreen 文档入口 —— 消息路由 + 录制器生命周期。
 *
 * 这是权威状态所有者：service worker 随时会被杀，而本文档在
 * reasons:['USER_MEDIA'] 下不设生命周期上限。因此 OFFSCREEN_PING 的回复
 * 在对账时**赢得所有冲突**。
 */

import { S, E } from '../common/states.js';
import { createLogger } from '../common/log.js';
import { OFF, EV, isFor, sendTo } from '../common/protocol.js';
import { TabRecorder } from './recorder.js';
import { webmBlobToMatroska } from './mkv.js';
import { saveBlob, revokeUrl } from './saver.js';
import * as store from '../common/chunk-store.js';

const log = createLogger('offscreen');

/** @type {Map<number, TabRecorder>} */
const recorders = new Map();

/** 各 Tab 的会话信息，收尾保存时需要（recorder 被销毁后仍要用） */
/** @type {Map<number, {sessionId:string, sessionStamp:string, title:string, index:number}>} */
const sessionInfo = new Map();

// ───────────────────────────── 上报给 SW ─────────────────────────────

/**
 * 向 SW 上报状态。
 *
 * 刻意吞掉发送错误：SW 可能正好处于被杀和重启之间，此时上报失败是**正常**的，
 * 不该产生噪音 —— SW 重启后会通过 PING 重新对账拿到权威状态。
 *
 * @param {number} tabId
 * @param {import('../common/states.js').TabState} state
 * @param {object} [extra]
 */
function reportState(tabId, state, extra = {}) {
  sendTo('sw', EV.TAB_STATE, { tabId, state, at: Date.now(), ...extra })
    .catch(() => { /* SW 可能正在重启，PING 会补上 */ });
}

/** 字节进度上报做节流 —— 1 秒一个分块，没必要每块都发消息 */
/** @type {Map<number, number>} */
const lastProgressReport = new Map();
const PROGRESS_THROTTLE_MS = 2000;

/**
 * @param {number} tabId
 * @param {{bytes:number, chunks:number, audio?:import('../common/audio-level.js').AudioSnapshot|null}} info
 */
function reportProgress(tabId, info) {
  const now = Date.now();
  const last = lastProgressReport.get(tabId) ?? 0;
  if (now - last < PROGRESS_THROTTLE_MS) return;
  lastProgressReport.set(tabId, now);
  sendTo('sw', EV.TAB_STATE, {
    tabId, state: S.RECORDING, at: now, bytes: info.bytes,
    // audio 为 null 表示分析链路不可用（如 AudioContext 被策略卡住），
    // 此时不带这个字段 —— 面板端「没有数据就不显示」，而不是显示错误状态
    ...(info.audio ? { audio: info.audio } : {}),
  }).catch(() => { /* 同上 */ });
}

// ───────────────────────── 录制器回调 ─────────────────────────

/** @type {import('./recorder.js').RecorderCallbacks} */
const callbacks = {
  onState(tabId, state, extra) {
    reportState(tabId, state, extra);
  },

  onProgress(tabId, info) {
    reportProgress(tabId, info);
  },

  onError(tabId, error) {
    log.warn(`tab=${tabId} 错误: ${error.code} ${error.message}`);
    reportState(tabId, S.ERROR, { error });
    recorders.delete(tabId);
  },

  /**
   * 收尾完成 → 改写容器 → 保存 → 清理 IDB。
   */
  async onFinalize(tabId, result) {
    const info = sessionInfo.get(tabId);
    if (!info) {
      log.error(`tab=${tabId} 缺少会话信息，无法保存`);
      reportState(tabId, S.ERROR, {
        error: { code: E.UNKNOWN, message: '缺少保存所需的会话信息' },
      });
      return;
    }

    try {
      // ── 改写 EBML DocType：WebM → Matroska（O(1) 内存）──
      const patched = await webmBlobToMatroska(result.blob);

      reportState(tabId, S.SAVING);

      const saved = await saveBlob({
        blob: patched.blob,
        title: info.title,
        tabId,
        index: info.index,
        sessionStamp: info.sessionStamp,
        extension: patched.extension,
      });

      if (!saved.ok) {
        // 下载失败：**保留 IDB 分块**，用户可以重试保存
        reportState(tabId, S.ERROR, {
          error: { code: E.DOWNLOAD, message: saved.error ?? '文件保存失败' },
        });
        return;
      }

      // 下载已启动成功 → 标记并清理分块
      await store.setStatus(info.sessionId, tabId, 'saved');
      await store.discard(info.sessionId, tabId);

      await sendTo('sw', EV.SAVED, {
        tabId,
        filename: saved.filename,
        downloadId: saved.downloadId,
        bytes: result.bytes,
        durationMs: result.durationMs,
        warning: patched.warning,
      }).catch(() => { /* SW 重启中，PING 会补 */ });

      log.info(`tab=${tabId} 已保存 ${saved.filename}`);
    } catch (err) {
      log.error(`tab=${tabId} 保存流程失败`, err);
      reportState(tabId, S.ERROR, {
        error: {
          code: E.DOWNLOAD,
          message: err instanceof Error ? err.message : String(err),
        },
      });
    } finally {
      recorders.delete(tabId);
      sessionInfo.delete(tabId);
      lastProgressReport.delete(tabId);
    }
  },
};

// ───────────────────────────── 消息处理 ─────────────────────────────

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  // ⚠️ 不是发给 offscreen 的必须立刻 return false（详见 protocol.js 的说明）
  if (!isFor(message, 'offscreen')) return false;

  handleMessage(message)
    .then(sendResponse)
    .catch((err) => {
      log.error(`处理 ${message?.type} 失败`, err);
      sendResponse({
        ok: false,
        error: {
          code: /** @type {any} */ (err)?.code ?? E.UNKNOWN,
          message: err instanceof Error ? err.message : String(err),
        },
      });
    });

  return true; // 我们确实会异步回复
});

/**
 * @param {any} message
 */
async function handleMessage(message) {
  switch (message.type) {
    case OFF.PING:
      return {
        ok: true,
        version: 1,
        tabs: Array.from(recorders.values()).map((r) => r.snapshot()),
      };

    case OFF.START_ONE:
      return await startOne(message);

    case OFF.STOP_ONE: {
      const rec = recorders.get(message.tabId);
      if (!rec) return { ok: true, note: '该标签页没有进行中的录制' };
      await rec.stop(message.reason ?? 'user');
      return { ok: true };
    }

    case OFF.STOP_ALL: {
      /** @type {number[]} */
      const targets = Array.isArray(message.tabIds) && message.tabIds.length > 0
        ? message.tabIds
        : Array.from(recorders.keys());
      // 并发停止是安全的：每个 recorder 独立，且 stop() 幂等
      await Promise.allSettled(
        targets.map((/** @type {number} */ id) =>
          recorders.get(id)?.stop(message.reason ?? 'user')),
      );
      return { ok: true, tabIds: targets };
    }

    case OFF.SET_MONITOR: {
      const rec = recorders.get(message.tabId);
      if (rec) await rec.setMonitor(Boolean(message.enabled));
      return { ok: true };
    }

    case OFF.SET_MONITOR_ALL: {
      const enabled = Boolean(message.enabled);
      // 并发切换是安全的：每个 recorder 各自持有独立的 GainNode
      await Promise.allSettled(
        Array.from(recorders.values()).map((r) => r.setMonitor(enabled)),
      );
      return { ok: true, affected: recorders.size };
    }

    case OFF.DISCARD: {
      const rec = recorders.get(message.tabId);
      if (rec) {
        rec.finalizing = true; // 阻止它走保存流程
        rec.cleanupStream();
        recorders.delete(message.tabId);
      }
      const info = sessionInfo.get(message.tabId);
      if (info) {
        await store.discard(info.sessionId, message.tabId);
        sessionInfo.delete(message.tabId);
      }
      return { ok: true };
    }

    case OFF.LIST_RECOVERABLE:
      return { ok: true, sessions: await store.listRecoverable() };

    case OFF.RECOVER_SAVE:
      return await recoverSave(message.sessionId, message.tabId);

    case OFF.RECOVER_DISCARD:
      await store.discard(message.sessionId, message.tabId);
      return { ok: true };

    case OFF.REVOKE_URL: {
      // SW 通知某个下载已到达终态。blob URL 必须在创建它的上下文回收，
      // 而创建方是这里（service worker 里没有 URL.createObjectURL）。
      const revoked = revokeUrl(message.blobUrl);
      return { ok: true, revoked };
    }

    default:
      return { ok: false, error: { code: E.UNKNOWN, message: `未知消息: ${message.type}` } };
  }
}

/**
 * 启动单个 Tab 的录制。
 *
 * 本函数 resolve `{ok:true}` 的含义是**兑换已完成** —— SW 的顺序铸造循环
 * 等的正是这个 ack，收到后才会铸造下一个 streamId。
 *
 * @param {any} message
 */
async function startOne(message) {
  const { tabId, streamId, meta, opts } = message;

  // 同一个 Tab 已有录制在跑 —— Chrome 侧也只允许每 Tab 一个捕获
  if (recorders.has(tabId)) {
    return {
      ok: false,
      error: { code: E.TAB_BUSY, message: '该标签页已在录制中' },
    };
  }

  const recorder = new TabRecorder({ tabId, meta, opts, callbacks });

  try {
    await recorder.start(streamId);
  } catch (err) {
    const code = /** @type {any} */ (err)?.code ?? E.GUM_FAILED;
    const rawMessage = err instanceof Error ? err.message : String(err);
    log.warn(`tab=${tabId} 启动失败: ${code} ${rawMessage}`);
    recorder.cleanupStream();
    return { ok: false, error: { code, message: rawMessage } };
  }

  recorders.set(tabId, recorder);
  sessionInfo.set(tabId, {
    sessionId: opts.sessionId,
    sessionStamp: opts.sessionStamp,
    title: meta.title,
    index: meta.index,
  });

  return { ok: true };
}

/**
 * 保存一段从 IDB 恢复出来的录音（崩溃/扩展重载后的补救）。
 * @param {string} sessionId
 * @param {number} tabId
 */
async function recoverSave(sessionId, tabId) {
  const meta = await store.getMeta(sessionId, tabId);
  if (!meta) return { ok: false, error: { code: E.NO_DATA, message: '找不到该录音的元数据' } };

  const chunks = await store.readChunks(sessionId, tabId);
  if (chunks.length === 0) {
    await store.discard(sessionId, tabId);
    return { ok: false, error: { code: E.NO_DATA, message: '该录音没有可用数据' } };
  }

  const merged = new Blob(chunks, { type: meta.mimeType || 'audio/webm' });
  const patched = await webmBlobToMatroska(merged);

  const saved = await saveBlob({
    blob: patched.blob,
    title: meta.title,
    tabId,
    index: meta.index,
    sessionStamp: meta.sessionStamp,
    extension: patched.extension,
  });

  if (!saved.ok) {
    return { ok: false, error: { code: E.DOWNLOAD, message: saved.error ?? '保存失败' } };
  }

  await store.setStatus(sessionId, tabId, 'saved');
  await store.discard(sessionId, tabId);

  log.info(`已恢复保存 tab=${tabId}: ${saved.filename}`);
  return { ok: true, filename: saved.filename, downloadId: saved.downloadId, bytes: merged.size };
}

// ───────────────────────────── 启动 ─────────────────────────────

/**
 * 文档加载时检查 IDB 里有没有未保存的录音。
 *
 * 有的话主动上报给 SW，由 panel 提示用户「发现 N 段未保存的录音」。
 * 这是选择 IDB 分块存储（而非纯内存）换来的最实际的好处。
 */
(async () => {
  try {
    // 先清掉「已保存」的残留元数据 —— 正常路径在保存成功时已连带删除，
    // 残留只来自异常退出（比如保存成功后、清理完成前文档被杀）
    await store.pruneSaved();

    const recoverable = await store.listRecoverable();
    if (recoverable.length > 0) {
      log.info(`发现 ${recoverable.length} 段未保存的录音`);
      await sendTo('sw', EV.RECOVERABLE, {
        sessions: recoverable.map((m) => ({
          sessionId: m.sessionId,
          tabId: m.tabId,
          title: m.title,
          bytes: m.bytes,
          chunks: m.chunks,
          startedAt: m.startedAt,
        })),
      }).catch(() => { /* SW 可能还没起来，panel 打开时会主动查询 */ });
    }
  } catch (err) {
    log.warn('检查可恢复录音失败', err);
  }
})();

log.info('offscreen 文档已就绪');
