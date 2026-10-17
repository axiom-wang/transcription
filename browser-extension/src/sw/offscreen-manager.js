// @ts-check
/**
 * offscreen 文档的生命周期管理（单例）。
 *
 * ## 为什么需要专门一个模块
 *
 * MV3 规定**每个扩展只能有一个 offscreen 文档**。并发调用 createDocument
 * 会抛「Only a single offscreen document may be created」—— 用户双击按钮就能触发。
 * 所以这里用模块级的 in-flight promise 做单例，并且额外捕获「已存在」错误当作成功。
 *
 * ## 为什么用 reasons: ['USER_MEDIA']
 *
 * `USER_MEDIA` **不设生命周期上限**，文档会一直活着。
 * 绝不能用 `AUDIO_PLAYBACK` —— 它会在无音频播放 30 秒后自动关闭文档，
 * 而我们的场景（录一个安静的会议 Tab）会被它直接杀掉。
 */

import { createLogger } from '../common/log.js';
import { OFF, sendTo, withTimeout } from '../common/protocol.js';

const log = createLogger('offscreen-mgr');

const DOCUMENT_PATH = 'src/offscreen/offscreen.html';

/**
 * 正在创建中的 promise。并发调用会共享它，从而避免竞态。
 * @type {Promise<void> | null}
 */
let creating = null;

/**
 * offscreen 文档是否存在。
 *
 * 用 getContexts 而非 hasDocument：前者是 Chrome 116+ 的通用 API，
 * 且能一并拿到上下文信息用于诊断。
 */
export async function exists() {
  try {
    const contexts = await chrome.runtime.getContexts({
      contextTypes: [/** @type {any} */ ('OFFSCREEN_DOCUMENT')],
    });
    return contexts.length > 0;
  } catch (err) {
    log.warn('getContexts 失败，回退到 hasDocument', err);
    try {
      // @ts-ignore —— hasDocument 在部分版本里未出现在类型定义中
      return await chrome.offscreen.hasDocument();
    } catch {
      return false;
    }
  }
}

/**
 * 确保 offscreen 文档存在。幂等、并发安全。
 *
 * @returns {Promise<void>}
 */
export async function ensureOffscreen() {
  if (await exists()) return;

  // 已有创建在途 —— 复用它，绝不并发创建
  if (creating !== null) {
    log.debug('已有创建在途，等待复用');
    return creating;
  }

  creating = (async () => {
    try {
      await chrome.offscreen.createDocument({
        url: DOCUMENT_PATH,
        // USER_MEDIA 不设生命周期上限。绝不用 AUDIO_PLAYBACK（30 秒无声即关闭）。
        reasons: [/** @type {any} */ ('USER_MEDIA')],
        justification: '在后台持续录制多个标签页的音频，并将音频回放给用户。',
      });
      log.info('offscreen 文档已创建');
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      // 竞态兜底：别的调用抢先创建成功了，这对我们来说等同于成功
      if (/single offscreen document|already exists|Only a single/i.test(message)) {
        log.debug('创建时发现已存在，视为成功');
        return;
      }
      throw err;
    } finally {
      creating = null;
    }
  })();

  return creating;
}

/**
 * 向 offscreen 文档发 PING，取回它持有的权威状态。
 *
 * **offscreen 是持久状态所有者，它的回复赢得所有冲突。** SW 可以随时被杀，
 * 而 offscreen 文档在 USER_MEDIA 下一直存活并真正持有那些 MediaRecorder，
 * 所以对账时必须以它为准。
 *
 * @returns {Promise<{ok:boolean, tabs?:import('../common/protocol.js').OffscreenTabState[], error?:string}>}
 */
export async function ping() {
  if (!(await exists())) {
    return { ok: false, error: 'offscreen 文档不存在' };
  }
  try {
    const reply = await withTimeout(sendTo('offscreen', OFF.PING), 3000, 'PING');
    return { ok: true, tabs: reply?.tabs ?? [] };
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    log.warn(`PING 失败: ${message}`);
    return { ok: false, error: message };
  }
}

/**
 * 关闭 offscreen 文档。
 *
 * 仅在确认没有任何录制在进行时调用 —— 关闭文档会**立即杀掉**所有捕获，
 * 未收尾的录音会丢失。
 */
export async function closeOffscreen() {
  if (!(await exists())) return;
  try {
    await chrome.offscreen.closeDocument();
    log.info('offscreen 文档已关闭');
  } catch (err) {
    log.warn('关闭 offscreen 文档失败', err);
  }
}
