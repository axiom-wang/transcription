// @ts-check
/**
 * Tab 生命周期监听 —— 授权失效判定与录制中断处理。
 *
 * ## 三条规则（Chromium 源码级已确认）
 *
 * 1. **跨源主框架跳转 → 授权被撤销。** 必须把该 Tab 降级为未授权。
 * 2. **同源 / 同文档跳转 → 授权存活。** 必须是空操作，不能误降级。
 * 3. **捕获会跟随页内跳转（frame tracker 会重定向）。** 所以正在录制的 Tab
 *    跳转时，录制不该中断 —— 即使跨源，已建立的流仍然有效（只是之后无法重启）。
 *
 * 规则 3 与规则 1 并不矛盾：跨源跳转杀掉的是**授权**，不是**已建立的流**。
 * 因此正在录制时跨源跳转，我们保持录制，但把「授权已失效」记下来，
 * 这样停止后 UI 就不会误让用户点「再次录制」。
 */

import { S } from '../common/states.js';
import { createLogger } from '../common/log.js';
import * as registry from './registry.js';
import { originOf, renderBadge, isCapturable } from './arming.js';

const log = createLogger('tab-watch');

/**
 * @typedef {object} Handlers
 * @property {(tabId:number, reason:string) => Promise<void>} onTabGone Tab 关闭/被替换
 * @property {(tabId:number) => Promise<void>} onGrantLost 授权失效（且未在录制）
 */

/** @type {Handlers | null} */
let handlers = null;

/**
 * @param {Handlers} h
 */
export function install(h) {
  handlers = h;

  chrome.tabs.onUpdated.addListener(handleUpdated);
  chrome.tabs.onRemoved.addListener(handleRemoved);
  chrome.tabs.onReplaced.addListener(handleReplaced);

  log.info('Tab 生命周期监听已安装');
}

/**
 * @param {number} tabId
 * @param {chrome.tabs.TabChangeInfo} changeInfo
 * @param {chrome.tabs.Tab} tab
 */
async function handleUpdated(tabId, changeInfo, tab) {
  // registry 可能还没从 storage 恢复（SW 刚被这个事件唤醒）。
  // 不先 hydrate 的话，下面的 get() 会返回 undefined 而静默跳过 ——
  // 于是跨源跳转导致的授权失效就被漏掉了。hydrate 是幂等的，代价可忽略。
  await registry.hydrate();

  const record = registry.get(tabId);
  if (!record) {
    return; // 不在我们的列表里，不关心
  }

  // 标题变化：更新用于文件名的标题。刻意**不**在录制中途更新 ——
  // 文件名应该反映用户开始录制时看到的那个标题，而不是中途被 SPA 改掉的版本。
  if (changeInfo.title && ![S.RECORDING, S.STOPPING, S.FINALIZING, S.SAVING].includes(record.state)) {
    registry.upsert(tabId, { title: changeInfo.title });
  }

  if (changeInfo.favIconUrl) {
    registry.upsert(tabId, { favIconUrl: changeInfo.favIconUrl });
  }

  // audible 只用于发现和排序，**永不**用作状态。
  // 它有约 2 秒的防抖且会在说话间隙抖动；而且被捕获的 Tab 是否还上报 audible
  // 本身未经验证。所以这里只记录，绝不据此改变状态或移除行。
  if (typeof changeInfo.audible === 'boolean') {
    registry.upsert(tabId, { audible: changeInfo.audible });
  }

  // 只有 URL 真正变化时才需要判断授权
  if (!changeInfo.url) return;

  const newOrigin = originOf(changeInfo.url);
  registry.upsert(tabId, { url: changeInfo.url });

  // 同源 → 授权存活，空操作
  if (newOrigin && newOrigin === record.origin) {
    log.debug(`tab=${tabId} 同源跳转，授权保持`);
    return;
  }

  // 跳到了不可捕获的页面（chrome:// 等）
  if (!isCapturable(changeInfo.url)) {
    log.info(`tab=${tabId} 跳转到不可捕获页面，授权失效`);
  } else {
    log.info(`tab=${tabId} 跨源跳转 ${record.origin} → ${newOrigin}，授权失效`);
  }

  // 正在录制中：**保持录制**。已建立的流会跟随跳转（frame tracker 重定向），
  // 杀掉的只是授权。把 origin 清空以标记「停止后不能再次录制」。
  if ([S.RECORDING, S.STARTING, S.STOPPING, S.FINALIZING, S.SAVING].includes(record.state)) {
    registry.upsert(tabId, { origin: '' });
    log.info(`tab=${tabId} 录制中跨源跳转，录制继续但授权已失效`);
    return;
  }

  // 未在录制：降级为未授权
  await handlers?.onGrantLost(tabId);
}

/**
 * @param {number} tabId
 */
async function handleRemoved(tabId) {
  await registry.hydrate(); // 同上：SW 可能刚被这个事件唤醒
  const record = registry.get(tabId);
  if (!record) return;
  log.info(`tab=${tabId} 已关闭（状态 ${record.state}）`);
  await handlers?.onTabGone(tabId, 'tab_closed');
}

/**
 * Tab 被替换（预渲染提交等）。旧 tabId 失效，授权不会转移到新 id。
 * @param {number} addedTabId
 * @param {number} removedTabId
 */
async function handleReplaced(addedTabId, removedTabId) {
  await registry.hydrate(); // 同上：SW 可能刚被这个事件唤醒
  const record = registry.get(removedTabId);
  if (!record) return;
  log.info(`tab=${removedTabId} 被替换为 ${addedTabId}`);
  await handlers?.onTabGone(removedTabId, 'tab_replaced');
}

/**
 * 全量对账：检查所有已知 Tab 是否仍然存在、origin 是否仍匹配。
 *
 * SW 重启后调用一次 —— 我们在被杀期间错过的所有事件，都靠这一次扫描补上。
 *
 * @returns {Promise<{gone:number[], grantLost:number[]}>}
 */
export async function reconcileTabs() {
  /** @type {number[]} */
  const gone = [];
  /** @type {number[]} */
  const grantLost = [];
  let closedWithRecording = 0;

  for (const record of registry.all()) {
    /** @type {chrome.tabs.Tab | null} */
    let tab = null;
    try {
      tab = await chrome.tabs.get(record.tabId);
    } catch {
      tab = null;
    }

    if (!tab && typeof record.downloadId === 'number') {
      // Keep saved recordings available after the source tab closes; only its grant is gone.
      if (record.origin) {
        registry.upsert(record.tabId, { origin: '' });
        closedWithRecording += 1;
      }
      continue;
    }
    if (!tab) {
      gone.push(record.tabId);
      continue;
    }

    // 更新可能已变化的元数据
    registry.upsert(record.tabId, {
      title: tab.title ?? record.title,
      url: tab.url ?? record.url,
      audible: Boolean(tab.audible),
      favIconUrl: tab.favIconUrl,
    });

    // 已在录制中的不做授权判定 —— 流仍然有效
    if ([S.RECORDING, S.STARTING, S.STOPPING, S.FINALIZING, S.SAVING].includes(record.state)) {
      continue;
    }

    if (record.origin && originOf(tab.url) !== record.origin) {
      grantLost.push(record.tabId);
    }
  }

  for (const tabId of gone) {
    registry.remove(tabId);
  }
  for (const tabId of grantLost) {
    if (registry.get(tabId)?.state === S.DONE) registry.upsert(tabId, { origin: '' });
    else registry.setState(tabId, S.IDLE, { origin: '' });
    await renderBadge(tabId, S.IDLE);
  }

  if (gone.length > 0 || grantLost.length > 0 || closedWithRecording > 0) {
    log.info(`对账：${gone.length} 个 Tab 已消失，${grantLost.length} 个授权失效`);
    await registry.flush();
  }

  return { gone, grantLost };
}
