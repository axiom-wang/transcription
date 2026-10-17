// @ts-check
/**
 * 授权（arming）管理 + 每 Tab 徽标渲染。
 *
 * ## 核心约束（Chromium 源码级已确认）
 *
 * `chrome.tabCapture.getMediaStreamId({targetTabId})` 校验的是**目标 Tab**
 * 是否持有 `kTabCaptureForTab` 权限。该权限由 `ActiveTabPermissionGranter::
 * GrantIfRequested()` 在用户于该 Tab 上触发扩展时授予 —— 触发途径有：
 * 点击扩展图标、右键菜单项、chrome.commands 快捷键、omnibox。
 *
 * 所以：**后台 Tab 可以录，但从未被触发过的 Tab 不能录。** 无法绕过。
 *
 * 好消息：授权持续存在（同源/同文档跳转都不失效），且调用 getMediaStreamId
 * **不会消耗**它。`getMediaStreamId` 内部也没有实时用户手势检查。
 * 因此「逐个授权 → 稍后一键全部开录」完全可行。
 *
 * ## 授权状态只是「信念」
 *
 * 唯一权威检测是真去调 getMediaStreamId。我们用 origin 追踪来维护信念：
 * 授权时记下 origin，`tabs.onUpdated` 里主框架 origin 变化就降级为未授权。
 */

import { S } from '../common/states.js';
import { createLogger } from '../common/log.js';
import * as registry from './registry.js';

const log = createLogger('arming');

/** 徽标样式。每 Tab 独立设置，是成本最低的「这个 Tab 授权了吗」教学机制。 */
const BADGE = {
  [S.ARMED]: { text: '●', color: '#2563eb' },
  [S.STARTING]: { text: '···', color: '#f59e0b' },
  [S.RECORDING]: { text: 'REC', color: '#dc2626' },
  [S.STOPPING]: { text: '···', color: '#f59e0b' },
  [S.FINALIZING]: { text: '···', color: '#f59e0b' },
  [S.SAVING]: { text: '···', color: '#f59e0b' },
  [S.DONE]: { text: '✓', color: '#16a34a' },
  [S.ERROR]: { text: '!', color: '#f59e0b' },
  [S.IDLE]: { text: '', color: '#000000' },
};

/**
 * 从 URL 取 origin。取不到（chrome:// 等）时返回空串。
 * @param {string | undefined} url
 */
export function originOf(url) {
  if (!url) return '';
  try {
    return new URL(url).origin;
  } catch {
    return '';
  }
}

/**
 * 该 URL 是否根本不可能被捕获。
 * chrome://、扩展页、Web Store 等特权页面永远无法 tabCapture，
 * 提前拦下能给出清晰的中文提示，而不是让用户看一句底层报错。
 * @param {string | undefined} url
 */
export function isCapturable(url) {
  if (!url) return false;
  return /^(https?|file|blob|data):/i.test(url) && !/^https:\/\/chrome\.google\.com\/webstore/i.test(url);
}

/**
 * 渲染某 Tab 的徽标。
 *
 * 注意：徽标是**每 Tab**的（传 tabId），所以不会互相干扰；
 * 但 SW 重启后全部丢失，需要 refreshAllBadges() 重绘。
 *
 * @param {number} tabId
 * @param {import('../common/states.js').TabState} state
 */
export async function renderBadge(tabId, state) {
  const style = BADGE[state] ?? BADGE[S.IDLE];
  try {
    await chrome.action.setBadgeText({ tabId, text: style.text });
    if (style.text) {
      await chrome.action.setBadgeBackgroundColor({ tabId, color: style.color });
    }
  } catch (err) {
    // Tab 可能已经关闭 —— 这是正常竞态，不值得报错
    log.debug(`设置徽标失败 tab=${tabId}（可能已关闭）`, err);
  }
}

/**
 * 重绘所有已知 Tab 的徽标。SW 重启后必须调用一次。
 */
export async function refreshAllBadges() {
  const records = registry.all();
  await Promise.all(records.map((r) => renderBadge(r.tabId, r.state)));
  log.debug(`重绘了 ${records.length} 个徽标`);
}

/**
 * 授权一个 Tab（把它加入录制列表）。
 *
 * 调用时机必须是**用户在该 Tab 上触发扩展的那一刻** —— 快捷键处理器、
 * action.onClicked、右键菜单处理器。此时浏览器已经授予了权限，
 * 我们只是把这个事实记录下来。
 *
 * @param {chrome.tabs.Tab} tab
 * @returns {Promise<{ok:boolean, reason?:string, record?:import('../common/protocol.js').TabRecord}>}
 */
export async function armTab(tab) {
  if (typeof tab.id !== 'number' || tab.id === chrome.tabs.TAB_ID_NONE) {
    return { ok: false, reason: '无法识别该标签页' };
  }
  if (!isCapturable(tab.url)) {
    return { ok: false, reason: '该页面类型无法录制（浏览器内部页面 / 扩展页面）' };
  }

  const existing = registry.get(tab.id);

  // 已在录制中的，重复按快捷键不该有副作用
  if (existing && [S.STARTING, S.RECORDING, S.STOPPING, S.FINALIZING, S.SAVING].includes(existing.state)) {
    return { ok: true, record: existing };
  }

  const index = existing?.index || registry.nextIndex();
  const record = registry.upsert(tab.id, {
    state: existing?.state === S.DONE ? S.DONE : S.ARMED,
    title: tab.title ?? '',
    url: tab.url ?? '',
    origin: originOf(tab.url),
    index,
    audible: Boolean(tab.audible),
    favIconUrl: tab.favIconUrl,
    error: undefined,
  });

  await renderBadge(tab.id, record.state);
  await registry.flush();
  log.info(`已授权 tab=${tab.id} index=${index} origin=${record.origin}`);
  return { ok: true, record };
}

/**
 * 取消授权（从列表移除）。
 *
 * 注意：这**不会**真正撤销浏览器侧的权限（没有 API 能做到），
 * 只是让我们不再把它算作候选。用户重新按快捷键即可再次加入。
 *
 * @param {number} tabId
 */
export async function disarmTab(tabId) {
  registry.remove(tabId);
  await renderBadge(tabId, S.IDLE);
  await registry.flush();
  log.info(`已从列表移除 tab=${tabId}`);
}

/**
 * 授权信念是否仍然可信 —— 通过比对 origin。
 *
 * 跨源主框架跳转会让浏览器撤销授权（ActiveTabPermissionGranter::
 * DidFinishNavigation 里只在 !IsSameOrigin() 时清除），所以 origin 变了
 * 就意味着授权没了。同源跳转和同文档跳转都不影响。
 *
 * @param {import('../common/protocol.js').TabRecord} record
 * @param {string} currentUrl
 */
export function grantLikelyValid(record, currentUrl) {
  if (!record.origin) return false;
  return originOf(currentUrl) === record.origin;
}

/**
 * 调试用：探测某 Tab 的授权是否真的有效。
 *
 * 铸造一个 streamId 然后丢弃 —— 它会在 10 秒后自然过期。
 *
 * ⚠️ 不作为默认路径使用：「被丢弃的待兑换审批是否会影响后续对同一 Tab 的捕获」
 * 这一点未经验证。仅供 M2 阶段的调试面板手动触发。
 *
 * @param {number} tabId
 * @returns {Promise<{ok:boolean, error?:string}>}
 */
export async function probeGrant(tabId) {
  try {
    const streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: tabId });
    log.debug(`探测成功 tab=${tabId}，streamId 长度 ${streamId.length}（将自然过期）`);
    return { ok: true };
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    log.debug(`探测失败 tab=${tabId}: ${message}`);
    return { ok: false, error: message };
  }
}
