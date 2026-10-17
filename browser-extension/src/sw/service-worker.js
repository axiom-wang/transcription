// @ts-check
/**
 * Service worker —— 消息路由、授权入口、以及**顺序 streamId 铸造循环**。
 *
 * SW 随时可能被杀，所以这里不持有任何不可重建的状态：
 * 权威状态在 offscreen 文档（它真正持有 MediaRecorder），
 * 镜像在 chrome.storage.session（供 panel 读取和 SW 重启后恢复）。
 */

import { S, E } from '../common/states.js';
import { createLogger } from '../common/log.js';
import { UI, OFF, EV, isFor, sendTo, withTimeout } from '../common/protocol.js';
import { formatSessionStamp } from '../common/filename.js';
import * as store from '../common/chunk-store.js';
import * as registry from './registry.js';
import * as arming from './arming.js';
import { ensureOffscreen, ping, exists as offscreenExists } from './offscreen-manager.js';
import * as tabWatch from './tab-watch.js';
import { importDownload, sendToTingji } from './tingji.js';

const log = createLogger('sw');

/** streamId 的 TTL 是 10 秒；用 8 秒超时，这样超时几乎必然意味着「已过期」。 */
const REDEEM_TIMEOUT_MS = 8000;

/** 默认录制参数 */
const DEFAULT_OPTS = {
  /** 必须 > 0：分块落盘依赖 timeslice，同时把崩溃时的数据丢失上限压到 1 秒 */
  timesliceMs: 1000,
  audioBitsPerSecond: 128000,
  /**
   * 默认**不**把音频回放给用户 —— 后台静默录制，不打扰当前工作。
   *
   * 注意这不影响录音文件：录制接的是原始 MediaStream，回放是另分的一路
   * （见 recorder.js 的说明）。静音只是把那条链路的 GainNode 置 0。
   *
   * 链路本身始终建立 —— AnalyserNode 的活性采样是面板上
   * 「有声 / 静音」指示的数据来源，静默录制时尤其需要它。
   */
  monitor: false,
  docType: 'matroska',
  extension: 'mkv',
};

/** 偏好设置的 storage.local 键前缀 */
const PREF_PREFIX = 'pref.';

/**
 * 读取偏好设置。
 *
 * 偏好存 `storage.local`（要跨浏览器重启保留），与 `storage.session` 里的
 * 状态镜像分开 —— 后者的生命周期刻意与 tabCapture 授权对齐，会话结束即失效。
 *
 * @template T
 * @param {string} key
 * @param {T} fallback
 * @returns {Promise<T>}
 */
async function getPref(key, fallback) {
  try {
    const got = await chrome.storage.local.get(PREF_PREFIX + key);
    const value = got[PREF_PREFIX + key];
    return value === undefined ? fallback : value;
  } catch (err) {
    log.warn(`读取偏好 ${key} 失败，用默认值`, err);
    return fallback;
  }
}

/**
 * @param {string} key
 * @param {any} value
 */
async function setPref(key, value) {
  await chrome.storage.local.set({ [PREF_PREFIX + key]: value });
}

// ───────────────────────────── 启动与恢复 ─────────────────────────────

chrome.runtime.onInstalled.addListener(async (details) => {
  log.info(`onInstalled: ${details.reason}`);

  // 扩展安装/更新会**撤销全部 tabCapture 授权**（OnExtensionUnloaded 会清空
  // ActiveTabPermissionGranter）。所以之前记录的「已授权」全部作废，必须清空 ——
  // 留着只会让用户点「开始录制」然后收到一堆 E_GRANT_LOST。
  if (details.reason === 'install' || details.reason === 'update') {
    await registry.hydrate();
    const stale = registry.all();
    registry.clear();
    for (const r of stale) {
      await arming.renderBadge(r.tabId, S.IDLE);
    }
    await registry.flush();
    log.info(`扩展重载，已清空 ${stale.length} 条失效的授权记录`);
  }

  setupContextMenu();
});

chrome.runtime.onStartup.addListener(() => {
  log.info('onStartup');
  void bootstrap();
});

/** @type {Promise<void> | null} */
let bootstrapping = null;
/**
 * bootstrap 是否已成功完成。
 *
 * 必须与 `bootstrapping`（在途 promise）分开：前者表示「这个 SW 生命周期内已经做过」，
 * 后者只用于让并发调用共享同一次执行。
 *
 * ⚠️ 曾经这里只有 `bootstrapping`，并在 finally 里置 null —— 于是
 * handleMessage 每条消息都会**完整重跑一次对账**：PING offscreen、
 * 给每个已知标签页调 tabs.get()、重绘所有徽标。8 个标签页时，
 * panel 每次刷新都会放大成十几次 API 调用。
 */
let bootstrapped = false;

/**
 * SW 冷启动后的状态恢复与对账。
 *
 * 顺序很重要：先读镜像 → 再问 offscreen（它赢冲突）→ 再对账 Tab → 最后重绘徽标。
 *
 * **每个 SW 生命周期只真正执行一次。** 之后的调用是廉价的空操作 ——
 * 各处入口（消息处理、快捷键、图标点击）都会无条件 `await bootstrap()`，
 * 靠的就是这个幂等性。
 */
function bootstrap() {
  if (bootstrapped) return Promise.resolve();   // 已完成，直接返回
  if (bootstrapping) return bootstrapping;      // 在途，共享同一次执行

  bootstrapping = (async () => {
    try {
      await registry.hydrate();

      // offscreen 是权威状态所有者。它若还活着，以它的说法为准。
      if (await offscreenExists()) {
        const result = await ping();
        if (result.ok && result.tabs) {
          reconcileWithOffscreen(result.tabs);
        }
      } else {
        // offscreen 不在了 —— 任何「录制中」的记录都已经名存实亡
        for (const r of registry.all()) {
          if ([S.STARTING, S.RECORDING, S.STOPPING, S.FINALIZING, S.SAVING].includes(r.state)) {
            registry.setState(r.tabId, S.ERROR, {
              error: { code: E.LOST, message: '录制在扩展重启期间中断' },
            });
          }
        }
      }

      await tabWatch.reconcileTabs();
      await arming.refreshAllBadges(); // SW 重启会丢失全部徽标
      await registry.flush();
      bootstrapped = true;
      // A completed download may have woken a fresh worker after its blob map was lost.
      for (const row of registry.all()) {
        if (row.tingji === 'sending' && !imports.has(row.downloadId)) {
          registry.upsert(row.tabId, { tingji: 'error', tingjiMessage: '连接已中断，可重试发送；相同录音会自动复用' });
        }
        if (typeof row.downloadId === 'number' && !row.downloadComplete) {
          void checkDownload(row.downloadId).catch((err) => log.warn('下载对账失败', err));
        }
      }
    } catch (err) {
      // 刻意**不**置 bootstrapped —— 失败后允许下次调用重试，
      // 否则一次偶发失败会让整个 SW 生命周期都停留在未对账状态。
      log.error('bootstrap 失败，下次调用将重试', err);
    } finally {
      bootstrapping = null;
    }
  })();
  return bootstrapping;
}

/**
 * 用 offscreen 的权威状态覆盖本地镜像。
 * @param {import('../common/protocol.js').OffscreenTabState[]} offscreenTabs
 */
function reconcileWithOffscreen(offscreenTabs) {
  const byId = new Map(offscreenTabs.map((t) => [t.tabId, t]));

  // ① offscreen 说在录的，一律采信（含 SW 完全不知道的 —— 收养它）
  for (const t of offscreenTabs) {
    const local = registry.get(t.tabId);
    if (!local) {
      log.info(`收养 offscreen 报告的未知录制 tab=${t.tabId}`);
      registry.upsert(t.tabId, {
        state: t.state, startedAt: t.startedAt, bytes: t.bytes,
        monitor: t.monitor, index: registry.nextIndex(),
      });
    } else if (local.state !== t.state) {
      log.info(`以 offscreen 为准修正 tab=${t.tabId}: ${local.state} → ${t.state}`);
      registry.upsert(t.tabId, {
        state: t.state, startedAt: t.startedAt, bytes: t.bytes, monitor: t.monitor,
      });
    }
  }

  // ② 镜像里卡在过渡态、但 offscreen 不知道的 —— 是孤儿
  for (const r of registry.all()) {
    if (byId.has(r.tabId)) continue;
    if ([S.STARTING, S.RECORDING, S.STOPPING, S.FINALIZING, S.SAVING].includes(r.state)) {
      log.warn(`tab=${r.tabId} 在镜像里是 ${r.state} 但 offscreen 不知道 —— 判定为孤儿`);
      registry.setState(r.tabId, S.ERROR, {
        error: { code: E.LOST, message: '录制状态丢失（扩展可能已重启）' },
      });
    }
  }
}

// ───────────────────────────── 授权入口 ─────────────────────────────

/**
 * 点击扩展图标：**同时**授权当前 Tab 并打开侧边栏。
 *
 * 两个前提，缺一个这条路径就废掉：
 *
 * ① manifest 里**没有** default_popup —— 设了就会吞掉 onClicked。
 * ② `chrome.sidePanel.open()` **必须在用户手势中同步调用**。
 *    ⚠️ 任何 `await` 都会让手势上下文失效，之后再调 open() 会抛
 *    "sidePanel.open() may only be called in response to a user gesture"。
 *    所以下面**先**同步发起 open()，把 bootstrap/armTab 全部推到它后面。
 *    这个顺序不是风格问题，是硬约束 —— 别把 await 挪到 open() 前面。
 */
chrome.action.onClicked.addListener((tab) => {
  // ① 先同步发起，抢在手势失效之前。不 await。
  if (typeof tab.windowId === 'number') {
    chrome.sidePanel.open({ windowId: tab.windowId }).catch((err) => {
      // 这里必须是 error 而不是 warn：静默失败会表现为「点图标没反应」，
      // 而那正是最难排查的一类症状。
      log.error('打开侧边栏失败', err);
    });
  }

  // ② 授权走异步。bootstrap 必须先跑 —— SW 可能刚被这次点击唤醒，
  //    registry 还没从 storage 恢复，nextIndex() 会从 1 重来并覆盖已有序号。
  void (async () => {
    try {
      await bootstrap();
      const result = await arming.armTab(tab);
      log.info(`图标点击授权: ${result.ok ? '成功' : result.reason}`);
    } catch (err) {
      log.error('图标点击授权失败', err);
    }
  })();
});

chrome.commands.onCommand.addListener(async (command, tab) => {
  log.debug(`快捷键: ${command}`);
  await bootstrap(); // 同上：registry 必须先恢复

  if (command === 'arm-current-tab') {
    if (!tab) {
      const [active] = await chrome.tabs.query({ active: true, currentWindow: true });
      tab = active;
    }
    if (tab) {
      const result = await arming.armTab(tab);
      log.info(`快捷键授权: ${result.ok ? '成功' : result.reason}`);
    }
  } else if (command === 'toggle-record-all') {
    const active = registry.activeTabIds();
    if (active.length > 0) {
      await stopMany(active, 'user_shortcut');
    } else {
      await startMany(registry.armedTabIds());
    }
  }
});

const MENU_ID = 'arm-this-tab';

function setupContextMenu() {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: MENU_ID,
      title: '将此标签页加入录制',
      contexts: ['page', 'video', 'audio'],
    });
  });
}

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId !== MENU_ID || !tab) return;
  await bootstrap(); // 同上：registry 必须先恢复
  const result = await arming.armTab(tab);
  log.info(`右键菜单授权: ${result.ok ? '成功' : result.reason}`);
});

// ─────────────────── ★ 顺序 streamId 铸造循环（核心） ───────────────────

/**
 * 启动多个 Tab 的录制。
 *
 * ## 为什么必须严格顺序
 *
 * `streamId` **单次使用、10 秒 TTL**（kApprovedStreamTimeToLiveSeconds = 10）。
 * 若一次铸造 N 个再并发分发，那么在第 1 个还在跑 getUserMedia 时，
 * 第 N 个的 TTL 已经在倒计时了 —— Tab 越多越容易超时，且失败是随机的、难复现的。
 *
 * 所以这里一个一个来，**等上一个兑换完成（ack）再铸下一个**。
 * 两个附带性质：任意时刻最多只有一个未兑换的 id；循环本身的消息往返
 * 会不断重置 SW 的空闲计时器，天然保活。
 *
 * @param {number[]} tabIds
 * @returns {Promise<Array<{tabId:number, ok:boolean, error?:{code:string,message:string}}>>}
 */
export async function startMany(tabIds) {
  /** @type {Array<{tabId:number, ok:boolean, error?:{code:string,message:string}}>} */
  const results = [];

  if (tabIds.length === 0) return results;

  // 必须在任何铸造之前把 offscreen 准备好 —— 否则第一个 streamId 会在
  // 等待文档创建的过程中白白消耗 TTL。
  try {
    await ensureOffscreen();
  } catch (err) {
    const error = { code: E.NO_OFFSCREEN, message: '无法创建录制上下文' };
    log.error('创建 offscreen 失败', err);
    for (const tabId of tabIds) {
      registry.setState(tabId, S.ERROR, { error });
      await arming.renderBadge(tabId, S.ERROR);
      results.push({ tabId, ok: false, error });
    }
    await registry.flush();
    return results;
  }

  // 一次「开始录制」= 一个会话 = 一个输出子目录
  const now = new Date();
  const session = {
    id: `${now.getTime().toString(36)}`,
    stamp: formatSessionStamp(now),
    startedAt: now.getTime(),
  };
  registry.setSession(session);

  // 读一次全局回放偏好，整批共用。
  // 这修掉了一个真实缺陷：以前每次都用写死的 DEFAULT_OPTS.monitor，
  // 于是用户这次静音了、停止后再录又会响 —— 界面让人以为设置会保留，实际不会。
  const monitor = await getPref('monitor', DEFAULT_OPTS.monitor);
  const opts = {
    ...DEFAULT_OPTS,
    monitor,
    sessionId: session.id,
    sessionStamp: session.stamp,
  };

  for (const tabId of tabIds) {          // ← 严格顺序，绝不用 Promise.all
    const record = registry.get(tabId);
    if (!record) {
      results.push({ tabId, ok: false, error: { code: E.TAB_GONE, message: '标签页已不在列表中' } });
      continue;
    }

    try {
      // setState 会**拒绝**非法转移并返回 null（例如该 Tab 已经在录制中，
      // 或者用户连点了两次「开始」）。必须检查返回值 —— 否则会给一个
      // 已在录制的 Tab 再铸一个 streamId，结果是 Chrome 报
      // "Cannot capture a tab with an active stream"，而且第一个录制会被搅乱。
      if (record.tingji === 'sending' || record.downloadComplete === false) {
        results.push({ tabId, ok: false, error: { code: E.TAB_BUSY, message: '请等待录音保存和发送完成后再录制' } });
        continue;
      }
      if (!registry.setState(tabId, S.STARTING)) {
        results.push({
          tabId, ok: false,
          error: { code: E.TAB_BUSY, message: '该标签页当前无法开始录制（可能已在录制中）' },
        });
        continue;
      }
      registry.upsert(tabId, { downloadId: undefined, downloadComplete: undefined,
        downloadBlobUrl: undefined, tingji: undefined, tingjiMessage: undefined, filename: undefined });
      await arming.renderBadge(tabId, S.STARTING);

      // 取最新的标题（文件名要用），顺便确认 Tab 还在
      /** @type {chrome.tabs.Tab} */
      let tab;
      try {
        tab = await chrome.tabs.get(tabId);
      } catch {
        throw Object.assign(new Error('标签页已关闭'), { code: E.TAB_GONE });
      }

      // ── t0：铸造。此刻起 10 秒内必须兑换 ──
      const streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: tabId });

      const ack = await withTimeout(
        sendTo('offscreen', OFF.START_ONE, {
          tabId,
          streamId,
          meta: {
            title: tab.title ?? record.title ?? '',
            url: tab.url ?? record.url ?? '',
            index: record.index,
          },
          opts,
        }),
        REDEEM_TIMEOUT_MS,
        '兑换 streamId',
      );

      if (!ack?.ok) {
        throw Object.assign(
          new Error(ack?.error?.message ?? '录制上下文未能启动录制'),
          { code: ack?.error?.code ?? E.GUM_FAILED },
        );
      }

      // ack 到达 = getUserMedia 已返回且 MediaRecorder.start() 已调用
      registry.setState(tabId, S.RECORDING, {
        startedAt: Date.now(), bytes: 0, monitor,
      });
      await arming.renderBadge(tabId, S.RECORDING);
      results.push({ tabId, ok: true });
      log.info(`tab=${tabId} 录制已启动`);
    } catch (err) {
      // 单个 Tab 失败**绝不中断**其余 —— 部分成功是完全正常的情形
      const error = classifyError(err);
      registry.setState(tabId, S.ERROR, { error });
      await arming.renderBadge(tabId, S.ERROR);
      results.push({ tabId, ok: false, error });
      log.warn(`tab=${tabId} 启动失败: ${error.code} ${error.message}`);
    }
  }

  await registry.flush();
  return results;
}

/**
 * 把底层错误映射为带中文文案的错误码。
 * @param {unknown} err
 * @returns {{code:string, message:string}}
 */
function classifyError(err) {
  const raw = err instanceof Error ? err.message : String(err);
  const explicit = /** @type {any} */ (err)?.code;
  if (typeof explicit === 'string') {
    return { code: explicit, message: chineseFor(explicit, raw) };
  }

  // 同一个 Tab 已经有活跃捕获（可能是本扩展重复启动，也可能是别的扩展占用）
  if (/active stream|Cannot capture a tab with an active stream/i.test(raw)) {
    return { code: E.TAB_BUSY, message: '该标签页已被占用（可能有其他录制程序）' };
  }
  // 授权不在了
  if (/not been invoked|activeTab|Chrome pages cannot be captured/i.test(raw)) {
    return { code: E.GRANT_LOST, message: '授权已失效，请切到该标签页重新按快捷键授权' };
  }
  if (/超时/.test(raw)) {
    return { code: E.STREAM_EXPIRED, message: '启动超时（授权令牌已过期），请重试' };
  }
  if (/No tab with id|No such tab/i.test(raw)) {
    return { code: E.TAB_GONE, message: '标签页已关闭' };
  }
  if (/Receiving end does not exist/i.test(raw)) {
    return { code: E.NO_OFFSCREEN, message: '录制上下文已丢失，请重试' };
  }
  return { code: E.UNKNOWN, message: raw };
}

/**
 * @param {string} code
 * @param {string} fallback
 */
function chineseFor(code, fallback) {
  switch (code) {
    case E.GRANT_LOST: return '授权已失效，请切到该标签页重新按快捷键授权';
    case E.TAB_BUSY: return '该标签页已被占用（可能有其他录制程序）';
    case E.STREAM_EXPIRED: return '启动超时（授权令牌已过期），请重试';
    case E.TAB_GONE: return '标签页已关闭';
    case E.NO_OFFSCREEN: return '录制上下文已丢失，请重试';
    case E.GUM_FAILED: return '无法获取该标签页的音频流';
    case E.NO_DATA: return '没有录到任何音频数据';
    case E.RECORDER: return '录制器内部错误';
    case E.DOWNLOAD: return '文件保存失败';
    case E.STORE: return '录音数据写入失败';
    default: return fallback;
  }
}

/**
 * 停止若干 Tab 的录制。
 * @param {number[]} tabIds
 * @param {string} reason
 */
export async function stopMany(tabIds, reason = 'user') {
  if (tabIds.length === 0) return { ok: true, tabIds: [] };
  try {
    await sendTo('offscreen', OFF.STOP_ALL, { tabIds, reason });
  } catch (err) {
    log.warn('停止指令发送失败', err);
  }
  return { ok: true, tabIds };
}

// ───────────────────────────── 消息路由 ─────────────────────────────

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  // ⚠️ 关键：sendMessage 会广播到所有上下文。不是发给我们的必须**立刻 return false**。
  // 在这里 return true 会让 Chrome 一直等一个永不到来的回复，
  // 并且把真正目标上下文的回复吞掉 —— 这是本模式最常见的 bug。
  if (!isFor(message, 'sw')) return false;

  handleMessage(message)
    .then(sendResponse)
    .catch((err) => {
      log.error(`处理 ${message?.type} 失败`, err);
      sendResponse({ ok: false, error: classifyError(err) });
    });

  return true; // 我们确实会异步回复
});

/**
 * @param {any} message
 */
async function handleMessage(message) {
  await bootstrap();

  switch (message.type) {
    case UI.GET_SNAPSHOT:
      // 顺带刷新一次可恢复列表 —— panel 每次打开都会问一次快照，
      // 这是最自然的刷新点。以前只依赖 offscreen 加载时主动上报，
      // 一旦 offscreen 已在运行（比如刚保存失败），panel 就永远看不到提示。
      await refreshRecoverable();
      return { ok: true, snapshot: await buildSnapshot() };

    case UI.LIST_CANDIDATES:
      return { ok: true, tabs: await listCandidates() };

    case UI.START:
      return { ok: true, results: await startMany(message.tabIds ?? []) };

    case UI.START_ALL:
      return { ok: true, results: await startMany(registry.armedTabIds()) };

    case UI.STOP:
      return await stopMany(message.tabIds ?? [], 'user');

    case UI.STOP_ALL:
      return await stopMany(registry.activeTabIds(), 'user');

    case UI.DISARM:
      await arming.disarmTab(message.tabId);
      return { ok: true };

    case UI.RETRY:
      return { ok: true, results: await startMany([message.tabId]) };

    case UI.SET_MONITOR:
      registry.upsert(message.tabId, { monitor: message.enabled });
      await registry.flush();
      try {
        await sendTo('offscreen', OFF.SET_MONITOR, {
          tabId: message.tabId, enabled: message.enabled,
        });
      } catch (err) {
        log.warn('切换监听失败', err);
      }
      return { ok: true };

    case UI.ACTIVATE_TAB: {
      const tab = await chrome.tabs.update(message.tabId, { active: true });
      if (tab && typeof tab.windowId === 'number') {
        await chrome.windows.update(tab.windowId, { focused: true });
      }
      return { ok: true };
    }

    case UI.DEBUG_PROBE_GRANT:
      return await arming.probeGrant(message.tabId);

    case UI.SEND_TO_TINGJI:
      return await sendRecording(message.tabId);
    case UI.OPEN_TINGJI:
      return await sendToTingji({ type: 'open' });
    case UI.PING_TINGJI:
      return await sendToTingji({ type: 'ping' });

    case UI.SET_PREF: {
      if (message.key === 'autoTingji') {
        await chrome.storage.local.set({ autoTingji: Boolean(message.value) });
      } else {
        await setPref(message.key, message.value);
      }
      return { ok: true };
    }

    case UI.GET_PREFS: {
      return {
        ok: true,
        prefs: { monitor: await getPref('monitor', DEFAULT_OPTS.monitor),
          autoTingji: (await chrome.storage.local.get('autoTingji')).autoTingji === true },
      };
    }

    /**
     * 全局回放开关：一次改掉所有正在录制的标签页，并记住这个选择。
     *
     * 存偏好 + 应用到进行中的录制是两件事，这里一起做完 ——
     * 否则用户会看到「开关变了但声音没变」，或者「这次变了下次又还原」。
     */
    case UI.SET_MONITOR_ALL: {
      const enabled = Boolean(message.enabled);
      await setPref('monitor', enabled);

      const active = registry.activeTabIds();
      for (const tabId of active) {
        registry.upsert(tabId, { monitor: enabled });
      }
      await registry.flush();

      if (active.length > 0) {
        try {
          await sendTo('offscreen', OFF.SET_MONITOR_ALL, { enabled });
        } catch (err) {
          log.warn('批量切换回放失败', err);
        }
      }
      log.info(`全局回放已${enabled ? '开启' : '静音'}（影响 ${active.length} 个录制中标签页）`);
      return { ok: true, enabled, affected: active.length };
    }

    // ── 崩溃恢复：保存 / 丢弃 IDB 里残留的录音 ──
    case UI.RECOVER_SAVE: {
      // 恢复需要 offscreen 在场（IDB 数据和保存逻辑都在那边）。
      // 典型场景是 offscreen 已经死过一次，所以这里必须先把它重建起来。
      await ensureOffscreen();
      const reply = await sendTo('offscreen', OFF.RECOVER_SAVE, {
        sessionId: message.sessionId, tabId: message.tabId,
      });
      if (reply?.ok) {
        registry.upsert(message.tabId, { state: S.DONE, filename: reply.filename,
          downloadId: reply.downloadId });
        registry.removeRecoverable(message.sessionId, message.tabId);
        await registry.flush();
        log.info(`已恢复保存 tab=${message.tabId}: ${reply.filename}`);
      }
      return reply ?? { ok: false, error: { code: E.UNKNOWN, message: '恢复保存无响应' } };
    }

    case UI.RECOVER_DISCARD: {
      await ensureOffscreen();
      await sendTo('offscreen', OFF.RECOVER_DISCARD, {
        sessionId: message.sessionId, tabId: message.tabId,
      }).catch((err) => log.warn('丢弃可恢复录音失败', err));
      registry.removeRecoverable(message.sessionId, message.tabId);
      await registry.flush();
      return { ok: true };
    }

    // ── 来自 offscreen 的主动上报 ──
    case EV.TAB_STATE: {
      // 显式挑选字段，**不要** `...rest`：消息里还有 v/to/type/at 这些信封字段，
      // 一并铺进记录会污染 storage 镜像，也会让 panel 读到无意义的键。
      const { tabId, state, bytes, durationMs, error, audio } = message;
      /** @type {Partial<import('../common/protocol.js').TabRecord>} */
      const extra = {};
      if (typeof bytes === 'number') extra.bytes = bytes;
      if (typeof durationMs === 'number') extra.durationMs = durationMs;
      if (error) extra.error = error;
      // 音频活性快照：只接受结构完整的对象，拒绝一切畸形数据进 storage
      if (audio && typeof audio.silentMs === 'number' && typeof audio.hasSound === 'boolean') {
        extra.audio = audio;
      }

      registry.setState(tabId, state, extra);
      await arming.renderBadge(tabId, state);
      return { ok: true };
    }

    case EV.SAVED: {
      const { tabId, filename, bytes, durationMs, downloadId } = message;
      const extra = { filename, bytes, durationMs };
      if (typeof downloadId === 'number') extra.downloadId = downloadId;
      registry.setState(tabId, S.DONE, extra);
      await arming.renderBadge(tabId, S.DONE);
      await registry.flush();
      log.info(`tab=${tabId} 已保存: ${filename}`);
      return { ok: true };
    }

    case EV.RECOVERABLE: {
      // offscreen 在启动时发现 IDB 里有未保存的录音。
      // 存进 registry 并镜像到 storage，这样 panel 打开时能看到提示 ——
      // 即使那时 offscreen 已经又被关掉了。
      const sessions = Array.isArray(message.sessions) ? message.sessions : [];
      registry.setRecoverable(sessions);
      await registry.flush();
      log.info(`offscreen 报告 ${sessions.length} 段未保存的录音`);
      return { ok: true };
    }

    case EV.REQUEST_DOWNLOAD:
      return await handleDownloadRequest(message);

    default:
      log.warn(`未知消息类型: ${message.type}`);
      return { ok: false, error: { code: E.UNKNOWN, message: `未知消息: ${message.type}` } };
  }
}

// ─────────────────────── 下载（只能在 SW 里做） ───────────────────────

/**
 * 正在进行的下载：downloadId → blobUrl。
 * 用于在下载到达终态后通知 offscreen 回收那个 URL。
 * @type {Map<number, string>}
 */
const activeDownloads = new Map();

let downloadListenerInstalled = false;

/**
 * 安装 downloads.onChanged 监听器，在终态时让 offscreen 回收 blob URL。
 *
 * **只在终态回收**（complete / interrupted）—— 下载进行中回收会截断文件。
 */
function installDownloadListener() {
  if (downloadListenerInstalled) return;
  downloadListenerInstalled = true;

  chrome.downloads.onChanged.addListener((delta) => {
    const state = delta.state?.current;
    if (state !== 'complete' && state !== 'interrupted') return;

    // blob URL 必须在**创建它的上下文**回收，也就是 offscreen 文档。
    // 这里发不出去也无所谓：offscreen 侧有兜底计时器。
    void finishDownload(delta.id, state).catch((err) => log.warn('下载状态更新失败', err));

    if (state === 'interrupted') {
      log.warn(`下载 #${delta.id} 被中断: ${delta.error?.current ?? '未知原因'}`);
    } else {
      log.info(`下载 #${delta.id} 已完成`);
    }
  });
}

/**
 * 执行下载。由 offscreen 通过 REQUEST_DOWNLOAD 委托过来。
 *
 * `chrome.downloads` 只在 SW（和普通扩展页）里存在，而 `URL.createObjectURL`
 * 只在 offscreen 里存在 —— 所以这个来回是两个上下文限制夹出来的唯一解，
 * 不是绕远路。详见 offscreen/saver.js 的文件头说明。
 *
 * @param {any} message
 */
async function handleDownloadRequest(message) {
  installDownloadListener();

  const { blobUrl, filename, fallbackFilename } = message;
  if (typeof blobUrl !== 'string' || !blobUrl) {
    return { ok: false, error: { code: E.DOWNLOAD, message: '缺少 blob URL' } };
  }

  try {
    const downloadId = await startDownload(blobUrl, filename);
    activeDownloads.set(downloadId, blobUrl);
    await rememberDownload(message, downloadId, filename);
    return { ok: true, downloadId, filename };
  } catch (err) {
    const first = err instanceof Error ? err.message : String(err);
    log.warn(`主文件名被拒绝（${first}），改用 ASCII 兜底名重试`);

    // 文件名问题绝不该变成数据丢失 —— 用纯 ASCII 名再试一次
    try {
      const downloadId = await startDownload(blobUrl, fallbackFilename);
      activeDownloads.set(downloadId, blobUrl);
      await rememberDownload(message, downloadId, fallbackFilename);
      return { ok: true, downloadId, filename: fallbackFilename, usedFallback: true };
    } catch (err2) {
      const second = err2 instanceof Error ? err2.message : String(err2);
      log.error(`下载失败（含兜底名）: ${second}`);
      return { ok: false, error: { code: E.DOWNLOAD, message: second } };
    }
  }
}

// Register synchronously on every worker load, so download events can wake it.
installDownloadListener();

/** @param {any} message @param {number} downloadId @param {string} filename */
async function rememberDownload(message, downloadId, filename) {
  registry.upsert(message.tabId, { downloadId, filename, downloadComplete: false,
    downloadBlobUrl: message.blobUrl, tingji: undefined, tingjiMessage: undefined });
  await registry.flush();
  // Covers a tiny download that finished before its ID was persisted.
  void checkDownload(downloadId).catch((err) => log.warn('下载状态检查失败', err));
}

/** @param {number} downloadId */
async function checkDownload(downloadId) {
  const [download] = await chrome.downloads.search({ id: downloadId });
  if (download && ['complete', 'interrupted'].includes(download.state)) {
    await finishDownload(downloadId, download.state);
  }
}

/** @param {number} downloadId @param {string} state */
async function finishDownload(downloadId, state) {
  await registry.hydrate();
  const row = registry.all().find((r) => r.downloadId === downloadId);
  const blobUrl = activeDownloads.get(downloadId) ?? row?.downloadBlobUrl;
  activeDownloads.delete(downloadId);
  if (blobUrl) void sendTo('offscreen', OFF.REVOKE_URL, { blobUrl }).catch(() => {});
  if (!row) return; // Never send unrelated browser downloads.
  registry.upsert(row.tabId, { downloadComplete: state === 'complete' ? true : undefined,
    downloadBlobUrl: undefined });
  await registry.flush();
  if (state === 'interrupted') {
    registry.upsert(row.tabId, { tingji: 'error', tingjiMessage: '录音下载中断，请重新保存后发送' });
    await registry.flush();
  } else if (!registry.get(row.tabId)?.tingji &&
      (await chrome.storage.local.get('autoTingji')).autoTingji === true) {
    await sendRecording(row.tabId);
  }
}

/** @type {Map<number, Promise<any>>} */
const imports = new Map();

/** @param {number} tabId */
async function sendRecording(tabId) {
  const row = registry.get(tabId);
  if (!row || typeof row.downloadId !== 'number') {
    return { ok: false, error: '请先保存录音后再送入听记' };
  }
  const downloadId = row.downloadId;
  if (imports.has(downloadId)) return imports.get(downloadId);
  const task = (async () => {
    registry.upsert(tabId, { tingji: 'sending', tingjiMessage: '正在送入听记…' });
    await registry.flush();
    const result = await importDownload(downloadId);
    if (registry.get(tabId)?.downloadId === downloadId) {
      registry.upsert(tabId, {
        tingji: result.ok ? (result.reused ? 'reused' : 'queued') : 'error',
        tingjiMessage: result.ok
          ? (result.reused ? '已有相同录音，直接复用' : '已加入听记队列')
          : result.error,
      });
      await registry.flush();
    }
    return result;
  })();
  imports.set(downloadId, task);
  try { return await task; } finally { imports.delete(downloadId); }
}

/**
 * @param {string} url
 * @param {string} filename
 * @returns {Promise<number>}
 */
function startDownload(url, filename) {
  return new Promise((resolve, reject) => {
    chrome.downloads.download(
      {
        url,
        filename,
        saveAs: false,              // 不弹对话框 —— N 个文件就是 N 次打断
        conflictAction: 'uniquify', // 重名交给 Chrome，自动追加 " (1)"
      },
      (downloadId) => {
        const err = chrome.runtime.lastError;
        if (err || typeof downloadId !== 'number') {
          reject(new Error(err?.message ?? '下载未能启动'));
          return;
        }
        resolve(downloadId);
      },
    );
  });
}

async function buildSnapshot() {
  return {
    tabs: registry.all(),
    session: registry.getSession(),
    recoverable: registry.getRecoverable(),
    activeCount: registry.activeTabIds().length,
    armedCount: registry.armedTabIds().length,
  };
}

/**
 * 主动读取 IDB 里未保存的录音，刷新镜像供 panel 显示。
 *
 * ## 为什么 SW 自己读，而不是问 offscreen
 *
 * 产生未保存录音的**主要原因就是扩展重载**，而那时 offscreen 文档并不存在。
 * 若把读取绑在「先问 offscreen」上，这个提示恰好在最需要它的场景下不出现。
 *
 * `chunk-store.js` 只依赖 indexedDB / IDBKeyRange，两者在 SW 里同样可用 ——
 * 所以这里直接读，不依赖任何其他上下文，也不会为了查列表把 offscreen 拉起来。
 */
async function refreshRecoverable() {
  try {
    const sessions = (await store.listRecoverable()).map((m) => ({
      sessionId: m.sessionId,
      tabId: m.tabId,
      title: m.title,
      bytes: m.bytes,
      chunks: m.chunks,
      startedAt: m.startedAt,
    }));

    const previous = registry.getRecoverable().length;
    registry.setRecoverable(sessions);
    await registry.flush();

    if (sessions.length !== previous) {
      log.info(`可恢复录音: ${sessions.length} 段`);
    }
  } catch (err) {
    log.debug('读取可恢复录音失败（不影响主流程）', err);
  }
}

/**
 * 候选列表 = audible ∪ armed ∪ capturing。
 *
 * **绝不因为 audible 变 false 就移除一行** —— audible 有约 2 秒防抖、
 * 会在说话间隙抖动，而且被捕获的 Tab 是否还上报 audible 未经验证。
 * 取并集意味着一旦进入列表就不会因为这个不可靠信号而消失。
 */
async function listCandidates() {
  /** @type {Map<number, any>} */
  const merged = new Map();

  // 已知记录优先（它们带状态）
  for (const r of registry.all()) {
    merged.set(r.tabId, { ...r, known: true });
  }

  // 补上正在出声但还没授权的
  try {
    const audible = await chrome.tabs.query({ audible: true });
    for (const tab of audible) {
      if (typeof tab.id !== 'number') continue;
      if (merged.has(tab.id)) {
        merged.set(tab.id, { ...merged.get(tab.id), audible: true });
        continue;
      }
      if (!arming.isCapturable(tab.url)) continue;
      merged.set(tab.id, {
        tabId: tab.id,
        state: S.IDLE,
        title: tab.title ?? '',
        url: tab.url ?? '',
        origin: arming.originOf(tab.url),
        index: 0,
        audible: true,
        favIconUrl: tab.favIconUrl,
        known: false,
      });
    }
  } catch (err) {
    log.warn('查询 audible 标签页失败', err);
  }

  // 排序：录制中 > 已授权 > 其余；同组内按授权顺序
  const rank = (/** @type {any} */ r) => {
    if ([S.STARTING, S.RECORDING, S.STOPPING, S.FINALIZING, S.SAVING].includes(r.state)) return 0;
    if (r.state === S.ARMED) return 1;
    if (r.state === S.DONE || r.state === S.ERROR) return 2;
    return 3;
  };
  return Array.from(merged.values()).sort(
    (a, b) => rank(a) - rank(b) || (a.index || 999) - (b.index || 999),
  );
}

// ───────────────────────── Tab 生命周期接线 ─────────────────────────

tabWatch.install({
  async onTabGone(tabId, reason) {
    const record = registry.get(tabId);
    if (!record) return;

    // 正在录制：让 offscreen 收尾保存已录部分。
    // track 的 ended 事件通常会先到（那是主路径），这里是第二重保险。
    if ([S.RECORDING, S.STARTING, S.STOPPING].includes(record.state)) {
      try {
        await sendTo('offscreen', OFF.STOP_ONE, { tabId, reason });
        log.info(`tab=${tabId} 关闭，已请求收尾保存`);
      } catch (err) {
        log.warn(`请求收尾失败 tab=${tabId}`, err);
      }
      // 不立刻移除记录 —— 等 SAVED 事件到达，用户需要看到文件名
      return;
    }

    if (typeof record.downloadId === 'number' || [S.FINALIZING, S.SAVING].includes(record.state)) {
      return; // Retain the file and its send button after closing the source tab.
    }
    registry.remove(tabId);
    await registry.flush();
  },

  async onGrantLost(tabId) {
    if (registry.get(tabId)?.state === S.DONE) {
      registry.upsert(tabId, { origin: '' });
      await registry.flush();
      return;
    }
    registry.setState(tabId, S.IDLE, { origin: '' });
    await arming.renderBadge(tabId, S.IDLE);
    await registry.flush();
  },
});

log.info('service worker 已加载');

/**
 * 模块顶层的一次 bootstrap —— SW 被杀后重启走的是这条路径，**不触发 onStartup**
 * （onStartup 只在浏览器启动时发一次）。没有它，SW 每次被唤醒后 registry 都是空的。
 *
 * ⚠️ 必须放在**文件末尾**，在 bootstrapping 等所有 `let`/`const` 声明之后。
 * ESM 的暂时性死区意味着，把它放在声明之前会抛
 * "Cannot access 'bootstrapping' before initialization"，
 * 而那会**中断整个模块求值** —— 后面所有 addListener 都不会执行，
 * 症状是「扩展装上了，但点图标、按快捷键全都没反应」。
 * 见 tests/service-worker.test.js 里的加载期回归测试。
 */
void bootstrap();
