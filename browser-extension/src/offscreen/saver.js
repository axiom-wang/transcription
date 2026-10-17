// @ts-check
/**
 * 文件保存 —— 在 offscreen 里造 blob URL，委托 service worker 调 downloads。
 *
 * ## 为什么是这个分工（两个硬约束夹出来的唯一解）
 *
 * 1. **`chrome.downloads` 在 offscreen 文档里不存在。** 官方文档写了两次
 *    「`runtime` API 是 offscreen 文档唯一支持的扩展 API」。权限声明照样生效，
 *    但 API 面被刻意收窄了。曾经在这里直接调 `chrome.downloads.onChanged`，
 *    线上报 `Cannot read properties of undefined (reading 'onChanged')`，
 *    导致**每一次录制的保存都失败**。
 * 2. **`URL.createObjectURL` 在 service worker 里不存在。**
 *    ServiceWorkerGlobalScope 没有这个方法，所以 blob URL 只能在这边造。
 *
 * 于是分工只能是：
 *
 * ```
 *   offscreen: 造 blob URL ──REQUEST_DOWNLOAD──> SW: chrome.downloads.download
 *   offscreen: 回收 URL   <──REVOKE_URL──────── SW: 下载到达终态
 * ```
 *
 * ## 为什么不用 anchor click
 *
 * 多文件的 anchor 下载在没有用户激活时会被 Chrome 限流甚至静默拦截，
 * 而我们的场景恰恰是「一次停止 8 个标签页，同时产生 8 个文件」。
 * 而且 anchor 无法控制重名策略，也无法指定子目录。
 *
 * ## URL 生命周期
 *
 * **提前回收会截断文件。** 所以这边只负责持有，等 SW 通知终态才回收；
 * 另加一个兜底计时器，防止消息丢失导致 URL 永久泄漏。
 */

import { createLogger, formatBytes } from '../common/log.js';
import { buildDownloadPath, buildAsciiFallbackPath } from '../common/filename.js';
import { EV, sendTo } from '../common/protocol.js';

const log = createLogger('saver');

/**
 * 本上下文创建的、尚未回收的 blob URL。
 * @type {Map<string, {timer:ReturnType<typeof setTimeout>, bytes:number}>}
 */
const liveUrls = new Map();

/**
 * 兜底回收延迟。
 *
 * 正常路径是 SW 在下载终态后发 REVOKE_URL。这个计时器只防消息丢失
 * （比如 SW 恰好在下载期间被杀）。取 10 分钟而不是 60 秒 ——
 * 几百 MB 的文件写盘可能相当慢，过早回收会截断文件，那比泄漏一个 URL 严重得多。
 */
const REVOKE_FALLBACK_MS = 10 * 60_000;

/**
 * @typedef {object} SaveArgs
 * @property {Blob} blob
 * @property {string} title Tab 标题（用于文件名）
 * @property {number} tabId
 * @property {number} index 授权顺序
 * @property {string} sessionStamp 会话目录名
 * @property {string} extension 'mkv' 或降级的 'webm'
 */

/**
 * @typedef {object} SaveResult
 * @property {boolean} ok
 * @property {string} [filename] 实际请求的相对路径（Chrome 可能因去重而改动）
 * @property {number} [downloadId]
 * @property {number} [bytes]
 * @property {string} [error]
 * @property {boolean} [usedFallback] 是否用了 ASCII 兜底名
 */

/**
 * 保存一个 Blob 为下载文件。
 *
 * 文件名由这边算好（`common/filename.js` 是纯函数，两个上下文都能用），
 * 连同 ASCII 兜底名一起发给 SW —— 这样 SW 端不需要重复文件名逻辑，
 * 而 SW 在主文件名被拒时可以直接用兜底名重试一次。
 * **绝不让文件名问题弄丢音频。**
 *
 * @param {SaveArgs} args
 * @returns {Promise<SaveResult>}
 */
export async function saveBlob(args) {
  const { blob, title, tabId, index, sessionStamp, extension } = args;
  const stoppedAt = new Date();

  const filename = buildDownloadPath({
    title, index, sessionStamp, stoppedAt, tabId, extension,
  });
  const fallbackFilename = buildAsciiFallbackPath({
    index, sessionStamp, stoppedAt, extension,
  });

  // blob URL 只能在这个上下文创建（SW 里没有 createObjectURL）
  const blobUrl = URL.createObjectURL(blob);
  trackUrl(blobUrl, blob.size);

  try {
    const reply = await sendTo('sw', EV.REQUEST_DOWNLOAD, {
      blobUrl,
      filename,
      fallbackFilename,
      tabId,
      bytes: blob.size,
    });

    if (!reply?.ok) {
      // SW 侧两个文件名都失败了 —— 立刻回收，避免泄漏
      revokeUrl(blobUrl);
      const message = reply?.error?.message ?? reply?.error ?? '下载未能启动';
      log.error(`下载失败: ${message}`);
      return { ok: false, error: String(message) };
    }

    log.info(
      `已开始下载 #${reply.downloadId}: ${reply.filename}（${formatBytes(blob.size)}）` +
      (reply.usedFallback ? '（使用 ASCII 兜底名）' : ''),
    );
    return {
      ok: true,
      filename: reply.filename,
      downloadId: reply.downloadId,
      bytes: blob.size,
      usedFallback: reply.usedFallback,
    };
  } catch (err) {
    // 消息本身发不出去（SW 正在重启等）—— URL 先留着，兜底计时器会收，
    // 因为此时无法确定下载到底有没有开始。
    const message = err instanceof Error ? err.message : String(err);
    log.error(`下载请求发送失败: ${message}`);
    return { ok: false, error: message };
  }
}

/**
 * @param {string} url
 * @param {number} bytes
 */
function trackUrl(url, bytes) {
  const timer = setTimeout(() => {
    if (liveUrls.has(url)) {
      log.warn(`blob URL 兜底回收（未收到终态通知，${formatBytes(bytes)}）`);
      revokeUrl(url);
    }
  }, REVOKE_FALLBACK_MS);
  liveUrls.set(url, { timer, bytes });
}

/**
 * 回收一个 blob URL。幂等。
 *
 * 由 SW 在下载到达终态后通过 REVOKE_URL 触发，或由兜底计时器触发。
 * @param {string} url
 */
export function revokeUrl(url) {
  const entry = liveUrls.get(url);
  if (!entry) return false;
  clearTimeout(entry.timer);
  liveUrls.delete(url);
  URL.revokeObjectURL(url);
  log.debug(`已回收 blob URL（${formatBytes(entry.bytes)}）`);
  return true;
}

/** 供诊断用：当前尚未回收的 URL 数量 */
export function liveUrlCount() {
  return liveUrls.size;
}
